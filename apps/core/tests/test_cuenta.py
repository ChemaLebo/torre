"""Mi cuenta (/cuenta/): cambio de contraseña propia para todos los roles y de
PIN propio para piso y Mesa; la sesión sobrevive al cambio y todo se audita."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario


def crear_usuario(username, rol, cliente=None, pin=""):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente, pin=pin)
    return user


class CuentaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.piso = crear_usuario("piso1", "piso", pin="1111")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.url = reverse("core:cuenta")

    def test_anonimo_va_al_login(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta["Location"])

    def test_link_en_el_menu(self):
        self.client.force_login(self.portal)
        respuesta = self.client.get(reverse("portal:dashboard"))
        self.assertContains(respuesta, f'href="{self.url}"')

    def test_cambia_password_y_conserva_sesion(self):
        self.client.force_login(self.portal)
        respuesta = self.client.post(self.url, {
            "accion": "password", "old_password": "x12345678",
            "new_password1": "nueva-clave-99", "new_password2": "nueva-clave-99",
        }, follow=True)
        self.assertContains(respuesta, "Contraseña actualizada")
        self.portal.refresh_from_db()
        self.assertTrue(self.portal.check_password("nueva-clave-99"))
        self.assertEqual(self.client.get(self.url).status_code, 200)  # sigue logueado
        self.assertTrue(EventoAuditoria.objects.filter(entidad="usuario", entidad_id="karina", accion="cambio_password").exists())

    def test_password_actual_incorrecta_o_corta_no_cambia(self):
        self.client.force_login(self.portal)
        respuesta = self.client.post(self.url, {
            "accion": "password", "old_password": "mala",
            "new_password1": "nueva-clave-99", "new_password2": "nueva-clave-99",
        })
        self.assertEqual(respuesta.status_code, 200)
        respuesta = self.client.post(self.url, {
            "accion": "password", "old_password": "x12345678",
            "new_password1": "corta", "new_password2": "corta",
        })
        self.assertEqual(respuesta.status_code, 200)
        self.portal.refresh_from_db()
        self.assertTrue(self.portal.check_password("x12345678"))

    def test_portal_no_ve_pin_y_piso_lo_cambia(self):
        self.client.force_login(self.portal)
        self.assertNotContains(self.client.get(self.url), "Cambiar PIN")
        respuesta = self.client.post(self.url, {"accion": "pin", "password": "x12345678", "pin": "2222", "pin2": "2222"})
        self.assertEqual(respuesta.status_code, 200)

        self.client.force_login(self.piso)
        self.assertContains(self.client.get(self.url), "Cambiar PIN")
        respuesta = self.client.post(self.url, {"accion": "pin", "password": "x12345678", "pin": "2222", "pin2": "2222"}, follow=True)
        self.assertContains(respuesta, "PIN actualizado")
        perfil = PerfilUsuario.objects.get(usuario=self.piso)
        self.assertTrue(perfil.check_pin("2222"))
        self.assertFalse(perfil.check_pin("1111"))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="usuario", entidad_id="piso1", accion="cambio_pin").exists())

    def test_pin_invalido_o_password_mala_no_cambia(self):
        self.client.force_login(self.piso)
        for datos in (
            {"password": "mala", "pin": "2222", "pin2": "2222"},
            {"password": "x12345678", "pin": "22", "pin2": "22"},
            {"password": "x12345678", "pin": "2222", "pin2": "3333"},
        ):
            respuesta = self.client.post(self.url, {"accion": "pin", **datos})
            self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(PerfilUsuario.objects.get(usuario=self.piso).check_pin("1111"))
