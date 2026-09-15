"""Acceso por correo: enlace de un solo uso al crear usuarios con correo,
"¿Olvidaste tu contraseña?" en el login y "Enviar acceso" en la ficha del
cliente. En tests el correo va a django.core.mail.outbox."""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.core import services
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario

RE_URL = re.compile(r"https?://\S+/restablecer/\S+/\S+/")


def crear_usuario(username, rol, cliente=None, email=""):
    user = get_user_model().objects.create_user(username=username, password="x12345678", email=email)
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


class AccesoPorCorreoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.karina = crear_usuario("karina", "portal", cliente=cls.colima, email="karina@colima.mx")
        cls.sin_correo = crear_usuario("lupe", "portal", cliente=cls.colima, email="lupe@torre380e.mx")

    def test_email_real_distingue_el_relleno(self):
        self.assertTrue(services.email_real(self.karina))
        self.assertFalse(services.email_real(self.sin_correo))

    def test_enviar_acceso_manda_enlace_que_define_password_y_audita_sin_token(self):
        self.assertTrue(services.enviar_acceso(self.karina, cliente=self.colima, actor=self.mesa))
        self.assertEqual(len(mail.outbox), 1)
        correo = mail.outbox[0]
        self.assertEqual(correo.to, ["karina@colima.mx"])
        self.assertIn("Tu acceso a Torre", correo.subject)
        url = RE_URL.search(correo.body).group(0)
        evento = EventoAuditoria.objects.get(entidad="usuario", entidad_id="karina", accion="acceso_enviado")
        self.assertNotIn(url.rsplit("/", 2)[1], evento.motivo)  # el token no queda en el log

        ruta = url.split("://", 1)[1].split("/", 1)[1]
        respuesta = self.client.get("/" + ruta, follow=True)  # Django redirige a la URL con token en sesión
        self.assertContains(respuesta, "Define tu contraseña")
        respuesta = self.client.post(respuesta.request["PATH_INFO"], {
            "new_password1": "clave-nueva-2026", "new_password2": "clave-nueva-2026",
        }, follow=True)
        self.karina.refresh_from_db()
        self.assertTrue(self.karina.check_password("clave-nueva-2026"))
        self.assertContains(respuesta, "Hoy")  # post_reset_login: entra directo al portal
        # El enlace es de un solo uso.
        respuesta = self.client.get("/" + ruta, follow=True)
        self.assertContains(respuesta, "Enlace caducado")

    def test_correo_brandeado_html_y_reply_to(self):
        from django.test import override_settings

        Cliente.objects.filter(pk=self.colima.pk).update(
            branding={"color_primario": "#123456", "nombre_publico": "Colima", "logo_url": "https://cdn/logo.png"},
        )
        self.colima.refresh_from_db()
        with override_settings(EMAIL_REPLY_TO="eduardo@wop.partners"):
            services.enviar_acceso(self.karina, cliente=self.colima)
        correo = mail.outbox[0]
        self.assertEqual(correo.reply_to, ["eduardo@wop.partners"])
        self.assertNotIn("Local 380", correo.body)
        html, tipo = correo.alternatives[0]
        self.assertEqual(tipo, "text/html")
        self.assertIn("#123456", html)
        self.assertIn("https://cdn/logo.png", html)
        self.assertIn("Definir mi contraseña", html)
        self.assertIn(RE_URL.search(correo.body).group(0), html)  # el botón lleva al mismo enlace

    def test_sin_correo_real_no_manda(self):
        self.assertFalse(services.enviar_acceso(self.sin_correo))
        self.assertEqual(len(mail.outbox), 0)

    def test_olvide_manda_correo_y_responde_igual_si_no_existe(self):
        respuesta = self.client.get(reverse("core:login"))
        self.assertContains(respuesta, reverse("core:olvide"))
        respuesta = self.client.post(reverse("core:olvide"), {"email": "karina@colima.mx"}, follow=True)
        self.assertContains(respuesta, "Revisa tu correo")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/restablecer/", mail.outbox[0].body)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("Definir contraseña nueva", html)
        self.assertIn("/restablecer/", html)
        respuesta = self.client.post(reverse("core:olvide"), {"email": "nadie@colima.mx"}, follow=True)
        self.assertContains(respuesta, "Revisa tu correo")
        self.assertEqual(len(mail.outbox), 1)


class AccesoDesdeMesaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.url = reverse("mesa:cliente_detalle", args=[cls.colima.pk])

    def setUp(self):
        self.client.force_login(self.mesa)

    def test_alta_con_correo_manda_enlace_y_no_muestra_password(self):
        respuesta = self.client.post(self.url, {
            "accion": "usuario_nuevo", "username": "diego", "nombre": "Diego Colima", "email": "diego@colima.mx",
        }, follow=True)
        self.assertContains(respuesta, "Le enviamos el enlace de acceso a diego@colima.mx")
        self.assertNotContains(respuesta, "Contraseña:")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["diego@colima.mx"])

    def test_alta_con_correo_y_password_capturada_no_manda(self):
        respuesta = self.client.post(self.url, {
            "accion": "usuario_nuevo", "username": "diego", "email": "diego@colima.mx", "password": "clave-fija-2026",
        }, follow=True)
        self.assertContains(respuesta, "Contraseña: clave-fija-2026")
        self.assertEqual(len(mail.outbox), 0)

    def test_alta_sin_correo_sigue_mostrando_password(self):
        respuesta = self.client.post(self.url, {"accion": "usuario_nuevo", "username": "diego"}, follow=True)
        self.assertContains(respuesta, "Usuario diego creado. Contraseña:")
        self.assertEqual(len(mail.outbox), 0)

    def test_enviar_acceso_desde_la_ficha(self):
        usuario = crear_usuario("diego", "portal", cliente=self.colima, email="diego@colima.mx")
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'value="usuario_enviar_acceso"')
        respuesta = self.client.post(self.url, {"accion": "usuario_enviar_acceso", "usuario_id": usuario.pk}, follow=True)
        self.assertContains(respuesta, "Enlace de acceso enviado a diego@colima.mx")
        self.assertEqual(len(mail.outbox), 1)
        sin = crear_usuario("lupe", "portal", cliente=self.colima, email="lupe@torre380e.mx")
        respuesta = self.client.post(self.url, {"accion": "usuario_enviar_acceso", "usuario_id": sin.pk}, follow=True)
        self.assertContains(respuesta, "no tiene un correo real")
        self.assertEqual(len(mail.outbox), 1)
