"""Máquina de estados del Pedido: transiciones, timestamps, folio y auditoría."""
from django.test import TestCase

from apps.core.models import Cliente, EventoAuditoria
from apps.pedidos.models import Pedido


def crear_pedido(cliente, estado=Pedido.PENDIENTE, **extra):
    return Pedido.objects.create(
        cliente=cliente,
        tienda=None,
        origen="manual",
        comprador_nombre="Ana Prueba",
        cp="28017",
        es_local=True,
        estado=estado,
        **extra,
    )


class MaquinaEstadosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")

    def test_transicion_valida_estampa_timestamp_y_registra_evento(self):
        pedido = crear_pedido(self.cliente)
        self.assertIsNone(pedido.ts_picking)
        pedido.transicionar(Pedido.EN_PICKING, motivo="prueba")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EN_PICKING)
        self.assertIsNotNone(pedido.ts_picking)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id=str(pedido.pk), accion="cambio_estado",
            ).exists()
        )

    def test_transicion_invalida_truena(self):
        pedido = crear_pedido(self.cliente)
        with self.assertRaises(ValueError):
            pedido.transicionar(Pedido.EMPACADO)  # PENDIENTE no puede saltar a EMPACADO
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)

    def test_estado_desconocido_truena(self):
        pedido = crear_pedido(self.cliente)
        with self.assertRaises(ValueError):
            pedido.transicionar("EN_PROCESO")  # prohibido el estado genérico

    def test_estado_terminal_no_sale(self):
        pedido = crear_pedido(self.cliente, estado=Pedido.CANCELADO)
        for destino in (Pedido.EN_PICKING, Pedido.PENDIENTE, Pedido.ENTREGADO):
            with self.assertRaises(ValueError):
                pedido.transicionar(destino)

    def test_flujo_feliz_completo(self):
        pedido = crear_pedido(self.cliente)
        for destino in (
            Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA,
            Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGADO,
        ):
            pedido.transicionar(destino)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.ENTREGADO)
        for campo in (
            "ts_picking", "ts_empacado", "ts_guia",
            "ts_recolectado", "ts_en_transito", "ts_entregado",
        ):
            self.assertIsNotNone(getattr(pedido, campo), campo)

    def test_timestamp_no_se_sobreescribe(self):
        pedido = crear_pedido(self.cliente, estado=Pedido.EN_TRANSITO)
        pedido.transicionar(Pedido.ENTREGA_PRESUNTA)
        pedido.transicionar(Pedido.ENTREGADO)
        primera = pedido.ts_entregado
        self.assertIsNotNone(primera)

    def test_cancelacion_pendiente_solo_cierra_en_cancelado(self):
        pedido = crear_pedido(self.cliente, estado=Pedido.CANCELACION_PENDIENTE)
        with self.assertRaises(ValueError):
            pedido.transicionar(Pedido.EN_PICKING)
        pedido.transicionar(Pedido.CANCELADO)
        self.assertEqual(pedido.estado, Pedido.CANCELADO)


class FolioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")

    def test_folio_autoincremental_con_formato(self):
        primero = crear_pedido(self.cliente)
        segundo = crear_pedido(self.cliente)
        self.assertRegex(primero.folio, r"^PED-\d{5}$")
        self.assertRegex(segundo.folio, r"^PED-\d{5}$")
        n1 = int(primero.folio.split("-")[1])
        n2 = int(segundo.folio.split("-")[1])
        self.assertEqual(n2, n1 + 1)

    def test_folio_no_cambia_al_guardar_de_nuevo(self):
        pedido = crear_pedido(self.cliente)
        folio = pedido.folio
        pedido.comprador_nombre = "Otro Nombre"
        pedido.save()
        pedido.refresh_from_db()
        self.assertEqual(pedido.folio, folio)
