"""Webhooks de rastreo de envia y 99minutos (Chema 2026-09-30): el evento entra
por el mismo camino que el poller y el poller queda de respaldo."""
import hashlib
import hmac
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.models import EventoGuia, Guia
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda

TOKEN = "token-de-prueba-envia"
TOKEN_99 = "token-de-prueba-99"


@override_settings(ENVIA_API_KEY="", ENVIA_WEBHOOK_TOKEN=TOKEN, ENVIA_WEBHOOK_SECRET="", NOVENTA9_WEBHOOK_TOKEN=TOKEN_99)
class WebhookEnviaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente, crear_tienda(self.cliente), estado="RECOLECTADO")
        self.guia = Guia.objects.create(pedido=self.pedido, carrier="estafeta", numero="EST-777", proveedor="envia",
                                        estado=Guia.RECOLECTADO)
        self.url = reverse("integraciones:webhook_envia", args=[TOKEN])

    def _post(self, payload, url=None, **headers):
        return self.client.post(url or self.url, json.dumps(payload), content_type="application/json", **headers)

    def evento(self, status="in_transit", descripcion="Package in transit", numero="EST-777"):
        return {"type": "tracking.simple", "created_at": "2026-09-30T18:05:00.000Z",
                "data": {"shipment_id": 1, "tracking_number": numero, "carrier_name": "Estafeta",
                         "status": status, "status_description": descripcion, "location": "Guadalajara, MX"}}

    def test_token_invalido_o_sin_configurar_cierra_el_endpoint(self):
        self.assertEqual(self._post(self.evento(), url=reverse("integraciones:webhook_envia", args=["otro"])).status_code, 403)
        with override_settings(ENVIA_WEBHOOK_TOKEN=""):
            self.assertEqual(self._post(self.evento()).status_code, 403)
        self.guia.refresh_from_db()
        self.assertEqual(self.guia.estado, Guia.RECOLECTADO)

    def test_evento_mueve_la_guia_y_el_pedido_como_el_poller(self):
        r = self._post(self.evento())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["estado"], "EN_TRANSITO")
        self.guia.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual((self.guia.estado, self.pedido.estado), (Guia.EN_TRANSITO, "EN_TRANSITO"))
        self.assertIn("Guadalajara", self.guia.ultimo_evento)
        self.assertEqual(EventoGuia.objects.filter(guia=self.guia).count(), 1)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(self.guia.pk), accion="evento_carrier_webhook").exists())
        # El mismo evento otra vez (reintento del carrier o el poller): sin duplicar.
        self._post(self.evento())
        self.assertEqual(EventoGuia.objects.filter(guia=self.guia).count(), 1)

    def test_guia_desconocida_o_de_otro_proveedor_responde_200_sin_tocar_nada(self):
        r = self._post(self.evento(numero="NO-EXISTE"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("desconocida", r.json()["motivo"])
        otra = Guia.objects.create(pedido=self.pedido, carrier="noventa9Minutos", numero="99-1", proveedor="99minutos")
        r = self._post(self.evento(numero="99-1"))
        self.assertFalse(r.json()["ok"])
        otra.refresh_from_db()
        self.assertEqual(otra.estado, Guia.GUIA_CREADA)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="webhook_carrier", accion="webhook_ignorado").exists())

    def test_con_secreto_se_exige_la_firma_v1(self):
        payload = self.evento("delivered", "Delivered")
        cuerpo = json.dumps(payload)
        with override_settings(ENVIA_WEBHOOK_SECRET="secreto"):
            r = self.client.post(self.url, cuerpo, content_type="application/json", HTTP_X_WEBHOOK_SIGNATURE="v1=mala")
            self.assertEqual(r.status_code, 401)
            base = f"1759250700000.tracking.simple.{cuerpo}".encode("utf-8")
            firma = hmac.new(b"secreto", base, hashlib.sha256).hexdigest()
            r = self.client.post(self.url, cuerpo, content_type="application/json", HTTP_X_WEBHOOK_SIGNATURE=f"v1={firma}",
                                 HTTP_X_WEBHOOK_TIMESTAMP="1759250700000", HTTP_X_WEBHOOK_EVENT="tracking.simple")
        self.assertEqual(r.status_code, 200)
        self.guia.refresh_from_db()
        self.assertEqual(self.guia.estado, Guia.ENTREGADO)

    def test_evento_que_no_es_de_rastreo_se_ignora(self):
        r = self._post({"type": "surcharge", "data": {"surcharge_data": {"amount": 10}}})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])


@override_settings(ENVIA_API_KEY="", ENVIA_WEBHOOK_TOKEN=TOKEN, NOVENTA9_WEBHOOK_TOKEN=TOKEN_99)
class Webhook99MinutosTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente, crear_tienda(self.cliente), estado="RECOLECTADO")
        self.guia = Guia.objects.create(pedido=self.pedido, carrier="noventa9Minutos", numero="2943210141",
                                        proveedor="99minutos", estado=Guia.RECOLECTADO)
        self.url = reverse("integraciones:webhook_99minutos", args=[TOKEN_99])

    def test_shipment_en_pascal_case_con_historial_entrega_y_guarda_eventos(self):
        payload = {
            "StatusName": "delivered", "TrackingId": "2943210141", "InternalKey": "PED-00001-1-r1",
            "Events": [
                {"StatusCode": "2003", "StatusName": "pickedUp", "CreatedAt": "2026-09-30T14:00:00Z", "Data": {}},
                {"StatusCode": "3001", "StatusName": "inTransit", "CreatedAt": "2026-09-30T15:00:00Z", "Data": {"comment": "Hub Colima"}},
                {"StatusCode": "4001", "StatusName": "delivered", "CreatedAt": "2026-09-30T18:30:00Z", "Data": {"comment": "Recibió Ana"}},
            ],
        }
        r = self.client.post(self.url, json.dumps(payload), content_type="application/json", HTTP_USER_AGENT="99notifications")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["ok"])
        self.guia.refresh_from_db()
        self.pedido.refresh_from_db()
        from apps.envios.adapters import CODIGOS_ESTADO_99MIN
        esperado = CODIGOS_ESTADO_99MIN.get(4001) or "ENTREGADO"
        self.assertEqual(self.guia.estado, esperado)
        self.assertEqual(EventoGuia.objects.filter(guia=self.guia).count(), 3)
        self.assertIn("Recibió Ana", self.guia.ultimo_evento)

    def test_token_invalido(self):
        r = self.client.post(reverse("integraciones:webhook_99minutos", args=["x"]), "{}", content_type="application/json")
        self.assertEqual(r.status_code, 403)
