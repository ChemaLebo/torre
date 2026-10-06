"""Cola de reintentos de escrituras de fulfillment a Shopify (2026-09-30) y
rastreo del fulfillment sustituido por una reposición.

Antes cada escritura era un solo intento: Shopify caído al firmar el
manifiesto = orden sin fulfillear y comprador sin correo, para siempre. Ahora
lo rechazado se encola (EscrituraShopifyPendiente) y sync_shopify lo
reintenta hasta que entra o vence.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.integraciones.models import EscrituraShopifyPendiente, SyncLog
from apps.integraciones.services import (
    marcar_fulfillment, registrar_evento_fulfillment, reintentar_escrituras_shopify,
)
from apps.integraciones.shopify import ShopifyError
from apps.pedidos.models import LineaPedido

FO = "gid://shopify/FulfillmentOrder/1"
LINEA_FO = "gid://shopify/FulfillmentOrderLineItem/10"


class Base(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba")
        self.pedido = crear_pedido(self.cliente, self.tienda)
        self.pedido.shopify_order_id = "5479812345678"
        self.pedido.save(update_fields=["shopify_order_id"])
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=2000)
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=2, cantidad_pickeada=2)
        self.c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal(4), carrier="estafeta", estado="EMPACADO")
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=self.linea, cantidad=2)
        self.g1 = Guia.objects.create(pedido=self.pedido, paquete=self.c1, carrier="estafeta", numero="ETQ-1")

    def _api(self, cliente_cls, restante=2):
        api = cliente_cls.return_value
        api.fulfillment_orders_lineas.return_value = [{
            "gid": FO, "status": "OPEN", "location_gid": "",
            "lineas": [{"line_item_id": "5", "fo_line_item_id": LINEA_FO, "sku": "SIX", "cantidad": restante}],
        }]
        api.crear_fulfillment.return_value = {"id": "gid://shopify/Fulfillment/A", "status": "SUCCESS"}
        return api


class EncolarTests(Base):
    def test_fulfillment_rechazado_se_encola_y_el_reintento_lo_escribe(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders_lineas.side_effect = ShopifyError("502 caído")
            self.assertFalse(marcar_fulfillment(self.pedido, cajas=[self.c1], notificar=True))
        escritura = EscrituraShopifyPendiente.objects.get()
        self.assertEqual(escritura.accion, EscrituraShopifyPendiente.ACCION_FULFILLMENT)
        self.assertEqual(escritura.datos, {"cajas": [self.c1.pk], "evento_inicial": "CARRIER_PICKED_UP", "notificar": True})
        self.assertEqual(escritura.intentos, 1)
        self.assertIn("502", escritura.ultimo_error)
        self.assertGreater(escritura.vence, timezone.now() + timedelta(hours=23))

        # Segunda falla en línea (p. ej. el carrier avisó y luego el manifiesto): mismo renglón.
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders_lineas.side_effect = ShopifyError("502 sigue caído")
            marcar_fulfillment(self.pedido, cajas=[self.c1], notificar=True)
        escritura.refresh_from_db()
        self.assertEqual(EscrituraShopifyPendiente.objects.count(), 1)
        self.assertEqual(escritura.intentos, 2)

        # Shopify vuelve: el cron la escribe con notificar=True (el correo que faltaba) y la borra.
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            resumen = reintentar_escrituras_shopify()
        self.assertEqual(resumen, {"pendientes": 1, "ok": 1, "error": 0, "vencidas": 0})
        self.assertTrue(api.crear_fulfillment.call_args.kwargs["notificar"])
        self.assertFalse(EscrituraShopifyPendiente.objects.exists())
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/A", "CARRIER_PICKED_UP"))

    def test_pedido_entero_rechazado_se_encola_sin_cajas(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders.side_effect = ShopifyError("500")
            self.assertFalse(marcar_fulfillment(self.pedido, evento_inicial="DELIVERED"))
        escritura = EscrituraShopifyPendiente.objects.get()
        self.assertEqual(escritura.datos["cajas"], [])
        self.assertEqual(escritura.datos["evento_inicial"], "DELIVERED")
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.fulfillment_orders.return_value = [(FO, "OPEN", "")]
            api.crear_fulfillment.return_value = {"id": "gid://shopify/Fulfillment/Z"}
            self.assertEqual(reintentar_escrituras_shopify()["ok"], 1)
        api.crear_fulfillment.assert_called_once()
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/Z", "DELIVERED"))

    def test_evento_rechazado_se_encola_y_se_reproduce_con_su_hora(self):
        self.c1.shopify_fulfillment_id = "gid://shopify/Fulfillment/A"
        self.c1.save(update_fields=["shopify_fulfillment_id"])
        ts = timezone.now() - timedelta(hours=1)
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.crear_evento_fulfillment.side_effect = ShopifyError("429")
            self.assertFalse(registrar_evento_fulfillment(self.pedido, self.g1, "EN_RUTA", descripcion="Salió", ts=ts))
        escritura = EscrituraShopifyPendiente.objects.get()
        self.assertEqual(escritura.accion, EscrituraShopifyPendiente.ACCION_EVENTO)
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            self.assertEqual(reintentar_escrituras_shopify()["ok"], 1)
        api.crear_evento_fulfillment.assert_called_once_with(
            "gid://shopify/Fulfillment/A", "OUT_FOR_DELIVERY", happened_at=ts, message="Salió",
        )

    def test_evento_sin_fulfillment_espera_detras_del_fulfillment_en_cola(self):
        """Shopify caído al manifiesto; el poller trae EN_TRANSITO 30 min después:
        el evento no se pierde, se escribe cuando el fulfillment por fin entra."""
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders_lineas.side_effect = ShopifyError("502")
            marcar_fulfillment(self.pedido, cajas=[self.c1])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertFalse(registrar_evento_fulfillment(self.pedido, self.g1, "EN_TRANSITO"))
        self.assertEqual(EscrituraShopifyPendiente.objects.count(), 2)
        self.assertIn("espera al fulfillment pendiente", SyncLog.objects.latest("ts").detalle)
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            resumen = reintentar_escrituras_shopify()
        self.assertEqual(resumen["ok"], 2)
        estados = [c.args[1] for c in api.crear_evento_fulfillment.call_args_list]
        self.assertEqual(estados, ["CARRIER_PICKED_UP", "IN_TRANSIT"])

    def test_sin_fulfillment_ni_cola_sigue_siendo_el_error_legado(self):
        with patch("apps.integraciones.services.ShopifyClient"):
            self.assertFalse(registrar_evento_fulfillment(self.pedido, self.g1, "EN_TRANSITO"))
        self.assertFalse(EscrituraShopifyPendiente.objects.exists())
        self.assertIn("shopify_eventos_backfill", SyncLog.objects.latest("ts").detalle)

    def test_la_vencida_no_se_reintenta_y_la_que_sigue_fallando_suma_intentos(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders_lineas.side_effect = ShopifyError("502")
            marcar_fulfillment(self.pedido, cajas=[self.c1])
        escritura = EscrituraShopifyPendiente.objects.get()
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders_lineas.side_effect = ShopifyError("502 aún")
            resumen = reintentar_escrituras_shopify()
        self.assertEqual(resumen["error"], 1)
        escritura.refresh_from_db()
        self.assertEqual(escritura.intentos, 2)
        self.assertIn("aún", escritura.ultimo_error)
        escritura.vence = timezone.now() - timedelta(minutes=1)
        escritura.save(update_fields=["vence"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            resumen = reintentar_escrituras_shopify()
        self.assertEqual(resumen, {"pendientes": 0, "ok": 0, "error": 0, "vencidas": 1})
        cliente_cls.return_value.fulfillment_orders_lineas.assert_not_called()
        self.assertTrue(escritura.vencida)

    def test_cajas_ya_con_fulfillment_no_caen_al_pedido_entero(self):
        """El carrier recogió la caja antes (ya tiene id) y luego llega el
        manifiesto: nada que escribir; jamás un fulfillment del pedido entero
        con otras cajas todavía en bodega."""
        self.c1.shopify_fulfillment_id = "gid://shopify/Fulfillment/A"
        self.c1.save(update_fields=["shopify_fulfillment_id"])
        Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal(1), carrier="estafeta", estado="EMPACADO")
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.fulfillment_orders.return_value = [(FO, "OPEN", "")]
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c1]))
        api.crear_fulfillment.assert_not_called()
        self.assertIn("nada que escribir", SyncLog.objects.latest("ts").detalle)


class TrackingReposicionTests(Base):
    """La caja de reposición no crea fulfillment: el de la caja sustituida
    cambia de guía a la nueva y avisa al comprador; sus eventos cuelgan de ahí."""

    def setUp(self):
        super().setUp()
        self.c1.estado = "DESPACHADO"
        self.c1.shopify_fulfillment_id = "gid://shopify/Fulfillment/A"
        self.c1.save(update_fields=["estado", "shopify_fulfillment_id"])
        self.g1.estado = Guia.ENTREGADO
        self.g1.save(update_fields=["estado"])
        self.c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal(2), carrier="imile", estado="EMPACADO")
        PaqueteLinea.objects.create(paquete=self.c2, linea_pedido=self.linea, cantidad=1, repone_a=self.c1)  # misma línea
        self.g2 = Guia.objects.create(pedido=self.pedido, paquete=self.c2, carrier="imile", numero="IM-2", proveedor="envia")

    def test_actualiza_el_rastreo_del_fulfillment_sustituido_y_avisa(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2], notificar=True))
        api.crear_fulfillment.assert_not_called()
        api.actualizar_tracking_fulfillment.assert_called_once()
        fid, carrier, numero, url = api.actualizar_tracking_fulfillment.call_args.args
        self.assertEqual((fid, carrier, numero), ("gid://shopify/Fulfillment/A", "imile", "IM-2"))
        self.assertIn("/r/", url)
        self.assertTrue(api.actualizar_tracking_fulfillment.call_args.kwargs["notificar"])
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/A", "CARRIER_PICKED_UP"))
        self.assertIn("reposición", SyncLog.objects.filter(resultado=SyncLog.RESULTADO_OK).latest("ts").detalle)
        # Reintento con la caja ya ligada: nada que escribir.
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2], notificar=True))
        cliente_cls.return_value.actualizar_tracking_fulfillment.assert_not_called()

    def test_los_eventos_de_la_caja_de_reposicion_cuelgan_del_fulfillment_sustituido(self):
        self.c2.shopify_fulfillment_id = "gid://shopify/Fulfillment/A"
        self.c2.save(update_fields=["shopify_fulfillment_id"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            self.assertTrue(registrar_evento_fulfillment(self.pedido, self.g2, "ENTREGADO"))
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/A", "DELIVERED"))

    def test_entrega_propia_viaja_como_wop_con_la_pagina_publica(self):
        self.g2.carrier = "local"
        self.g2.numero = "LOCAL-X-2"
        self.g2.save(update_fields=["carrier", "numero"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2]))
        _, carrier, numero, url = api.actualizar_tracking_fulfillment.call_args.args
        self.assertEqual((carrier, numero), ("WOP", ""))
        self.assertIn("/r/", url)

    def test_sin_fulfillment_original_queda_en_synclog_sin_cola(self):
        self.c1.shopify_fulfillment_id = ""
        self.c1.save(update_fields=["shopify_fulfillment_id"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertFalse(marcar_fulfillment(self.pedido, cajas=[self.c2]))
        cliente_cls.return_value.actualizar_tracking_fulfillment.assert_not_called()
        self.assertIn("sin fulfillment original", SyncLog.objects.latest("ts").detalle)
        self.assertFalse(EscrituraShopifyPendiente.objects.exists())

    def test_shopify_caido_encola_el_rastreo_y_el_cron_lo_reintenta(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.actualizar_tracking_fulfillment.side_effect = ShopifyError("503")
            self.assertFalse(marcar_fulfillment(self.pedido, cajas=[self.c2], notificar=True))
        escritura = EscrituraShopifyPendiente.objects.get()
        self.assertEqual(escritura.accion, EscrituraShopifyPendiente.ACCION_TRACKING)
        self.assertEqual(escritura.datos, {"caja": self.c2.pk, "notificar": True, "evento_inicial": "CARRIER_PICKED_UP"})
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            self.assertEqual(reintentar_escrituras_shopify()["ok"], 1)
        self.assertTrue(api.actualizar_tracking_fulfillment.call_args.kwargs["notificar"])
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")


class ClienteShopifyTests(TestCase):
    def test_actualizar_tracking_manda_la_mutacion_y_levanta_user_errors(self):
        from apps.integraciones.shopify import ShopifyClient

        cliente = crear_cliente()
        tienda = crear_tienda(cliente, token="shpat_prueba")
        api = ShopifyClient(tienda)
        with patch.object(ShopifyClient, "graphql", return_value={"fulfillmentTrackingInfoUpdate": {"fulfillment": {"id": "gid://shopify/Fulfillment/A", "status": "SUCCESS"}, "userErrors": []}}) as graphql:
            resultado = api.actualizar_tracking_fulfillment("gid://shopify/Fulfillment/A", "imile", "IM-2", "https://t/r/x/", notificar=True)
        self.assertEqual(resultado["id"], "gid://shopify/Fulfillment/A")
        variables = graphql.call_args.args[1]
        self.assertEqual(variables["trackingInfoInput"], {"company": "imile", "url": "https://t/r/x/", "number": "IM-2"})
        self.assertTrue(variables["notifyCustomer"])
        with patch.object(ShopifyClient, "graphql", return_value={"fulfillmentTrackingInfoUpdate": {"fulfillment": None, "userErrors": [{"field": "id", "message": "no existe"}]}}):
            with self.assertRaises(ShopifyError):
                api.actualizar_tracking_fulfillment("gid://shopify/Fulfillment/A", "imile", "IM-2", "")

    def test_cancelar_fulfillment_manda_la_mutacion_y_levanta_user_errors(self):
        from apps.integraciones.shopify import ShopifyClient

        api = ShopifyClient(crear_tienda(crear_cliente(), token="shpat_prueba"))
        with patch.object(ShopifyClient, "graphql", return_value={"fulfillmentCancel": {"fulfillment": {"id": "gid://shopify/Fulfillment/A", "status": "CANCELLED"}, "userErrors": []}}) as graphql:
            resultado = api.cancelar_fulfillment("gid://shopify/Fulfillment/A")
        self.assertEqual(resultado["status"], "CANCELLED")
        self.assertIn("fulfillmentCancel", graphql.call_args.args[0])
        self.assertEqual(graphql.call_args.args[1], {"id": "gid://shopify/Fulfillment/A"})
        with patch.object(ShopifyClient, "graphql", return_value={"fulfillmentCancel": {"fulfillment": None, "userErrors": [{"field": "id", "message": "ya cancelado"}]}}):
            with self.assertRaises(ShopifyError):
                api.cancelar_fulfillment("gid://shopify/Fulfillment/A")


class CancelarFulfillmentCajaTests(Base):
    """"Quitar de salida" (2026-10-05): el fulfillment de la caja que nunca se
    fue se cancela en Shopify y la caja se desliga; si falla, a la cola."""

    FID = "gid://shopify/Fulfillment/A"

    def setUp(self):
        super().setUp()
        Paquete.objects.filter(pk=self.c1.pk).update(shopify_fulfillment_id=self.FID)
        self.c1.refresh_from_db()

    def test_cancela_en_shopify_y_desliga_la_caja(self):
        from apps.core.models import EventoAuditoria
        from apps.integraciones.services import cancelar_fulfillment_caja

        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.cancelar_fulfillment.return_value = {"id": self.FID, "status": "CANCELLED"}
            self.assertTrue(cancelar_fulfillment_caja(self.pedido, self.c1))
        api.cancelar_fulfillment.assert_called_once_with(self.FID)
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.shopify_fulfillment_id, "")
        self.assertEqual(SyncLog.objects.latest("ts").resultado, SyncLog.RESULTADO_OK)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="paquete", entidad_id=str(self.c1.pk), accion="fulfillment_cancelado_shopify").exists())
        # Sin id no hay nada que hacer (y no se llama a Shopify).
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertTrue(cancelar_fulfillment_caja(self.pedido, self.c1))
        cliente_cls.return_value.cancelar_fulfillment.assert_not_called()

    def test_id_compartido_con_otra_caja_o_con_el_pedido_solo_se_desliga(self):
        from apps.integraciones.services import cancelar_fulfillment_caja

        c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal(4), carrier="estafeta", estado="DESPACHADO", shopify_fulfillment_id=self.FID)
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertTrue(cancelar_fulfillment_caja(self.pedido, self.c1))
        cliente_cls.return_value.cancelar_fulfillment.assert_not_called()
        self.c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual((self.c1.shopify_fulfillment_id, c2.shopify_fulfillment_id), ("", self.FID))
        self.assertIn("no se cancela", SyncLog.objects.latest("ts").detalle)
        # El del pedido entero (líneas no separables) tampoco se cancela.
        Paquete.objects.filter(pk=c2.pk).update(shopify_fulfillment_id="")
        Paquete.objects.filter(pk=self.c1.pk).update(shopify_fulfillment_id=self.FID)
        self.pedido.shopify_fulfillment_id = self.FID
        self.pedido.save(update_fields=["shopify_fulfillment_id"])
        self.c1.refresh_from_db()
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertTrue(cancelar_fulfillment_caja(self.pedido, self.c1))
        cliente_cls.return_value.cancelar_fulfillment.assert_not_called()

    def test_shopify_caido_se_encola_y_el_reintento_cancela_con_el_id_guardado(self):
        from apps.integraciones.services import cancelar_fulfillment_caja

        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.cancelar_fulfillment.side_effect = ShopifyError("502 caído")
            self.assertFalse(cancelar_fulfillment_caja(self.pedido, self.c1))
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.shopify_fulfillment_id, "")  # la caja ya quedó desligada en Torre
        escritura = EscrituraShopifyPendiente.objects.get()
        self.assertEqual((escritura.accion, escritura.datos), (EscrituraShopifyPendiente.ACCION_CANCELAR, {"fid": self.FID, "caja": self.c1.pk}))
        self.assertIn("502", escritura.ultimo_error)
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.cancelar_fulfillment.return_value = {"id": self.FID, "status": "CANCELLED"}
            resumen = reintentar_escrituras_shopify()
        self.assertEqual(resumen, {"pendientes": 1, "ok": 1, "error": 0, "vencidas": 0})
        api.cancelar_fulfillment.assert_called_once_with(self.FID)
        self.assertFalse(EscrituraShopifyPendiente.objects.exists())
