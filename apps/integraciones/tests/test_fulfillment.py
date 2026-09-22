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
        marcar.assert_called_once_with(self.pedido, notificar=True)  # primer manifiesto de la ola


class FulfillmentPorCajaTests(BaseFulfillment):
    """Un fulfillment POR CAJA al salir cada una (Partially fulfilled entre
    manifiestos), con sus líneas, su guía y su id guardado; solo el primero
    notifica; tras crearlo viaja CARRIER_PICKED_UP."""

    FO = "gid://shopify/FulfillmentOrder/1"
    LINEA_FO = "gid://shopify/FulfillmentOrderLineItem/10"

    def setUp(self):
        super().setUp()
        from decimal import Decimal

        from apps.catalogo.models import SKU
        from apps.envios.models import Paquete, PaqueteLinea
        from apps.pedidos.models import LineaPedido

        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=2000)
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=3, cantidad_pickeada=3)
        self.c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal(4), carrier="estafeta", estado="EMPACADO")
        self.c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal(2), carrier="estafeta", estado="EMPACADO")
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=self.linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=self.c2, linea_pedido=self.linea, cantidad=1)
        self.g1 = Guia.objects.create(pedido=self.pedido, paquete=self.c1, carrier="estafeta", numero="ETQ-1")
        self.g2 = Guia.objects.create(pedido=self.pedido, paquete=self.c2, carrier="estafeta", numero="ETQ-2")

    def _api(self, cliente_cls, restante=3, ids=("gid://shopify/Fulfillment/A", "gid://shopify/Fulfillment/B")):
        api = cliente_cls.return_value
        api.fulfillment_orders_lineas.return_value = [{
            "gid": self.FO, "status": "OPEN", "location_gid": "",
            "lineas": [{"line_item_id": "5", "fo_line_item_id": self.LINEA_FO, "sku": "SIX", "cantidad": restante}],
        }]
        api.crear_fulfillment.side_effect = [{"id": i, "status": "SUCCESS"} for i in ids]
        return api

    def test_sale_la_caja_1_y_luego_la_2(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c1]))
        api.crear_fulfillment.assert_called_once()
        fo_ids, numeros, url, carrier = api.crear_fulfillment.call_args.args
        self.assertEqual((fo_ids, numeros, carrier), ([self.FO], ["ETQ-1"], "estafeta"))
        self.assertIn("/r/", url)
        kwargs = api.crear_fulfillment.call_args.kwargs
        self.assertTrue(kwargs["notificar"])
        self.assertEqual(kwargs["lineas"], [
            {"fulfillmentOrderId": self.FO, "fulfillmentOrderLineItems": [{"id": self.LINEA_FO, "quantity": 2}]},
        ])
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")
        api.crear_evento_fulfillment.assert_called_once()
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/A", "CARRIER_PICKED_UP"))

        # Segundo manifiesto: la caja 2 con lo que resta, sin segundo correo.
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls, restante=1, ids=("gid://shopify/Fulfillment/B",))
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2]))
        kwargs = api.crear_fulfillment.call_args.kwargs
        self.assertFalse(kwargs["notificar"])
        self.assertEqual(kwargs["lineas"][0]["fulfillmentOrderLineItems"], [{"id": self.LINEA_FO, "quantity": 1}])
        self.assertEqual(api.crear_fulfillment.call_args.args[1], ["ETQ-2"])
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.shopify_fulfillment_id, "gid://shopify/Fulfillment/B")
        # Reintento: la caja ya tiene id, nada que escribir.
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2]))
        api.crear_fulfillment.assert_not_called()

    def test_lineas_no_separables_espera_a_la_ultima_caja_y_fulfillea_entero(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            api.fulfillment_orders_lineas.return_value[0]["lineas"][0]["sku"] = "OTRO-SKU"
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c1]))
        api.crear_fulfillment.assert_not_called()
        self.assertIn("espera a la última caja", SyncLog.objects.latest("ts").detalle)

        self.c1.estado = "DESPACHADO"
        self.c1.save(update_fields=["estado"])
        self.c2.estado = "DESPACHADO"
        self.c2.save(update_fields=["estado"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            api.fulfillment_orders_lineas.return_value[0]["lineas"][0]["sku"] = "OTRO-SKU"
            api.fulfillment_orders.return_value = [(self.FO, "OPEN", "")]
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2]))
        fo_ids, numeros, _url, _carrier = api.crear_fulfillment.call_args.args
        self.assertEqual((fo_ids, numeros), ([self.FO], ["ETQ-1", "ETQ-2"]))
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")

    def test_media_caja_comparte_el_fulfillment_de_su_hermana(self):
        from apps.envios.models import PaqueteLinea

        PaqueteLinea.objects.filter(paquete__in=[self.c1, self.c2]).update(cantidad=1, fraccion_de=2)
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls, restante=1)
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c1, self.c2]))
        api.crear_fulfillment.assert_called_once()  # la unidad viaja con la primera media
        self.c1.refresh_from_db()
        self.c2.refresh_from_db()
        self.assertEqual(self.c1.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")
        self.assertEqual(self.c2.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")

    def test_pedido_entero_guarda_su_id_y_manda_el_evento_inicial(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.fulfillment_orders.return_value = [(self.FO, "OPEN", "")]
            api.crear_fulfillment.return_value = {"id": "gid://shopify/Fulfillment/Z", "status": "SUCCESS"}
            self.assertTrue(marcar_fulfillment(self.pedido, evento_inicial="DELIVERED"))
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.shopify_fulfillment_id, "gid://shopify/Fulfillment/Z")
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/Z", "DELIVERED"))
        # Segunda llamada: ya tiene id, no se duplica.
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            self.assertTrue(marcar_fulfillment(self.pedido))
        cliente_cls.return_value.crear_fulfillment.assert_not_called()


class NotificarPorOlaTests(FulfillmentPorCajaTests):
    """Fulfillment parcial: la segunda ola (días después, con su propia guía)
    sí notifica al comprador aunque el pedido ya tenga un fulfillment. El
    caller (marcar_recolectado) manda `notificar` en el primer manifiesto de
    cada ola; None conserva la regla de "solo el primer fulfillment"."""

    def test_segunda_ola_notifica_cuando_el_manifiesto_lo_pide(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            marcar_fulfillment(self.pedido, cajas=[self.c1])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls, restante=1, ids=("gid://shopify/Fulfillment/B",))
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c2], notificar=True))
        self.assertTrue(api.crear_fulfillment.call_args.kwargs["notificar"])

    def test_notificar_false_calla_aunque_sea_el_primero(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = self._api(cliente_cls)
            self.assertTrue(marcar_fulfillment(self.pedido, cajas=[self.c1], notificar=False))
        self.assertFalse(api.crear_fulfillment.call_args.kwargs["notificar"])


class EventoFulfillmentTests(BaseFulfillment):
    """registrar_evento_fulfillment: el avance de la guía cuelga del fulfillment
    de su caja (o del pedido entero), con el mapa de estados y la hora real."""

    def setUp(self):
        super().setUp()
        from decimal import Decimal

        from apps.envios.models import Paquete

        self.c1 = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal(4), carrier="estafeta",
            estado="DESPACHADO", shopify_fulfillment_id="gid://shopify/Fulfillment/A",
        )
        self.g1 = Guia.objects.create(pedido=self.pedido, paquete=self.c1, carrier="estafeta", numero="ETQ-1")

    def _evento(self, guia, estado, **kw):
        from apps.integraciones.services import registrar_evento_fulfillment

        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            resultado = registrar_evento_fulfillment(self.pedido, guia, estado, **kw)
        return resultado, api

    def test_cuelga_del_fulfillment_de_la_caja_con_hora_y_mensaje(self):
        from django.utils import timezone

        ts = timezone.now()
        resultado, api = self._evento(self.g1, "EN_RUTA", descripcion="Salió a reparto", ts=ts)
        self.assertTrue(resultado)
        api.crear_evento_fulfillment.assert_called_once_with(
            "gid://shopify/Fulfillment/A", "OUT_FOR_DELIVERY", happened_at=ts, message="Salió a reparto",
        )
        self.assertIn("OUT_FOR_DELIVERY", SyncLog.objects.latest("ts").detalle)

    def test_estado_sin_equivalente_no_viaja(self):
        resultado, api = self._evento(self.g1, "GUIA_CREADA")
        self.assertFalse(resultado)
        api.crear_evento_fulfillment.assert_not_called()

    def test_sin_id_queda_en_synclog(self):
        self.c1.shopify_fulfillment_id = ""
        self.c1.save(update_fields=["shopify_fulfillment_id"])
        resultado, api = self._evento(self.g1, "EN_TRANSITO")
        self.assertFalse(resultado)
        api.crear_evento_fulfillment.assert_not_called()
        log = SyncLog.objects.latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_ERROR)
        self.assertIn("shopify_eventos_backfill", log.detalle)

    def test_delivered_del_pedido_entero_espera_a_todas_las_guias(self):
        self.c1.shopify_fulfillment_id = ""
        self.c1.save(update_fields=["shopify_fulfillment_id"])
        self.pedido.shopify_fulfillment_id = "gid://shopify/Fulfillment/P"
        self.pedido.save(update_fields=["shopify_fulfillment_id"])
        g2 = Guia.objects.create(pedido=self.pedido, carrier="estafeta", numero="ETQ-2")
        self.g1.estado = Guia.ENTREGADO
        self.g1.save(update_fields=["estado"])
        resultado, api = self._evento(self.g1, "ENTREGADO")
        self.assertFalse(resultado)  # la otra guía sigue en camino
        api.crear_evento_fulfillment.assert_not_called()
        g2.estado = Guia.ENTREGADO
        g2.save(update_fields=["estado"])
        resultado, api = self._evento(g2, "ENTREGADO")
        self.assertTrue(resultado)
        self.assertEqual(api.crear_evento_fulfillment.call_args.args[:2], ("gid://shopify/Fulfillment/P", "DELIVERED"))

    def test_error_de_shopify_no_levanta(self):
        from apps.integraciones.services import registrar_evento_fulfillment
        from apps.integraciones.shopify import ShopifyError

        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.crear_evento_fulfillment.side_effect = ShopifyError("500")
            self.assertFalse(registrar_evento_fulfillment(self.pedido, self.g1, "ENTREGADO"))
        self.assertEqual(SyncLog.objects.latest("ts").resultado, SyncLog.RESULTADO_ERROR)


class BackfillEventosTests(BaseFulfillment):
    def test_recupera_ids_por_numero_de_guia_y_reproduce_el_historial(self):
        from decimal import Decimal
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        from apps.envios.models import EventoGuia, Paquete

        self.pedido.estado = "EN_TRANSITO"
        self.pedido.ts_recolectado = timezone.now()
        self.pedido.save(update_fields=["estado", "ts_recolectado"])
        c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal(4), carrier="estafeta", estado="DESPACHADO")
        g1 = Guia.objects.create(pedido=self.pedido, paquete=c1, carrier="estafeta", numero="ETQ-1", estado=Guia.EN_TRANSITO)
        EventoGuia.objects.create(guia=g1, estado="EN_TRANSITO", crudo="IT", descripcion="En camino", ts_carrier=timezone.now())

        with patch("apps.integraciones.management.commands.shopify_eventos_backfill.ShopifyClient") as cliente_cls, \
             patch("apps.integraciones.services.ShopifyClient") as servicio_cls:
            cliente_cls.return_value.fulfillments_de_orden.return_value = [
                ("gid://shopify/Fulfillment/A", "SUCCESS", ["ETQ-1"]),
            ]
            salida = StringIO()
            call_command("shopify_eventos_backfill", "--folio", self.pedido.folio, stdout=salida)
            c1.refresh_from_db()
            self.assertEqual(c1.shopify_fulfillment_id, "")  # simulación
            self.assertIn("CARRIER_PICKED_UP", salida.getvalue())
            self.assertIn("IN_TRANSIT", salida.getvalue())

            salida = StringIO()
            call_command("shopify_eventos_backfill", "--folio", self.pedido.folio, "--aplicar", stdout=salida)
        c1.refresh_from_db()
        self.assertEqual(c1.shopify_fulfillment_id, "gid://shopify/Fulfillment/A")
        estados = [c.args[1] for c in servicio_cls.return_value.crear_evento_fulfillment.call_args_list]
        self.assertEqual(estados, ["CARRIER_PICKED_UP", "IN_TRANSIT"])
        self.assertIn("2 evento(s) mandados", salida.getvalue())
        # Reentrable: ya con id, se salta.
        with patch("apps.integraciones.management.commands.shopify_eventos_backfill.ShopifyClient"), \
             patch("apps.integraciones.services.ShopifyClient") as servicio_cls:
            salida = StringIO()
            call_command("shopify_eventos_backfill", "--folio", self.pedido.folio, "--aplicar", stdout=salida)
        self.assertIn("ya tiene id", salida.getvalue())
        servicio_cls.return_value.crear_evento_fulfillment.assert_not_called()
