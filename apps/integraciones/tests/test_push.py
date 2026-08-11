"""Tests del push de inventario: encolado idempotente y drenado (modo mock)."""
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente
from apps.integraciones import services
from apps.integraciones.models import PushInventarioPendiente, SyncLog, Tienda


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

    def test_on_hand_nunca_es_negativo(self):
        """Sin saldos, con buffer del cliente: el on_hand publicado se acota en 0."""
        self.cliente.buffer_stock = 10
        self.cliente.save()
        self.sku.cliente.refresh_from_db()
        self.assertEqual(services.calcular_on_hand(self.sku), 0)


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
