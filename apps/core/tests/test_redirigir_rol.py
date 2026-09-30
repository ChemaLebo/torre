"""Links cruzados Mesa ⇄ portal (Chema 2026-09-30): un pedido o una incidencia
abiertos con el rol equivocado redirigen a la misma entidad en la pantalla
del rol, en vez de un 403. Piso sigue recibiendo 403."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia


class RedirigirRolTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente, crear_tienda(self.cliente))
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="Rota")
        usuario = get_user_model()
        self.mesa = usuario.objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.portal = usuario.objects.create_user("cliente1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.portal, rol="portal", cliente=self.cliente)
        self.piso = usuario.objects.create_user("piso1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.piso, rol="piso")

    def test_mesa_con_link_de_portal_cae_en_mesa(self):
        self.client.force_login(self.mesa)
        for nombre_portal, nombre_mesa, pk in (
            ("portal:pedido_detalle", "mesa:pedido_detalle", self.pedido.pk),
            ("portal:incidencia_detalle", "mesa:incidencia_detalle", self.inc.pk),
        ):
            respuesta = self.client.get(reverse(nombre_portal, args=[pk]))
            self.assertRedirects(respuesta, reverse(nombre_mesa, args=[pk]))

    def test_portal_con_link_de_mesa_cae_en_su_portal(self):
        self.client.force_login(self.portal)
        for nombre_mesa, nombre_portal, pk in (
            ("mesa:pedido_detalle", "portal:pedido_detalle", self.pedido.pk),
            ("mesa:incidencia_detalle", "portal:incidencia_detalle", self.inc.pk),
        ):
            respuesta = self.client.get(reverse(nombre_mesa, args=[pk]))
            self.assertRedirects(respuesta, reverse(nombre_portal, args=[pk]))

    def test_portal_con_pedido_de_otro_cliente_no_lo_ve(self):
        ajeno = crear_pedido(crear_cliente(), None)
        self.client.force_login(self.portal)
        respuesta = self.client.get(reverse("mesa:pedido_detalle", args=[ajeno.pk]), follow=True)
        self.assertEqual(respuesta.status_code, 404)

    def test_un_post_con_el_rol_equivocado_sigue_siendo_403(self):
        self.client.force_login(self.portal)
        respuesta = self.client.post(reverse("mesa:pedido_detalle", args=[self.pedido.pk]), {"accion": "cancelar", "folio": self.pedido.folio, "motivo": "x"})
        self.assertEqual(respuesta.status_code, 403)

    def test_piso_sigue_sin_pasar(self):
        self.client.force_login(self.piso)
        self.assertEqual(self.client.get(reverse("mesa:pedido_detalle", args=[self.pedido.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("portal:pedido_detalle", args=[self.pedido.pk])).status_code, 403)
