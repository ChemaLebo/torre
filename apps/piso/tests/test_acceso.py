"""Acceso al piso: solo roles piso/mesa (y superuser). El portal NO entra."""
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.models import PerfilUsuario

from .base import PisoTestCase


class AccesoPisoTests(PisoTestCase):
    def test_anonimo_redirige_a_login(self):
        respuesta = self.client.get(reverse("piso:home"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/entrar/", respuesta["Location"])

    def test_rol_portal_no_entra_al_piso(self):
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.get(reverse("piso:home"))
        self.assertEqual(respuesta.status_code, 403)

    def test_usuario_sin_perfil_no_entra(self):
        pelado = get_user_model().objects.create_user("sinperfil", password="x")
        self.client.force_login(pelado)
        respuesta = self.client.get(reverse("piso:home"))
        self.assertEqual(respuesta.status_code, 403)

    def test_rol_piso_ve_mi_turno(self):
        self.login_piso()
        respuesta = self.client.get(reverse("piso:home"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Mi turno")

    def test_rol_mesa_tambien_entra(self):
        mesa = get_user_model().objects.create_user("mesa1", password="x")
        PerfilUsuario.objects.create(usuario=mesa, rol=PerfilUsuario.ROL_MESA)
        self.client.force_login(mesa)
        respuesta = self.client.get(reverse("piso:home"))
        self.assertEqual(respuesta.status_code, 200)

    def test_todas_las_listas_cargan(self):
        # entrega_local queda fuera: sin flota propia (TORRE["FLOTA_PROPIA"]
        # default False) responde 404 amable — ver test_entrega_local.
        self.login_piso()
        for nombre in ("recepciones", "picking", "empaque", "salida", "conteos", "cuarentena"):
            respuesta = self.client.get(reverse(f"piso:{nombre}"))
            self.assertEqual(respuesta.status_code, 200, f"piso:{nombre} no cargó")
