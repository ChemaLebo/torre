"""Write-back de fulfillment a Shopify al firmar el manifiesto.

Hermano del "va en camino" (mismo momento canónico, módulos separados):
Shopify manda SU correo nativo de envío con el link a NUESTRA página brandeada
y el admin del cliente muestra Fulfilled. Best-effort total: jamás bloquea.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.envios.models import Guia
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.integraciones.models import SyncLog
from apps.integraciones.services import marcar_fulfillment


def crear_guia(pedido, numero="ETQ-1", carrier="estafeta"):
    return Guia.objects.create(
        pedido=pedido, carrier=carrier, servicio="ground", numero=numero,
    )


class BaseFulfillment(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba")
        self.pedido = crear_pedido(self.cliente, self.tienda, cp="06600")
        self.pedido.shopify_order_id = "5479812345678"
        self.pedido.save(update_fields=["shopify_order_id"])


class MarcarFulfillmentTests(BaseFulfillment):
    def test_feliz_crea_fulfillment_con_tracking_y_pagina_brandeada(self):
        crear_guia(self.pedido, "ETQ-1")
        crear_guia(self.pedido, "ETQ-2")
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.fulfillment_orders.return_value = [
                ("gid://shopify/FulfillmentOrder/1", "OPEN", "gid://shopify/Location/77"),
            ]
            self.assertTrue(marcar_fulfillment(self.pedido))

        api.crear_fulfillment.assert_called_once()
        fo_ids, numeros, url, carrier = api.crear_fulfillment.call_args.args
        self.assertEqual(fo_ids, ["gid://shopify/FulfillmentOrder/1"])
        self.assertEqual(numeros, ["ETQ-1", "ETQ-2"])
        self.assertIn("/r/", url)  # la página pública BRANDEADA, no la del carrier
        self.assertEqual(carrier, "estafeta")
        log = SyncLog.objects.latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_OK)
        self.assertIn("fulfillment", log.detalle)
        self.assertIn(self.pedido.folio, log.detalle)

    def test_sin_fulfillment_orders_abiertas_es_noop_idempotente(self):
        """Reintentos gratis: ya fulfilled (o retenido) = ok sin mutación."""
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.fulfillment_orders.return_value = [
                ("gid://shopify/FulfillmentOrder/1", "CLOSED", "gid://shopify/Location/77"),
            ]
            self.assertTrue(marcar_fulfillment(self.pedido))
        api.crear_fulfillment.assert_not_called()
        self.assertIn("sin fulfillment orders nuestras abiertas", SyncLog.objects.latest("ts").detalle)


class LocationScopedTests(BaseFulfillment):
    """Stage 1 multi-location: solo se cierran tickets de NUESTRA location."""

    LOC_NUESTRA = "gid://shopify/Location/77"
    LOC_AJENA = "gid://shopify/Location/99"

    def setUp(self):
        super().setUp()
        self.tienda.location_id = "77"
        self.tienda.save(update_fields=["location_id"])
        crear_guia(self.pedido, "ETQ-1")

    def _marcar(self, estados):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.location_gid = self.LOC_NUESTRA
            api.fulfillment_orders.return_value = estados
            resultado = marcar_fulfillment(self.pedido)
        return resultado, api

    def test_mixta_solo_cierra_las_nuestras(self):
        resultado, api = self._marcar([
            ("gid://shopify/FulfillmentOrder/1", "OPEN", self.LOC_NUESTRA),
            ("gid://shopify/FulfillmentOrder/2", "OPEN", self.LOC_AJENA),
        ])
        self.assertTrue(resultado)
        fo_ids = api.crear_fulfillment.call_args.args[0]
        self.assertEqual(fo_ids, ["gid://shopify/FulfillmentOrder/1"])
        self.assertIn("1 FO de otra location", SyncLog.objects.latest("ts").detalle)

    def test_todas_ajenas_es_noop_ok(self):
        resultado, api = self._marcar([
            ("gid://shopify/FulfillmentOrder/2", "OPEN", self.LOC_AJENA),
        ])
        self.assertTrue(resultado)  # nada nuestro que fulfillear: no-op idempotente
        api.crear_fulfillment.assert_not_called()
        log = SyncLog.objects.latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_OK)
        self.assertIn("no se tocan", log.detalle)

    def test_location_nula_cuenta_como_nuestra(self):
        # Location borrada en Shopify: comportamiento legado (se fulfillea).
        resultado, api = self._marcar([("gid://shopify/FulfillmentOrder/1", "OPEN", "")])
        self.assertTrue(resultado)
        api.crear_fulfillment.assert_called_once()

    def test_tienda_sin_location_id_no_filtra(self):
        self.tienda.location_id = ""
        self.tienda.save(update_fields=["location_id"])
        resultado, api = self._marcar([("gid://shopify/FulfillmentOrder/2", "OPEN", self.LOC_AJENA)])
        self.assertTrue(resultado)
        api.crear_fulfillment.assert_called_once()  # compat: sin config no hay filtro

    def test_error_de_shopify_queda_en_synclog_y_no_revienta(self):
        from apps.integraciones.shopify import ShopifyError

        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.fulfillment_orders.side_effect = ShopifyError("500 caído")
            self.assertFalse(marcar_fulfillment(self.pedido))  # jamás levanta
        log = SyncLog.objects.latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_ERROR)
        self.assertIn(self.pedido.folio, log.detalle)

    def test_pedido_manual_es_noop(self):
        self.pedido.tienda = None
        self.pedido.shopify_order_id = ""
        self.pedido.save(update_fields=["tienda", "shopify_order_id"])
        self.assertFalse(marcar_fulfillment(self.pedido))
        self.assertFalse(SyncLog.objects.exists())

    def test_sin_token_en_produccion_es_error_visible(self):
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        self.assertFalse(marcar_fulfillment(self.pedido))
        log = SyncLog.objects.latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_ERROR)
        self.assertIn("sin token", log.detalle)

    @override_settings(DEBUG=True)
    def test_sin_token_en_dev_es_mock(self):
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        self.assertTrue(marcar_fulfillment(self.pedido))
        self.assertIn("mock", SyncLog.objects.latest("ts").detalle)


class DisparadorEnManifiestoTests(BaseFulfillment):
    def test_marcar_recolectado_dispara_fulfillment_en_on_commit(self):
        """El write-back sale del momento canónico (manifiesto) y SOLO tras el
        commit — jamás dentro del atomic que despacha inventario."""
        from apps.pedidos.services import marcar_recolectado

        self.pedido.estado = "GUIA_GENERADA"
        self.pedido.save(update_fields=["estado"])
        crear_guia(self.pedido)

        with patch("apps.integraciones.services.marcar_fulfillment") as marcar, \
             patch("apps.mensajeria.services.enviar_en_camino"):
            with self.captureOnCommitCallbacks(execute=True):
                marcar_recolectado(self.pedido, None)
                marcar.assert_not_called()  # dentro de la transacción: nada
        marcar.assert_called_once_with(self.pedido)
