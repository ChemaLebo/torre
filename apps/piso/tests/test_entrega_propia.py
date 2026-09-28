"""Entregas locales (POD) sin flota propia (Chema 2026-09-28): un pedido que
salió con guía interna "local" (salida sin guía de carrier) se cierra ahí con
foto, receptor y mayoría de edad; los demás siguen sin ese carril (404)."""
from django.urls import reverse

from apps.envios.models import Guia
from apps.pedidos.models import Pedido

from .base import PisoTestCase


class EntregaPropiaSinFlotaTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.pedido = self.crear_pedido(cantidad=1, es_local=False, estado=Pedido.RECOLECTADO, reservar_stock=False)
        self.guia = Guia.objects.create(
            pedido=self.pedido, carrier="local", proveedor="local", numero=f"LOCAL-{self.pedido.folio}",
            estado=Guia.GUIA_CREADA,
        )
        self.otro = self.crear_pedido(cantidad=1, es_local=True, estado=Pedido.RECOLECTADO, reservar_stock=False)

    def test_salida_ofrece_el_pod_y_la_lista_trae_solo_los_de_guia_interna(self):
        self.assertContains(self.client.get(reverse("piso:salida")), reverse("piso:entrega_local"))
        respuesta = self.client.get(reverse("piso:entrega_local"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.pedido.folio)
        self.assertNotContains(respuesta, self.otro.folio)
        self.assertEqual(self.client.get(reverse("piso:entrega_local_pedido", args=[self.otro.pk])).status_code, 404)

    def test_el_pod_entrega_el_pedido_y_su_guia_interna(self):
        respuesta = self.client.post(
            reverse("piso:entrega_local_pedido", args=[self.pedido.pk]),
            {"receptor": "Dulce", "mayoria_edad": "si", "foto_pod": self.foto()}, follow=True,
        )
        self.assertContains(respuesta, "entregado a Dulce")
        self.pedido.refresh_from_db()
        self.guia.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.guia.estado), (Pedido.ENTREGADO, Guia.ENTREGADO))
