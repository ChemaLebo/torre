"""Tests del push de inventario: encolado idempotente y drenado (modo mock)."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente
from apps.integraciones import services
from apps.integraciones.models import PushInventarioPendiente, SyncLog, Tienda
from apps.integraciones.shopify import ErrorItemNoStockeado, ShopifyClient, ShopifyError


def crear_sku(cliente, codigo="COLIMITA-SIX"):
    return SKU.objects.create(
        cliente=cliente,
        codigo=codigo,
        codigo_barras="7501031100016",
        descripcion="Colimita Lager six pack",
        peso_gr=2500,
        largo_cm=25,
        ancho_cm=17,
        alto_cm=24,
        unidad="six",
        precio_declarado=Decimal("155.00"),
        punto_reorden=24,
        activo=True,
    )


class EncolarPushTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.sku = crear_sku(self.cliente)

    def test_encola_una_sola_vez_por_sku(self):
        """Encolar N veces el mismo SKU deja exactamente UNA entrada pendiente."""
        services.encolar_push_inventario(self.sku)
        services.encolar_push_inventario(self.sku)
        services.encolar_push_inventario(self.sku)
        self.assertEqual(PushInventarioPendiente.objects.filter(sku=self.sku).count(), 1)

    def test_skus_distintos_encolan_por_separado(self):
        otro = crear_sku(self.cliente, codigo="PARAMO-SIX")
        services.encolar_push_inventario(self.sku)
        services.encolar_push_inventario(otro)
        self.assertEqual(PushInventarioPendiente.objects.count(), 2)


@override_settings(DEBUG=True)  # el modo mock (tienda sin token) solo existe en dev
class PushInventarioTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", buffer_stock=0)
        self.tienda_mx = Tienda.objects.create(cliente=self.cliente, dominio="colima-mx.myshopify.com")
        self.tienda_usa = Tienda.objects.create(cliente=self.cliente, dominio="colima-usa.myshopify.com")
        self.sku = crear_sku(self.cliente)

    def test_push_mock_drena_cola_y_registra_synclog_por_tienda(self):
        """Sin token: modo mock — SyncLog 'ok (mock)' en TODAS las tiendas del cliente."""
        services.encolar_push_inventario(self.sku)
        resumen = services.push_inventario()
        self.assertEqual(resumen["skus"], 1)
        self.assertEqual(resumen["pushes_ok"], 2)  # multi-tienda: mx + usa
        self.assertEqual(resumen["pushes_error"], 0)
        self.assertEqual(PushInventarioPendiente.objects.count(), 0)  # cola drenada
        for tienda in (self.tienda_mx, self.tienda_usa):
            log = tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_PUSH).latest("ts")
            self.assertEqual(log.resultado, SyncLog.RESULTADO_OK)
            self.assertIn("mock", log.detalle)
            self.assertIn(self.sku.codigo, log.detalle)

    def test_push_ignora_tiendas_inactivas(self):
        self.tienda_usa.activo = False
        self.tienda_usa.save()
        services.encolar_push_inventario(self.sku)
        resumen = services.push_inventario()
        self.assertEqual(resumen["pushes_ok"], 1)
        self.assertFalse(self.tienda_usa.sync_logs.exists())

    def test_push_con_cola_vacia_es_noop(self):
        resumen = services.push_inventario()
        self.assertEqual(resumen, {"skus": 0, "pushes_ok": 0, "pushes_error": 0})
        self.assertFalse(SyncLog.objects.exists())

    def test_push_descarta_los_kits_con_evento(self):
        from apps.core.models import EventoAuditoria

        kit = SKU.objects.create(
            cliente=self.cliente, codigo="TEABOX", descripcion="TeaBox", es_kit=True,
        )
        services.encolar_push_inventario(kit)
        resumen = services.push_inventario()
        # El kit se descarta ANTES de tocar tiendas: no cuenta ni ensucia SyncLog.
        self.assertEqual(resumen, {"skus": 0, "pushes_ok": 0, "pushes_error": 0})
        self.assertEqual(PushInventarioPendiente.objects.count(), 0)
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="sku", entidad_id="TEABOX", accion="push_omitido_kit",
        ).exists())

    def test_on_hand_nunca_es_negativo(self):
        """Sin saldos, con buffer del cliente: el on_hand publicado se acota en 0."""
        self.cliente.buffer_stock = 10
        self.cliente.save()
        self.sku.cliente.refresh_from_db()
        self.assertEqual(services.calcular_on_hand(self.sku), 0)


class AutoActivarInventarioTests(TestCase):
    """El push cura ITEM_NOT_STOCKED_AT_LOCATION: activa el item y reintenta."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", buffer_stock=0)
        self.tienda = Tienda.objects.create(
            cliente=self.cliente, dominio="colima-mx.myshopify.com",
            token="shpat_prueba", location_id="123",
        )
        self.sku = crear_sku(self.cliente)
        services.encolar_push_inventario(self.sku)

    def test_item_no_stockeado_activa_y_reintenta_con_compare_cero(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.consultar_inventario_sku.return_value = ("gid://shopify/InventoryItem/1", 7)
            api.set_on_hand.side_effect = [ErrorItemNoStockeado("no stockeado"), {}]
            resumen = services.push_inventario()
        self.assertEqual(resumen["pushes_ok"], 1)
        api.activar_inventario.assert_called_once_with("gid://shopify/InventoryItem/1")
        self.assertEqual(api.set_on_hand.call_count, 2)
        self.assertEqual(api.set_on_hand.call_args_list[1].kwargs["compare_quantity"], 0)
        self.assertEqual(PushInventarioPendiente.objects.count(), 0)
        log = SyncLog.objects.latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_OK)
        self.assertIn("activado", log.detalle)

    def test_activacion_fallida_deja_error_y_el_pendiente_sobrevive(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.consultar_inventario_sku.return_value = ("gid://shopify/InventoryItem/1", 0)
            api.set_on_hand.side_effect = ErrorItemNoStockeado("no stockeado")
            api.activar_inventario.side_effect = ShopifyError("activate falló")
            resumen = services.push_inventario()
        self.assertEqual(resumen["pushes_error"], 1)
        self.assertEqual(PushInventarioPendiente.objects.count(), 1)
        self.assertEqual(SyncLog.objects.latest("ts").resultado, SyncLog.RESULTADO_ERROR)

    def test_error_generico_no_intenta_activar(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.consultar_inventario_sku.return_value = ("gid://shopify/InventoryItem/1", 3)
            api.set_on_hand.side_effect = ShopifyError("compare viejo")
            resumen = services.push_inventario()
        self.assertEqual(resumen["pushes_error"], 1)
        api.activar_inventario.assert_not_called()


class SetOnHandClasificaErroresTests(TestCase):
    """El código ITEM_NOT_STOCKED_AT_LOCATION se distingue del resto de userErrors."""

    def _api(self):
        tienda = Tienda(dominio="x.myshopify.com", token="shpat_x", location_id="1")
        return ShopifyClient(tienda)

    def test_item_not_stocked_lanza_su_excepcion(self):
        api = self._api()
        respuesta = {"inventorySetQuantities": {"userErrors": [
            {"code": "ITEM_NOT_STOCKED_AT_LOCATION", "field": None, "message": "not stocked"},
        ]}}
        with patch.object(api, "graphql", return_value=respuesta):
            with self.assertRaises(ErrorItemNoStockeado):
                api.set_on_hand("gid://shopify/InventoryItem/1", 5, 0)

    def test_otro_user_error_lanza_shopify_error_plano(self):
        api = self._api()
        respuesta = {"inventorySetQuantities": {"userErrors": [
            {"code": "INVALID", "field": None, "message": "otra cosa"},
        ]}}
        with patch.object(api, "graphql", return_value=respuesta):
            with self.assertRaises(ShopifyError) as ctx:
                api.set_on_hand("gid://shopify/InventoryItem/1", 5, 0)
        self.assertNotIsInstance(ctx.exception, ErrorItemNoStockeado)


@override_settings(DEBUG=True)  # el modo mock (tienda sin token) solo existe en dev
class ReconciliarPedidosTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="colima-mx.myshopify.com")

    def test_reconciliar_sin_token_es_mock_y_avanza_checkpoint(self):
        antes = timezone.now()
        nuevos = services.reconciliar_pedidos(self.tienda)
        self.assertEqual(nuevos, 0)
        self.tienda.refresh_from_db()
        self.assertIsNotNone(self.tienda.checkpoint_reconciliacion)
        self.assertGreaterEqual(self.tienda.checkpoint_reconciliacion, antes)
        log = self.tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_INGESTA).latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_OK)
        self.assertIn("mock", log.detalle)


class BackfillPrimerSyncTests(TestCase):
    """Checkpoint nulo = backfill acotado (90d, pagadas, sin fulfillear);
    checkpoint puesto = sync recurrente por updated_at, sin acotar."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.tienda = Tienda.objects.create(
            cliente=self.cliente, dominio="colima-mx.myshopify.com",
            token="shpat_prueba", location_id="",
        )

    def test_primer_sync_usa_backfill_acotado(self):
        antes = timezone.now()
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.listar_pedidos_backfill.return_value = [
                {"id": 4444, "updated_at": "2026-08-20T10:00:00-06:00",
                 "financial_status": "paid", "line_items": []},
            ]
            with patch("apps.pedidos.services.ingerir_pedido_shopify", return_value=None):
                nuevos = services.reconciliar_pedidos(self.tienda)
        self.assertEqual(nuevos, 1)
        api.listar_pedidos.assert_not_called()
        desde = api.listar_pedidos_backfill.call_args.kwargs["created_at_min"]
        esperado = antes - timedelta(days=90)
        self.assertLess(abs(desde - esperado), timedelta(seconds=10))
        self.tienda.refresh_from_db()
        self.assertIsNotNone(self.tienda.checkpoint_reconciliacion)
        log = self.tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_INGESTA).latest("ts")
        self.assertIn("backfill inicial (90d)", log.detalle)

    def test_sync_recurrente_usa_updated_at_sin_acotar(self):
        checkpoint = timezone.now()
        self.tienda.checkpoint_reconciliacion = checkpoint
        self.tienda.save(update_fields=["checkpoint_reconciliacion"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.listar_pedidos.return_value = []
            services.reconciliar_pedidos(self.tienda)
        api.listar_pedidos_backfill.assert_not_called()
        api.listar_pedidos.assert_called_once_with(updated_at_min=checkpoint)


class SinTokenEnProduccionTests(TestCase):
    """Fail-closed con DEBUG=0 (default en tests): una tienda sin token jamás
    simula éxito — error visible, cola intacta, checkpoint intacto."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="colima-mx.myshopify.com")
        self.sku = crear_sku(self.cliente)

    def test_push_no_drena_la_cola_y_deja_error_visible(self):
        services.encolar_push_inventario(self.sku)
        resumen = services.push_inventario()
        self.assertEqual(resumen["pushes_ok"], 0)
        self.assertEqual(resumen["pushes_error"], 1)
        # El pendiente SOBREVIVE: el cambio de stock no se descarta en silencio.
        self.assertEqual(PushInventarioPendiente.objects.count(), 1)
        log = self.tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_PUSH).latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_ERROR)
        self.assertIn("sin token", log.detalle)

    def test_reconciliar_no_avanza_checkpoint_y_deja_error(self):
        nuevos = services.reconciliar_pedidos(self.tienda)
        self.assertEqual(nuevos, 0)
        self.tienda.refresh_from_db()
        # La ventana de reconciliación queda intacta para cuando haya token.
        self.assertIsNone(self.tienda.checkpoint_reconciliacion)
        log = self.tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_INGESTA).latest("ts")
        self.assertEqual(log.resultado, SyncLog.RESULTADO_ERROR)
        self.assertIn("sin token", log.detalle)
