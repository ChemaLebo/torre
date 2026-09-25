"""Nombre de la orden de Shopify en Mesa y en la página de rastreo (Chema
2026-09-25): se muestra y se busca por "#4074"; el link al admin usa el id."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.rastreo.services import obtener_o_crear_token


class NombreOrdenTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, shopify_order_id="8398059995298", shopify_order_name="#4074")
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_lista_de_mesa_muestra_y_busca_por_nombre(self):
        html = self.client.get(reverse("mesa:pedidos")).content.decode()
        self.assertIn("#4074 ↗", html)
        self.assertIn("/admin/orders/8398059995298", html)
        self.assertIn(self.pedido.folio, self.client.get(reverse("mesa:pedidos"), {"q": "#4074"}).content.decode())
        self.assertNotIn(self.pedido.folio, self.client.get(reverse("mesa:pedidos"), {"q": "#9999"}).content.decode())

    def test_rastreo_publico_muestra_la_orden(self):
        self.client.logout()
        html = self.client.get(f"/r/{obtener_o_crear_token(self.pedido)}/").content.decode()
        self.assertIn(f"PEDIDO {self.pedido.folio} · ORDEN #4074", html)
