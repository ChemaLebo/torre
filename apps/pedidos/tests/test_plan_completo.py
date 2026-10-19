"""Red al iniciar picking (Chema 2026-10-19, PED-00606): si alguna unidad de
la ola no va en ninguna caja (una línea que entró después del plan), EMPEZAR
completa el plan antes de que el piso lo use; con el plan completo no toca
nada."""
from decimal import Decimal

from django.test import override_settings

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.cotizador import planificar_envio
from apps.pedidos.models import LineaPedido, Pedido
from apps.pedidos.services import iniciar_picking
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class PlanCompletoAlIniciarPickingTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.otro = SKU.objects.create(
            cliente=self.cliente, codigo="OTRO-12", codigo_barras="7500000000012", descripcion="Otro doce",
            peso_gr=7000, requiere_lote=False, precio_declarado=Decimal("500.00"),
        )

    def _cubiertas(self, pedido):
        return sum(pl.cantidad for c in pedido.paquetes.all() for pl in c.lineas.all())

    def test_empezar_completa_el_plan_si_una_linea_no_tiene_caja(self):
        pedido = self.crear_pedido(cantidad=2)
        planificar_envio(pedido)
        self.assertEqual(self._cubiertas(pedido), 2)
        LineaPedido.objects.create(pedido=pedido, sku=self.otro, cantidad=1, reservada=True)  # entró después del plan
        with self.captureOnCommitCallbacks(execute=True):
            iniciar_picking(pedido, self.operador)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EN_PICKING)
        self.assertEqual(self._cubiertas(pedido), 3)
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="plan_incompleto")
        self.assertEqual(evento.delta, {"sin_caja": ["OTRO-12"], "unidades": 1})

    def test_con_el_plan_completo_empezar_no_toca_las_cajas(self):
        pedido = self.crear_pedido(cantidad=2)
        cajas = [c.pk for c in planificar_envio(pedido)]
        with self.captureOnCommitCallbacks(execute=True):
            iniciar_picking(pedido, self.operador)
        self.assertEqual([c.pk for c in pedido.paquetes.order_by("numero")], cajas)
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="plan_incompleto").exists())
