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

    def test_lista_de_mesa_busca_por_numero_de_guia(self):
        # Chema 2026-10-05: folio, comprador, orden de Shopify o # de guía (viva o cancelada).
        from apps.envios.models import Guia

        Guia.objects.create(pedido=self.pedido, carrier="estafeta", numero="005870980061070999ARYW", proveedor="mock")
        Guia.objects.create(pedido=self.pedido, carrier="imile", numero="6092226496868", proveedor="mock", estado=Guia.CANCELADA)
        for q in ("005870980061070999ARYW", "0610709", "6092226496868"):
            html = self.client.get(reverse("mesa:pedidos"), {"q": q}).content.decode()
            self.assertIn(self.pedido.folio, html)
            self.assertEqual(html.count(f">{self.pedido.folio}<"), self.client.get(reverse("mesa:pedidos")).content.decode().count(f">{self.pedido.folio}<"))  # sin filas duplicadas
        self.assertNotIn(self.pedido.folio, self.client.get(reverse("mesa:pedidos"), {"q": "1111111111"}).content.decode())
        self.assertIn("# de guía", self.client.get(reverse("mesa:pedidos")).content.decode())

    def test_rastreo_publico_muestra_la_orden(self):
        self.client.logout()
        html = self.client.get(f"/r/{obtener_o_crear_token(self.pedido)}/").content.decode()
        self.assertIn(f"PEDIDO {self.pedido.folio} · ORDEN #4074", html)
