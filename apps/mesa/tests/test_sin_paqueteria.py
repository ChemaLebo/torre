"""Mesa y la incidencia interna "Sin paquetería que cotice" (Chema
2026-09-24): pill interna, selector de paquetería que replanea el pedido, y el
tag con link en la lista de pedidos."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_sin_paqueteria
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase

POOL_CLASICO = {**settings.TORRE, "CARRIERS_COTIZAR": ["estafeta", "fedex"], "CARRIER_PRIORITARIO": ""}


@override_settings(ENVIA_API_KEY="", TORRE=POOL_CLASICO)
class SinPaqueteriaMesaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        self.inc = abrir_sin_paqueteria(self.pedido, "Ningún carrier cotiza el pedido a CP 44100 con paquetes ≤20 kg.")
        self.url = reverse("mesa:incidencia_detalle", args=[self.inc.pk])

    def test_detalle_con_pill_interna_y_selector(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Interna · bodega", html)
        self.assertIn('name="accion" value="replanear_carrier"', html)
        for etiqueta in ("99minutos directo", "iMile (vía envia.com)", "envia.com: el más barato de su lista", "estafeta (vía envia.com)"):
            self.assertIn(etiqueta, html)
        lista = self.client.get(reverse("mesa:incidencias")).content.decode()
        self.assertIn(">interna</span>", lista)
        pedidos = self.client.get(reverse("mesa:pedidos")).content.decode()
        self.assertIn(f"Sin paquetería · {self.inc.folio}", pedidos)

    def test_elegir_paqueteria_replanea_y_cierra_la_incidencia(self):
        respuesta = self.client.post(self.url, {"accion": "replanear_carrier", "carrier": "fedex"}, follow=True)
        self.assertContains(respuesta, "con fedex (vía envia.com): 1 caja planeadas")
        self.inc.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual((self.inc.estado, self.pedido.carrier_forzado, self.pedido.estado), (Incidencia.CERRADA, "fedex", Pedido.EMPACADO))
        self.assertEqual(self.pedido.paquetes.get().carrier, "fedex")
        self.assertNotIn("replanear_carrier", self.client.get(self.url).content.decode())  # cerrada: sin selector

    def test_paqueteria_que_tampoco_cotiza_deja_error_y_sigue_abierta(self):
        with override_settings(TORRE={**POOL_CLASICO, "CARRIERS_COTIZAR": ["fantasma"]}):
            respuesta = self.client.post(self.url, {"accion": "replanear_carrier", "carrier": "fantasma"}, follow=True)
        self.assertContains(respuesta, "tampoco cotiza")
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.estado, Incidencia.ABIERTA)
