"""Modal de confirmación tras cada escaneo de picking (Chema 2026-10-13): la
pantalla trae el modal y los datos que lo llenan; el JSON del escaneo trae
caja N de M. Mientras el modal está abierto el lector se descarta (JS)."""
from django.urls import reverse

from apps.pedidos.services import iniciar_picking

from .base import PisoTestCase


class ModalPickingTests(PisoTestCase):
    def test_la_pantalla_trae_el_modal_y_el_json_lo_que_lo_llena(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        pedido = self.crear_pedido(cantidad=3)
        iniciar_picking(pedido, self.operador)
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        r = self.client.get(url)
        self.assertContains(r, 'id="modal-pick"')
        self.assertContains(r, 'id="pick-confirmar"')
        self.assertContains(r, f'data-descripcion="{self.sku.descripcion}"')
        self.assertContains(r, "data-ubicacion=")
        r = self.client.post(url, {"codigo": self.sku.codigo_barras or self.sku.codigo, "cantidad": "1"}, HTTP_ACCEPT="application/json")
        datos = r.json()
        self.assertEqual((datos["ok"], datos["sku"], datos["pickeada"], datos["cantidad"], datos["completo"]), (True, self.sku.codigo, 1, 3, False))
