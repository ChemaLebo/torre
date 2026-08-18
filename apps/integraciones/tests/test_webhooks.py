"""Tests del endpoint de webhooks: idempotencia de entrada y validación HMAC."""
import base64
import hashlib
import hmac
import json
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, EventoAuditoria
from apps.integraciones.models import SyncLog, Tienda, WebhookEvento
from apps.integraciones.services import procesar_webhook


def payload_orders_create(order_id=5479812345678, numero=1001):
    """Payload de prueba con la estructura estándar de Shopify orders/create."""
    return {
        "id": order_id,
        "order_number": numero,
        "name": f"#{numero}",
        "email": "comprador@ejemplo.mx",
        "created_at": "2026-07-21T10:15:00-06:00",
        "updated_at": "2026-07-21T10:15:00-06:00",
        "cancelled_at": None,
        "currency": "MXN",
        "total_price": "620.00",
        "financial_status": "paid",
        "note": "",
        "customer": {
            "id": 700001,
            "first_name": "Ana",
            "last_name": "Preciado",
            "email": "comprador@ejemplo.mx",
            "phone": "+523121234567",
        },
        "shipping_address": {
            "first_name": "Ana",
            "last_name": "Preciado",
            "address1": "Av. Tecnológico 380",
            "address2": "Local E",
            "city": "Colima",
            "province": "Colima",
            "province_code": "COL",
            "country": "Mexico",
            "zip": "28017",
            "phone": "+523121234567",
        },
        "line_items": [
            {"id": 1101, "sku": "COLIMITA-SIX", "quantity": 2, "title": "Colimita Lager Six", "price": "155.00", "grams": 2500},
            {"id": 1102, "sku": "PARAMO-SIX", "quantity": 2, "title": "Páramo Pale Ale Six", "price": "155.00", "grams": 2500},
        ],
    }


@override_settings(DEBUG=True)  # tienda sin token solo se acepta en modo dev/demo
class WebhookShopifyTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="colima-mx.myshopify.com")
        self.url = reverse("integraciones:webhook_shopify", args=[self.tienda.pk])

    def _post(self, payload, webhook_id=None, topic="orders/create", firma=None):
        headers = {"X-Shopify-Topic": topic}
        if webhook_id:
            headers["X-Shopify-Webhook-Id"] = webhook_id
        if firma:
            headers["X-Shopify-Hmac-Sha256"] = firma
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json", headers=headers,
        )

    def test_idempotencia_mismo_webhook_id_procesa_un_solo_pedido(self):
        """Mismo webhook_id dos veces = un solo WebhookEvento y UNA sola ingesta."""
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            ingerir.return_value = mock.Mock(folio="PED-00001")
            r1 = self._post(payload_orders_create(), webhook_id="wh-abc-123")
            r2 = self._post(payload_orders_create(), webhook_id="wh-abc-123")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json().get("duplicado"))
        self.assertEqual(WebhookEvento.objects.filter(webhook_id="wh-abc-123").count(), 1)
        self.assertEqual(ingerir.call_count, 1)

    def test_sin_header_webhook_id_el_mismo_cuerpo_sigue_siendo_idempotente(self):
        """Pruebas manuales sin header: el id se deriva del contenido."""
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            ingerir.return_value = mock.Mock(folio="PED-00002")
            self._post(payload_orders_create(order_id=111))
            self._post(payload_orders_create(order_id=111))
        self.assertEqual(WebhookEvento.objects.count(), 1)
        self.assertEqual(ingerir.call_count, 1)

    def test_hmac_invalido_regresa_401_y_no_guarda(self):
        self.tienda.webhook_secret = "shhh-secreto-colima"
        self.tienda.save()
        r = self._post(payload_orders_create(), webhook_id="wh-mal", firma="ZmlybWEtZmFsc2E=")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(WebhookEvento.objects.count(), 0)

    def test_hmac_valido_pasa(self):
        self.tienda.webhook_secret = "shhh-secreto-colima"
        self.tienda.save()
        cuerpo = json.dumps(payload_orders_create()).encode("utf-8")
        firma = base64.b64encode(
            hmac.new(self.tienda.webhook_secret.encode(), cuerpo, hashlib.sha256).digest()
        ).decode("ascii")
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            ingerir.return_value = mock.Mock(folio="PED-00003")
            r = self.client.post(
                self.url, data=cuerpo, content_type="application/json",
                headers={
                    "X-Shopify-Topic": "orders/create",
                    "X-Shopify-Webhook-Id": "wh-firmado",
                    "X-Shopify-Hmac-Sha256": firma,
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WebhookEvento.objects.filter(webhook_id="wh-firmado", procesado=True).count(), 1)

    def test_json_invalido_regresa_400(self):
        r = self.client.post(self.url, data="esto no es json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_tienda_inexistente_regresa_404(self):
        url = reverse("integraciones:webhook_shopify", args=[99999])
        r = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_webhook_recibido_queda_en_auditoria(self):
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            ingerir.return_value = mock.Mock(folio="PED-00004")
            self._post(payload_orders_create(), webhook_id="wh-audit")
        self.assertTrue(
            EventoAuditoria.objects.filter(entidad="webhook", entidad_id="wh-audit", accion="recibido").exists()
        )


class WebhookSinSecretoEnProduccionTests(TestCase):
    """Fail-closed: con DEBUG=0 (default en tests) una tienda sin webhook_secret
    NO acepta webhooks — el endpoint es público y tienda_id es secuencial. La
    firma se valida contra webhook_secret, JAMÁS contra el access token."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="colima-mx.myshopify.com")
        self.url = reverse("integraciones:webhook_shopify", args=[self.tienda.pk])

    def _post_firmado(self, llave, webhook_id):
        cuerpo = json.dumps(payload_orders_create()).encode("utf-8")
        firma = base64.b64encode(
            hmac.new(llave.encode(), cuerpo, hashlib.sha256).digest()
        ).decode("ascii")
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            ingerir.return_value = mock.Mock(folio="PED-00009")
            return self.client.post(
                self.url, data=cuerpo, content_type="application/json",
                headers={
                    "X-Shopify-Topic": "orders/create",
                    "X-Shopify-Webhook-Id": webhook_id,
                    "X-Shopify-Hmac-Sha256": firma,
                },
            )

    def test_sin_webhook_secret_rechaza_403_y_no_guarda_nada(self):
        # Aunque el access token SÍ esté configurado: el token no es el secreto.
        self.tienda.token = "shpat_token_de_api"
        self.tienda.save()
        r = self.client.post(
            self.url, data=json.dumps(payload_orders_create()),
            content_type="application/json",
            headers={"X-Shopify-Webhook-Id": "wh-prod-abierto"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(WebhookEvento.objects.count(), 0)

    def test_con_webhook_secret_y_firma_valida_pasa(self):
        # El access token puede estar vacío: el webhook no depende de él.
        self.tienda.webhook_secret = "secreto-de-webhooks"
        self.tienda.save()
        r = self._post_firmado("secreto-de-webhooks", "wh-prod-firmado")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WebhookEvento.objects.filter(webhook_id="wh-prod-firmado").count(), 1)

    def test_firma_hecha_con_el_access_token_se_rechaza(self):
        """El bug que este campo corrige: Shopify jamás firma con el shpat_ —
        una firma hecha con el token debe rechazarse, no aceptarse."""
        self.tienda.token = "shpat_token_de_api"
        self.tienda.webhook_secret = "secreto-de-webhooks"
        self.tienda.save()
        r = self._post_firmado("shpat_token_de_api", "wh-prod-mal-firmado")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(WebhookEvento.objects.count(), 0)


class ProcesarWebhookTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="colima-mx.myshopify.com")

    def _evento(self, topic, webhook_id, payload=None):
        return WebhookEvento.objects.create(
            tienda=self.tienda, webhook_id=webhook_id, topic=topic,
            payload=payload or payload_orders_create(),
        )

    def test_orders_cancelled_rutea_a_ingerir_con_origen(self):
        payload = payload_orders_create()
        payload["cancelled_at"] = "2026-07-21T12:00:00-06:00"
        evento = self._evento("orders/cancelled", "wh-cancel", payload)
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            ingerir.return_value = mock.Mock(folio="PED-00005")
            procesar_webhook(evento)
        ingerir.assert_called_once_with(self.tienda, payload, origen="webhook")
        evento.refresh_from_db()
        self.assertTrue(evento.procesado)

    def test_evento_ya_procesado_es_noop(self):
        evento = self._evento("orders/create", "wh-noop")
        evento.procesado = True
        evento.save()
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            resultado = procesar_webhook(evento)
        self.assertIsNone(resultado)
        ingerir.assert_not_called()

    def test_topic_desconocido_se_marca_procesado_sin_ingerir(self):
        evento = self._evento("products/update", "wh-otro", {"id": 1})
        with mock.patch("apps.pedidos.services.ingerir_pedido_shopify") as ingerir:
            procesar_webhook(evento)
        ingerir.assert_not_called()
        evento.refresh_from_db()
        self.assertTrue(evento.procesado)

    def test_error_de_ingesta_deja_synclog_error_y_evento_para_replay(self):
        evento = self._evento("orders/create", "wh-error")
        with mock.patch(
            "apps.pedidos.services.ingerir_pedido_shopify", side_effect=RuntimeError("stock roto"),
        ):
            resultado = procesar_webhook(evento)
        self.assertIsNone(resultado)
        evento.refresh_from_db()
        self.assertFalse(evento.procesado)  # queda para replay
        log = SyncLog.objects.filter(tienda=self.tienda, direccion="ingesta", resultado="error").first()
        self.assertIsNotNone(log)
        self.assertIn("stock roto", log.detalle)
