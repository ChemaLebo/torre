"""Tests de manuales en el portal: lectura para el cliente y sugerencias auditadas.

Usan los partials pre-renderizados de templates/manuales/ (commiteados por
`manage.py render_manuales`) — nunca la carpeta fuente de .md.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario

SLUG_SOP02 = "sop-02-recepcion-descarga"


class BaseManualesPortal(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        User = get_user_model()
        cls.karina = User.objects.create_user(
            "karina", password="colima2026", first_name="Karina"
        )
        PerfilUsuario.objects.create(
            usuario=cls.karina, rol=PerfilUsuario.ROL_PORTAL, cliente=cls.colima
        )

    def entrar(self):
        self.client.login(username="karina", password="colima2026")

    def _sugerencias(self):
        return EventoAuditoria.objects.filter(
            entidad="manual", accion="sugerencia_cliente"
        )


class LecturaManualesTests(BaseManualesPortal):
    def test_lista_200_con_copy_del_portal(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:manuales"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Así operamos tu producto, paso a paso.")
        self.assertContains(respuesta, "SOP-02")
        self.assertContains(respuesta, "Seguridad")

    def test_detalle_200_con_texto_real(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:manual_detalle", args=[SLUG_SOP02]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "conos")
        self.assertContains(respuesta, "Sugerir una mejora")

    def test_detalle_inexistente_404(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:manual_detalle", args=["sop-99-nada"]))
        self.assertEqual(respuesta.status_code, 404)

    def test_anonimo_302(self):
        respuesta = self.client.get(reverse("portal:manuales"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("entrar", respuesta["Location"])
        respuesta = self.client.get(reverse("portal:manual_detalle", args=[SLUG_SOP02]))
        self.assertEqual(respuesta.status_code, 302)

    def test_rol_mesa_no_entra_al_portal(self):
        user = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=user, rol=PerfilUsuario.ROL_MESA)
        self.client.force_login(user)
        respuesta = self.client.get(reverse("portal:manuales"))
        self.assertEqual(respuesta.status_code, 403)


class SugerenciaManualTests(BaseManualesPortal):
    def test_sugerencia_crea_evento_auditado(self):
        self.entrar()
        respuesta = self.client.post(
            reverse("portal:manual_detalle", args=[SLUG_SOP02]),
            {"texto": "El paso de los conos merece un diagrama."},
            follow=True,
        )
        self.assertContains(
            respuesta, "Gracias — tu sugerencia llegó directo a la Mesa de Control."
        )
        evento = self._sugerencias().get()
        self.assertEqual(evento.cliente, self.colima)
        self.assertEqual(evento.entidad_id, "SOP-02")
        self.assertEqual(evento.accion, "sugerencia_cliente")
        self.assertEqual(evento.actor_id, "karina")
        self.assertEqual(evento.delta["texto"], "El paso de los conos merece un diagrama.")

    def test_sugerencia_vacia_no_crea_evento(self):
        self.entrar()
        respuesta = self.client.post(
            reverse("portal:manual_detalle", args=[SLUG_SOP02]), {"texto": "   "},
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Cuéntanos tu sugerencia antes de enviarla")
        self.assertEqual(self._sugerencias().count(), 0)

    def test_sugerencia_anonima_302_no_crea(self):
        respuesta = self.client.post(
            reverse("portal:manual_detalle", args=[SLUG_SOP02]),
            {"texto": "Intento sin sesión."},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self._sugerencias().count(), 0)
