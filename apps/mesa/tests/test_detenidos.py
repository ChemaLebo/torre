"""Detenidos en piso en Mesa (Chema 2026-10-06): tarjeta en Torre de control,
pill en Pedidos, y resolver la DET desde la incidencia reanuda el pedido."""
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.pedidos.models import Pedido
from apps.pedidos.services import detener_pedido, iniciar_picking
from apps.piso.tests.base import PisoTestCase


class DetenidosEnMesaTests(PisoTestCase):
    def setUp(self):
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=1)
        iniciar_picking(self.pedido, self.operador)
        self.det = detener_pedido(self.pedido, self.operador, "Producto dañado: el six viene roto")
        mesa = get_user_model().objects.create_user("mesa-det", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_tarjeta_pill_y_resolver_reanuda(self):
        respuesta = self.client.get(reverse("mesa:dashboard"))
        self.assertContains(respuesta, "Detenidos en piso")
        self.assertContains(respuesta, self.pedido.folio)
        self.assertContains(respuesta, "Producto dañado: el six viene roto")
        self.assertContains(respuesta, f"{self.det.folio} →")
        respuesta = self.client.get(reverse("mesa:pedidos"))
        self.assertContains(respuesta, ">detenido<")
        url = reverse("mesa:incidencia_detalle", args=[self.det.pk])
        respuesta = self.client.get(url)
        self.assertContains(respuesta, "detenido en piso")
        self.assertContains(respuesta, "Resolver y reanudar el pedido")
        respuesta = self.client.post(url, {"accion": "resolver", "texto": "Se cambió el six por uno bueno."}, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.pedido.refresh_from_db()
        self.det.refresh_from_db()
        self.assertEqual((self.pedido.detenido, self.det.estado), (False, Incidencia.RESUELTA))
        self.assertContains(self.client.get(reverse("mesa:dashboard")), "Ningún pedido detenido")
        self.assertNotContains(self.client.get(reverse("mesa:pedidos")), ">detenido<")

    def test_la_forma_de_nueva_incidencia_no_ofrece_det(self):
        respuesta = self.client.get(reverse("mesa:incidencia_nueva"))
        self.assertNotContains(respuesta, "DET · Detenido en piso")
        self.assertContains(respuesta, "DAN · Daño / rotura")

    def test_dos_bloqueos_el_segundo_se_suma_y_solo_se_reanuda_al_quitar_ambos(self):
        from apps.incidencias.services import abrir_sin_paqueteria, resolver

        paq = abrir_sin_paqueteria(self.pedido, "Ningún carrier cotiza.")
        self.assertNotEqual(paq.pk, self.det.pk)
        resolver(self.det, "Ya se repuso el producto.", self.operador)
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.detenido)  # la PAQ sigue abierta
        resolver(paq, "Se eligió paquetería.", self.operador)
        self.pedido.refresh_from_db()
        self.assertFalse(self.pedido.detenido)
