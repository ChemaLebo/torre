"""Mesa → Pedidos → "Cambiar paquetería" (Chema 2026-09-28): selector por
renglón antes de salir, con la salida sin guía de carrier ("local")."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class CambiarPaqueteriaMesaTests(PisoTestCase):
    def setUp(self):
        self.crear_stock(cantidad=20)
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.url = reverse("mesa:pedidos")

    def test_selector_y_accion_fuerzan_la_salida_sin_guia(self):
        pedido = self.crear_pedido(cantidad=2, es_local=False)
        html = self.client.get(self.url).content.decode()
        self.assertIn('value="cambiar_paqueteria"', html)
        self.assertIn("Sin guía: entrega propia o la recoge el cliente", html)
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(self.url, {"accion": "cambiar_paqueteria", "folio": pedido.folio, "carrier": "local"}, follow=True)
        self.assertContains(respuesta, "Sin guía: entrega propia o la recoge el cliente")
        pedido.refresh_from_db()
        self.assertEqual(pedido.carrier_forzado, "local")
        self.assertEqual({c.carrier for c in pedido.paquetes.all()}, {"local"})

    def test_con_algo_en_la_calle_no_hay_selector_y_la_accion_avisa(self):
        pedido = self.crear_pedido(cantidad=1, es_local=False, estado=Pedido.EN_TRANSITO, reservar_stock=False)
        self.assertNotIn('value="cambiar_paqueteria"', self.client.get(self.url).content.decode())
        respuesta = self.client.post(self.url, {"accion": "cambiar_paqueteria", "folio": pedido.folio, "carrier": "local"}, follow=True)
        self.assertContains(respuesta, "solo se cambia antes de salir")
