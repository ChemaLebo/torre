"""Home del piso: resumen de tareas del día y cierre de restocks de cancelación."""
from django.urls import reverse

from apps.inventario.models import TareaConteo
from apps.pedidos.models import Pedido

from .base import PisoTestCase


class HomePisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.url = reverse("piso:home")

    def test_sin_tareas_todo_al_dia(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Todo al día")

    def test_resumen_cuenta_las_tareas_del_dia(self):
        self.crear_stock(cantidad=50)
        self.crear_pedido(cantidad=2)  # PENDIENTE → cuenta como "por pickear"
        TareaConteo.objects.create(sku=self.sku)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "por pickear")
        self.assertContains(respuesta, "COLIMITA-SIX")

    def test_confirmar_restock_cierra_la_cancelacion(self):
        from apps.inventario.services import disponible
        from apps.pedidos.services import cancelar, iniciar_picking

        self.crear_stock(cantidad=50)
        pedido = self.crear_pedido(cantidad=4)
        iniciar_picking(pedido, self.operador)
        cancelar(pedido, self.operador, motivo="Cambio de opinión del comprador")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELACION_PENDIENTE)
        self.assertEqual(disponible(self.sku), 46)  # la reserva sigue viva

        respuesta = self.client.post(self.url, {
            "accion": "confirmar_restock", "pedido_id": pedido.pk,
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertEqual(disponible(self.sku), 50)  # todo de vuelta al anaquel

    def test_confirmar_restock_de_pedido_sin_cancelacion_avisa(self):
        self.crear_stock(cantidad=50)
        pedido = self.crear_pedido(cantidad=1)
        respuesta = self.client.post(self.url, {
            "accion": "confirmar_restock", "pedido_id": pedido.pk,
        }, follow=True)
        self.assertContains(respuesta, "no tiene cancelación pendiente")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
