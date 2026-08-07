"""Picking en piso: iniciar la ola y escanear línea por línea contra el SKU real."""
from django.urls import reverse

from apps.pedidos.models import Pedido

from .base import PisoTestCase


class PickingPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=3)
        self.url_detalle = reverse("piso:picking_pedido", args=[self.pedido.pk])

    def _iniciar(self):
        self.client.post(reverse("piso:picking"), {
            "accion": "iniciar", "pedido_id": self.pedido.pk,
        })
        self.pedido.refresh_from_db()

    def test_iniciar_picking_transiciona_y_estampa_ts(self):
        self._iniciar()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertIsNotNone(self.pedido.ts_picking)

    def test_iniciar_pedido_ya_tomado_avisa_la_carrera(self):
        # Dos tablets con la misma lista: el segundo POST de "iniciar" llega
        # cuando el pedido ya está EN_PICKING — la vista valida bajo lock y
        # avisa sin drama (jamás doble inicio ni transición inválida).
        self._iniciar()
        respuesta = self.client.post(reverse("piso:picking"), {
            "accion": "iniciar", "pedido_id": self.pedido.pk,
        }, follow=True)
        self.assertContains(respuesta, "Otro operador ya tomó")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    def test_escaneo_equivocado_no_pickea_y_avisa(self):
        self._iniciar()
        respuesta = self.client.post(self.url_detalle, {
            "codigo": "0000000000000", "cantidad": "1",
        }, follow=True)
        self.assertContains(respuesta, "Código equivocado")
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 0)

    def test_escaneo_correcto_por_codigo_de_barras(self):
        self._iniciar()
        self.client.post(self.url_detalle, {
            "codigo": self.sku.codigo_barras, "cantidad": "2",
        }, follow=True)
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 2)

    def test_completar_pedido_manda_a_empaque(self):
        self._iniciar()
        self.client.post(self.url_detalle, {"codigo": self.sku.codigo_barras, "cantidad": "2"})
        respuesta = self.client.post(self.url_detalle, {
            "codigo": self.sku.codigo, "cantidad": "1",  # el código de SKU también vale
        })
        self.assertRedirects(
            respuesta, reverse("piso:empaque_pedido", args=[self.pedido.pk]),
            fetch_redirect_response=False,
        )
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 3)

    def test_no_se_puede_pickear_de_mas(self):
        self._iniciar()
        self.client.post(self.url_detalle, {"codigo": self.sku.codigo_barras, "cantidad": "3"})
        respuesta = self.client.post(self.url_detalle, {
            "codigo": self.sku.codigo_barras, "cantidad": "1",
        }, follow=True)
        self.assertContains(respuesta, "ya está completa")
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 3)

    def test_detalle_de_pedido_no_en_picking_redirige(self):
        respuesta = self.client.get(self.url_detalle)  # sigue PENDIENTE
        self.assertRedirects(respuesta, reverse("piso:picking"), fetch_redirect_response=False)
