"""Tests de Web Push (PWA de Torre): suscripción idempotente, envío con poda
de suscripciones muertas, no-op sin VAPID, disparador de pedido nuevo y las
piezas de la PWA (sw.js, manifest, botón de opt-in).

webpush SIEMPRE va mockeado: los tests jamás tocan un push service real.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from pywebpush import WebPushException

from apps.core.models import Cliente, PerfilUsuario
from apps.mensajeria import push
from apps.mensajeria.models import SuscripcionPush

User = get_user_model()


def crear_usuario(username, rol, password=None):
    usuario = User.objects.create_user(username=username, password=password or "clave-123")
    PerfilUsuario.objects.create(usuario=usuario, rol=rol)
    return usuario


def crear_suscripcion(usuario, endpoint, activo=True):
    return SuscripcionPush.objects.create(
        usuario=usuario, endpoint=endpoint, p256dh="p256dh-demo", auth="auth-demo",
        activo=activo,
    )


# Endpoint SIEMPRE de un push service real: suscribir() valida lista blanca.
ENDPOINT_FCM = "https://fcm.googleapis.com/fcm/send/ep-1"


def datos_subscription(endpoint=ENDPOINT_FCM, p256dh="llave-p", auth="llave-a"):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class SuscribirTests(TestCase):
    def setUp(self):
        self.usuario = crear_usuario("piso1", "piso")

    def test_suscribir_crea_la_suscripcion(self):
        suscripcion = push.suscribir(self.usuario, datos_subscription(), user_agent="Chrome Android")
        self.assertEqual(SuscripcionPush.objects.count(), 1)
        self.assertEqual(suscripcion.usuario, self.usuario)
        self.assertEqual(suscripcion.endpoint, ENDPOINT_FCM)
        self.assertEqual(suscripcion.p256dh, "llave-p")
        self.assertEqual(suscripcion.auth, "llave-a")
        self.assertEqual(suscripcion.user_agent, "Chrome Android")
        self.assertTrue(suscripcion.activo)

    def test_rotacion_de_dispositivo_reasigna_con_auditoria(self):
        # Celular compartido: el endpoint existente de A se reasigna a B —
        # EXPLÍCITAMENTE, con evento de auditoría (jamás robo silencioso).
        from apps.core.models import EventoAuditoria

        primera = push.suscribir(self.usuario, datos_subscription(), user_agent="Chrome Android")
        otro = crear_usuario("jefe", "piso")
        # Variante del usuario anterior en el MISMO dispositivo: debe podarse.
        variante = SuscripcionPush.objects.create(
            usuario=self.usuario, endpoint="https://fcm.googleapis.com/fcm/send/vieja",
            p256dh="p", auth="a", user_agent="Chrome Android", activo=True,
        )
        segunda = push.suscribir(
            otro, datos_subscription(p256dh="nueva-p", auth="nueva-a"),
            user_agent="Chrome Android",
        )

        self.assertEqual(segunda.pk, primera.pk)
        self.assertEqual(segunda.usuario, otro)
        self.assertEqual(segunda.p256dh, "nueva-p")
        self.assertTrue(segunda.activo)
        evento = EventoAuditoria.objects.filter(
            entidad="suscripcion_push", entidad_id=str(primera.pk),
            accion="rotacion_dispositivo",
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.delta["usuario_anterior"], "piso1")
        self.assertEqual(evento.delta["usuario_nuevo"], "jefe")
        variante.refresh_from_db()
        self.assertFalse(variante.activo)

    def test_resuscribir_mismo_usuario_revive_sin_auditoria(self):
        from apps.core.models import EventoAuditoria

        primera = push.suscribir(self.usuario, datos_subscription())
        primera.activo = False  # simulamos una poda previa
        primera.save(update_fields=["activo"])
        segunda = push.suscribir(self.usuario, datos_subscription())
        self.assertEqual(SuscripcionPush.objects.count(), 1)
        self.assertEqual(segunda.pk, primera.pk)
        self.assertTrue(segunda.activo)  # re-suscribirse la revive
        self.assertFalse(
            EventoAuditoria.objects.filter(accion="rotacion_dispositivo").exists()
        )

    def test_suscribir_incompleta_truena_con_mensaje_claro(self):
        with self.assertRaises(ValueError):
            push.suscribir(self.usuario, {"endpoint": ENDPOINT_FCM, "keys": {}})

    def test_endpoint_fuera_de_lista_blanca_se_rechaza(self):
        # Anti-SSRF: https obligatorio, host de push service conocido, jamás IPs.
        malos = [
            "http://fcm.googleapis.com/fcm/send/x",          # sin TLS
            "https://torre-interna.local/webhook",           # host arbitrario
            "https://169.254.169.254/latest/meta-data/",     # metadata IP
            "https://127.0.0.1:8000/",                       # loopback
            "https://fcm.googleapis.com.evil.mx/x",          # sufijo falso
        ]
        for endpoint in malos:
            with self.assertRaises(ValueError, msg=endpoint):
                push.suscribir(self.usuario, datos_subscription(endpoint=endpoint))
        self.assertEqual(SuscripcionPush.objects.count(), 0)

    def test_hosts_de_push_reales_pasan(self):
        buenos = [
            "https://fcm.googleapis.com/fcm/send/abc",
            "https://updates.push.services.mozilla.com/wpush/v2/abc",
            "https://db5p.notify.windows.com/w/?token=abc",
            "https://web.push.apple.com/QOl0Xkpm",
        ]
        for endpoint in buenos:
            self.assertTrue(push.endpoint_permitido(endpoint), endpoint)

    def test_longitudes_excesivas_se_rechazan(self):
        with self.assertRaises(ValueError):
            push.suscribir(
                self.usuario,
                datos_subscription(endpoint="https://fcm.googleapis.com/f/" + "x" * 500),
            )
        with self.assertRaises(ValueError):
            push.suscribir(self.usuario, datos_subscription(p256dh="p" * 201))
        with self.assertRaises(ValueError):
            push.suscribir(self.usuario, datos_subscription(auth="a" * 101))

    def test_maximo_cinco_activas_por_usuario_poda_la_mas_vieja(self):
        for i in range(6):
            push.suscribir(
                self.usuario,
                datos_subscription(endpoint=f"https://fcm.googleapis.com/fcm/send/ep-{i}"),
            )
        activas = SuscripcionPush.objects.filter(usuario=self.usuario, activo=True)
        self.assertEqual(activas.count(), 5)
        primera = SuscripcionPush.objects.get(endpoint="https://fcm.googleapis.com/fcm/send/ep-0")
        self.assertFalse(primera.activo)  # la más vieja se podó

    def test_desactivar_poda_por_endpoint_o_todas(self):
        push.suscribir(self.usuario, datos_subscription())
        push.suscribir(
            self.usuario,
            datos_subscription(endpoint="https://fcm.googleapis.com/fcm/send/ep-2"),
        )
        self.assertEqual(push.desactivar(self.usuario, endpoint=ENDPOINT_FCM), 1)
        self.assertEqual(
            SuscripcionPush.objects.filter(usuario=self.usuario, activo=True).count(), 1,
        )
        self.assertEqual(push.desactivar(self.usuario), 1)
        self.assertEqual(
            SuscripcionPush.objects.filter(usuario=self.usuario, activo=True).count(), 0,
        )


class EndpointSuscripcionTests(TestCase):
    def setUp(self):
        self.url = reverse("mensajeria:suscribir_push")
        self.usuario = crear_usuario("piso1", "piso", password="colima-pwa")

    def test_anonimo_redirige_al_login(self):
        respuesta = self.client.post(
            self.url, data=json.dumps(datos_subscription()), content_type="application/json",
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(SuscripcionPush.objects.count(), 0)

    def test_anonimo_con_accept_json_recibe_401_json(self):
        # push.js manda Accept: application/json — con la sesión expirada el
        # fetch NO debe celebrar el 200 de la página de login: 401 directo.
        respuesta = self.client.post(
            self.url, data=json.dumps(datos_subscription()), content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 401)
        self.assertFalse(respuesta.json()["ok"])

    def test_rol_portal_no_puede_suscribirse(self):
        crear_usuario("karina", "portal", password="colima-pwa")
        self.client.login(username="karina", password="colima-pwa")
        respuesta = self.client.post(
            self.url, data=json.dumps(datos_subscription()), content_type="application/json",
        )
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(SuscripcionPush.objects.count(), 0)

    def test_guarda_para_el_usuario_autenticado(self):
        self.client.login(username="piso1", password="colima-pwa")
        respuesta = self.client.post(
            self.url, data=json.dumps(datos_subscription()), content_type="application/json",
            HTTP_USER_AGENT="Chrome Android PWA",
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json(), {"ok": True})
        suscripcion = SuscripcionPush.objects.get()
        self.assertEqual(suscripcion.usuario, self.usuario)
        self.assertEqual(suscripcion.user_agent, "Chrome Android PWA")

    def test_payload_invalido_regresa_400(self):
        self.client.login(username="piso1", password="colima-pwa")
        rota = self.client.post(self.url, data="esto no es json", content_type="application/json")
        self.assertEqual(rota.status_code, 400)
        incompleta = self.client.post(
            self.url, data=json.dumps({"endpoint": ""}), content_type="application/json",
        )
        self.assertEqual(incompleta.status_code, 400)
        endpoint_malo = self.client.post(
            self.url,
            data=json.dumps(datos_subscription(endpoint="https://atacante.mx/hook")),
            content_type="application/json",
        )
        self.assertEqual(endpoint_malo.status_code, 400)
        self.assertEqual(SuscripcionPush.objects.count(), 0)


class BajaPushTests(TestCase):
    def setUp(self):
        self.url = reverse("mensajeria:baja_push")
        self.usuario = crear_usuario("piso1", "piso", password="colima-pwa")
        self.suscripcion = crear_suscripcion(self.usuario, ENDPOINT_FCM)

    def test_baja_por_endpoint(self):
        self.client.login(username="piso1", password="colima-pwa")
        respuesta = self.client.post(
            self.url, data=json.dumps({"endpoint": ENDPOINT_FCM}),
            content_type="application/json",
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["podadas"], 1)
        self.suscripcion.refresh_from_db()
        self.assertFalse(self.suscripcion.activo)

    def test_anonimo_no_puede(self):
        respuesta = self.client.post(
            self.url, data="{}", content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 401)
        self.suscripcion.refresh_from_db()
        self.assertTrue(self.suscripcion.activo)

    def test_logout_poda_las_suscripciones(self):
        self.client.login(username="piso1", password="colima-pwa")
        respuesta = self.client.post(reverse("core:logout"))
        self.assertEqual(respuesta.status_code, 302)
        self.suscripcion.refresh_from_db()
        self.assertFalse(self.suscripcion.activo)


class EnviarPushTests(TestCase):
    """enviar_push_a_rol con webpush mockeado: envío, poda y best-effort."""

    def setUp(self):
        self.piso1 = crear_usuario("piso1", "piso")
        self.jefe = crear_usuario("jefe", "piso")
        self.mesa1 = crear_usuario("mesa1", "mesa")
        self.sub_piso1 = crear_suscripcion(self.piso1, "https://push.example/piso-1")
        self.sub_jefe = crear_suscripcion(self.jefe, "https://push.example/jefe-1")
        self.sub_mesa = crear_suscripcion(self.mesa1, "https://push.example/mesa-1")
        self.sub_muerta = crear_suscripcion(self.piso1, "https://push.example/piso-vieja", activo=False)

    def test_manda_solo_a_las_suscripciones_activas_del_rol(self):
        with patch("apps.mensajeria.push.webpush") as mock_webpush:
            enviadas = push.enviar_push_a_rol("piso", "📦 Nuevo pedido", "2 pzas", url="/piso/picking/")
        self.assertEqual(enviadas, 2)
        self.assertEqual(mock_webpush.call_count, 2)
        endpoints = {c.kwargs["subscription_info"]["endpoint"] for c in mock_webpush.call_args_list}
        self.assertEqual(endpoints, {"https://push.example/piso-1", "https://push.example/jefe-1"})
        # Payload y claims VAPID bien formados.
        kwargs = mock_webpush.call_args.kwargs
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload, {"title": "📦 Nuevo pedido", "body": "2 pzas", "url": "/piso/picking/"})
        self.assertTrue(kwargs["vapid_claims"]["sub"].startswith("mailto:"))
        self.assertTrue(kwargs["vapid_private_key"].endswith(".pem"))
        # Anti-cuelgue: timeout SIEMPRE explícito (pywebpush manda timeout=None
        # si no se pasa) y sesión propia sin redirects (anti-SSRF por rebote).
        self.assertEqual(kwargs["timeout"], push.TIMEOUT_ENVIO_S)
        self.assertIs(kwargs["requests_session"], push._sesion_push)

    def test_usuario_desactivado_no_recibe_push(self):
        # is_active=False (baja del empleado) corta el push aunque la
        # suscripción siga activa en la tabla.
        self.jefe.is_active = False
        self.jefe.save(update_fields=["is_active"])
        with patch("apps.mensajeria.push.webpush") as mock_webpush:
            enviadas = push.enviar_push_a_rol("piso", "Prueba", "cuerpo")
        self.assertEqual(enviadas, 1)
        endpoints = {c.kwargs["subscription_info"]["endpoint"] for c in mock_webpush.call_args_list}
        self.assertEqual(endpoints, {"https://push.example/piso-1"})
        with patch("apps.mensajeria.push.webpush") as mock_webpush:
            self.assertEqual(push.enviar_push_a_usuario(self.jefe, "Prueba", "cuerpo"), 0)
        mock_webpush.assert_not_called()

    def test_410_poda_la_suscripcion_muerta_y_sigue(self):
        def explota_en_jefe(subscription_info, **kwargs):
            if subscription_info["endpoint"] == "https://push.example/jefe-1":
                raise WebPushException("Gone", response=SimpleNamespace(status_code=410))

        with patch("apps.mensajeria.push.webpush", side_effect=explota_en_jefe):
            enviadas = push.enviar_push_a_rol("piso", "Prueba", "cuerpo")
        self.assertEqual(enviadas, 1)
        self.sub_jefe.refresh_from_db()
        self.sub_piso1.refresh_from_db()
        self.assertFalse(self.sub_jefe.activo)
        self.assertTrue(self.sub_piso1.activo)

    def test_excepcion_generica_no_truena_y_cuenta_bien(self):
        def explota_en_piso1(subscription_info, **kwargs):
            if subscription_info["endpoint"] == "https://push.example/piso-1":
                raise RuntimeError("el push service se cayó")

        with patch("apps.mensajeria.push.webpush", side_effect=explota_en_piso1):
            enviadas = push.enviar_push_a_rol("piso", "Prueba", "cuerpo")
        self.assertEqual(enviadas, 1)
        self.sub_piso1.refresh_from_db()
        self.assertTrue(self.sub_piso1.activo)  # error genérico NO poda

    def test_enviar_a_usuario_solo_toca_sus_suscripciones(self):
        with patch("apps.mensajeria.push.webpush") as mock_webpush:
            enviadas = push.enviar_push_a_usuario(self.mesa1, "Hola", "cuerpo")
        self.assertEqual(enviadas, 1)
        self.assertEqual(
            mock_webpush.call_args.kwargs["subscription_info"]["endpoint"],
            "https://push.example/mesa-1",
        )

    @override_settings(VAPID_PUBLIC_KEY="")
    def test_sin_vapid_es_noop_con_cero_llamadas(self):
        self.assertFalse(push.vapid_configurado())
        with patch("apps.mensajeria.push.webpush") as mock_webpush:
            self.assertEqual(push.enviar_push_a_rol("piso", "Prueba", "cuerpo"), 0)
            self.assertEqual(push.enviar_push_a_usuario(self.piso1, "Prueba", "cuerpo"), 0)
        mock_webpush.assert_not_called()


class TriggerPedidoNuevoTests(TestCase):
    """El alta de pedido avisa al piso; si el push truena, el pedido nace igual."""

    def setUp(self):
        from apps.catalogo.models import SKU

        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.sku = SKU.objects.create(
            cliente=self.cliente, codigo="COL-SIX", descripcion="Colimita six",
            peso_gr=2000, requiere_lote=False,
        )

    def test_pedido_manual_dispara_push_al_rol_piso(self):
        from apps.pedidos.services import crear_pedido_manual

        with patch("apps.mensajeria.push.enviar_push_a_rol") as mock_push, \
             self.captureOnCommitCallbacks(execute=True):
            pedido = crear_pedido_manual(
                self.cliente, comprador_nombre="Karina", comprador_tel="",
                direccion={"zip": "01780", "city": "CDMX"}, cp="01780",
                lineas=[(self.sku, 2)], actor="mesa1",
            )
        llamadas_piso = [c for c in mock_push.call_args_list if c.args[0] == "piso"]
        self.assertEqual(len(llamadas_piso), 1)
        rol, titulo, cuerpo = llamadas_piso[0].args[:3]
        self.assertIn(pedido.folio, titulo)
        self.assertIn("2 pzas", cuerpo)
        self.assertEqual(llamadas_piso[0].kwargs.get("url"), "/piso/picking/")

    def test_ingesta_shopify_dispara_push_al_rol_piso(self):
        from django.apps import apps as django_apps

        from apps.pedidos.services import ingerir_pedido_shopify

        Tienda = django_apps.get_model("integraciones", "Tienda")
        tienda = Tienda.objects.create(
            cliente=self.cliente, plataforma="shopify",
            dominio="colima-mx.myshopify.com", token="",
        )
        payload = {
            "id": 987654, "name": "#1001", "total_price": "500.00",
            "shipping_address": {"zip": "28017", "city": "Colima"},
            "line_items": [{"sku": "COL-SIX", "quantity": 3}],
        }
        with patch("apps.mensajeria.push.enviar_push_a_rol") as mock_push, \
             self.captureOnCommitCallbacks(execute=True):
            pedido = ingerir_pedido_shopify(tienda, payload)
        llamadas_piso = [c for c in mock_push.call_args_list if c.args[0] == "piso"]
        self.assertEqual(len(llamadas_piso), 1)
        self.assertIn(pedido.folio, llamadas_piso[0].args[1])
        self.assertIn("Colima", llamadas_piso[0].args[2])

    def test_push_sale_en_on_commit_jamas_dentro_de_la_transaccion(self):
        # Regresión del crítico PWA: el push NO debe dispararse con el lock de
        # la ingesta tomado — solo tras el commit (transaction.on_commit).
        from apps.pedidos.services import crear_pedido_manual

        def llamadas_piso(mock):
            return [c for c in mock.call_args_list if c.args and c.args[0] == "piso"]

        with patch("apps.inventario.services.reservar", return_value=True), \
             patch("apps.mensajeria.push.enviar_push_a_rol") as mock_push:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                crear_pedido_manual(
                    self.cliente, comprador_nombre="Karina",
                    direccion={"zip": "01780"}, cp="01780",
                    lineas=[(self.sku, 1)], actor="mesa1",
                )
                # Dentro de la transacción: cero push del piso disparados.
                self.assertEqual(llamadas_piso(mock_push), [])
            self.assertGreaterEqual(len(callbacks), 1)
            for callback in callbacks:
                callback()
            self.assertEqual(len(llamadas_piso(mock_push)), 1)

    def test_si_el_push_truena_el_pedido_se_crea_igual(self):
        from apps.pedidos.models import Pedido
        from apps.pedidos.services import crear_pedido_manual

        with patch(
            "apps.mensajeria.push.enviar_push_a_rol",
            side_effect=RuntimeError("sin red"),
        ), self.captureOnCommitCallbacks(execute=True):
            pedido = crear_pedido_manual(
                self.cliente, comprador_nombre="Karina",
                direccion={"zip": "01780"}, cp="01780",
                lineas=[(self.sku, 1)], actor="mesa1",
            )
        self.assertTrue(Pedido.objects.filter(pk=pedido.pk).exists())


class PWATests(TestCase):
    """Las piezas visibles de la PWA: sw.js, manifest en base y botón de opt-in."""

    def test_sw_js_responde_javascript_en_la_raiz(self):
        respuesta = self.client.get("/sw.js")
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta["Content-Type"].startswith("application/javascript"))
        contenido = respuesta.content.decode("utf-8")
        self.assertIn("push", contenido)
        self.assertIn("notificationclick", contenido)
        self.assertIn("Sin conexión", contenido)
        # El navegador siempre revalida el SW y la versión del shell viene
        # estampada del servidor (mtime de static/), no hardcodeada.
        self.assertEqual(respuesta["Cache-Control"], "no-cache")
        self.assertNotIn('"torre-shell-v1"', contenido)
        self.assertIn("torre-shell-", contenido)

    def test_sw_js_no_cachea_entrar_en_el_shell(self):
        # /entrar/ con CSRF congelado en caché rompía el login al volver la red.
        contenido = self.client.get("/sw.js").content.decode("utf-8")
        self.assertNotIn("/entrar/", contenido)
        # El shell sí trae los estáticos nuevos del rediseño.
        self.assertIn("/static/js/push.js", contenido)
        self.assertIn("questrial", contenido)

    def test_manifest_linkeado_en_base(self):
        respuesta = self.client.get(reverse("core:login"))
        self.assertContains(respuesta, "manifest.webmanifest")
        self.assertContains(respuesta, "theme-color")

    def test_boton_de_notificaciones_en_piso_home(self):
        crear_usuario("piso1", "piso", password="colima-pwa")
        self.client.login(username="piso1", password="colima-pwa")
        respuesta = self.client.get(reverse("piso:home"))
        self.assertContains(respuesta, "btn-push")
        self.assertContains(respuesta, "Activar notificaciones")
