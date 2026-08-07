"""Sistema de tema light/dark de base.html.

El contrato del template base: <html> lleva data-theme con el default por rol
(piso arranca en LIGHT — bodega iluminada —, mesa/portal en dark), el script
que lee localStorage("torre-tema") corre ANTES de la hoja de estilos (cero
flash) y el botón sol/luna (#btn-tema) vive en el footer del sidebar.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario


def crear_usuario(username, rol, cliente=None):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


class TemaBaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.usuario_mesa = crear_usuario("mesa1", PerfilUsuario.ROL_MESA)
        cls.usuario_piso = crear_usuario("piso1", PerfilUsuario.ROL_PISO)
        cls.usuario_portal = crear_usuario("karina", PerfilUsuario.ROL_PORTAL, cliente=cls.colima)

    # ── Login (sin sesión: default dark, script presente) ──

    def test_login_renderiza_con_tema_dark_por_defecto(self):
        respuesta = self.client.get(reverse("core:login"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'data-theme="dark"')
        self.assertContains(respuesta, "torre-tema")

    def test_script_de_tema_corre_antes_de_la_hoja_de_estilos(self):
        # Cero flash: si el CSS cargara antes que el script, el primer paint
        # saldría con el tema equivocado para quien ya eligió el suyo.
        html = self.client.get(reverse("core:login")).content.decode()
        self.assertIn("torre-tema", html)
        self.assertIn("css/wop.css", html)
        self.assertLess(html.index("torre-tema"), html.index("css/wop.css"))

    # ── Smoke por rol: default del tema + toggle presente ──

    def test_mesa_dashboard_arranca_en_dark_con_toggle(self):
        self.client.force_login(self.usuario_mesa)
        respuesta = self.client.get(reverse("mesa:dashboard"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'data-theme="dark"')
        self.assertContains(respuesta, 'id="btn-tema"')

    def test_piso_home_arranca_en_light_con_toggle(self):
        # Los operadores de piso trabajan con celulares en bodega iluminada.
        self.client.force_login(self.usuario_piso)
        respuesta = self.client.get(reverse("piso:home"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'data-theme="light"')
        self.assertContains(respuesta, 'id="btn-tema"')

    def test_portal_dashboard_arranca_en_dark_con_toggle(self):
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'data-theme="dark"')
        self.assertContains(respuesta, 'id="btn-tema"')

    def test_el_toggle_persiste_en_localstorage(self):
        # El script del toggle guarda la elección bajo la llave canónica.
        self.client.force_login(self.usuario_piso)
        html = self.client.get(reverse("piso:home")).content.decode()
        self.assertIn('localStorage.setItem("torre-tema"', html)
        self.assertIn('localStorage.getItem("torre-tema")', html)
