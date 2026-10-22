"""Conectar con Shopify desde Torre (Chema 2026-10-21): la pantalla de
permisos se abre con la lista de scopes de Torre, el callback valida firma y
state, cambia el código por el token y deja la tienda operable (webhooks por
API, metafield, location, permisos). Shopify se simula: nada sale a la red."""
import hashlib
import hmac
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, EventoAuditoria
from apps.integraciones import oauth
from apps.integraciones.models import Tienda


def firmar(params, secret):
    """La firma que Shopify manda: HMAC-SHA256 hex de los parámetros sin hmac, ordenados, unidos con &."""
    mensaje = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    return hmac.new(secret.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


def graphql_simulado(query, variables=None):
    if "webhookSubscriptions(" in query:
        return {"webhookSubscriptions": {"edges": []}}
    if "webhookSubscriptionCreate" in query:
        return {"webhookSubscriptionCreate": {"webhookSubscription": {"id": f"gid://shopify/WebhookSubscription/{variables['topic']}"}, "userErrors": []}}
    if "locations(" in query:
        return {"locations": {"edges": [
            {"node": {"id": "gid://shopify/Location/77", "name": "WOP Fulfillment", "isActive": True}},
            {"node": {"id": "gid://shopify/Location/1", "name": "Tienda Centro", "isActive": True}},
        ]}}
    if "currentAppInstallation" in query:
        return {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in oauth.SCOPES_TORRE]}}
    raise AssertionError(f"consulta inesperada: {query}")


class OAuthShopifyTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Soda Mate", slug="sodamate", integracion_envios="envia")
        self.tienda = Tienda.objects.create(
            cliente=self.cliente, dominio="sodamate.myshopify.com", client_id="cid123", client_secret="secreto-app",
        )

    def _params_callback(self, **extra):
        oauth.iniciar_conexion(self.tienda)
        self.tienda.refresh_from_db()
        params = {"code": "codigo-1", "shop": "sodamate.myshopify.com", "state": self.tienda.oauth_state, "timestamp": "1700000000"}
        params.update(extra)
        params["hmac"] = firmar(params, "secreto-app")
        return params

    def test_iniciar_conexion_arma_la_url_y_guarda_el_state(self):
        url = oauth.iniciar_conexion(self.tienda)
        self.tienda.refresh_from_db()
        self.assertTrue(self.tienda.oauth_state)
        self.assertTrue(url.startswith("https://admin.shopify.com/store/sodamate/oauth/authorize?"))  # sin token: admin unificado
        self.assertIn("client_id=cid123", url)
        self.assertIn("write_merchant_managed_fulfillment_orders", url)
        self.assertNotIn("read_all_orders", url)
        self.assertIn(f"state={self.tienda.oauth_state}", url)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A8380%2Foauth%2Fcallback", url)
        self.tienda.read_all_orders = True
        self.tienda.save()
        self.assertIn("read_all_orders", oauth.iniciar_conexion(self.tienda))
        # Con token (actualizar permisos): la forma clásica probada con Colima.
        self.tienda.token = "shpat_x"
        self.tienda.save()
        self.assertTrue(oauth.iniciar_conexion(self.tienda).startswith("https://sodamate.myshopify.com/admin/oauth/authorize?"))

    def test_sin_app_no_se_puede_iniciar(self):
        self.tienda.client_secret = ""
        self.tienda.save()
        with self.assertRaisesMessage(ValueError, "client secret"):
            oauth.iniciar_conexion(self.tienda)

    def test_firma_valida_rechaza_secret_ajeno_y_params_alterados(self):
        params = {"code": "abc", "shop": "sodamate.myshopify.com", "state": "n1", "timestamp": "1700000000", "host": "aG9zdA"}
        params["hmac"] = firmar(params, "secreto-app")
        self.assertTrue(oauth.firma_valida(params, "secreto-app"))
        self.assertFalse(oauth.firma_valida(params, "otro"))
        self.assertFalse(oauth.firma_valida(dict(params, code="zzz"), "secreto-app"))
        self.assertFalse(oauth.firma_valida({k: v for k, v in params.items() if k != "hmac"}, "secreto-app"))

    def test_callback_guarda_el_token_y_deja_la_tienda_operable(self):
        params = self._params_callback()
        respuesta_token = MagicMock(status_code=200)
        respuesta_token.json.return_value = {"access_token": "shpat_nuevo", "scope": ",".join(oauth.SCOPES_TORRE)}
        with patch("apps.integraciones.oauth.requests.post", return_value=respuesta_token) as post, \
                patch("apps.integraciones.oauth.ShopifyClient") as cliente_cls, \
                patch("apps.integraciones.services.crear_definicion_link", return_value="creada") as definicion:
            cliente_cls.return_value.graphql.side_effect = graphql_simulado
            respuesta = self.client.get(reverse("oauth:callback"), params)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Torre quedó conectado")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"client_id": "cid123", "client_secret": "secreto-app", "code": "codigo-1"})
        self.tienda.refresh_from_db()
        self.assertEqual(
            (self.tienda.token, self.tienda.oauth_state, self.tienda.webhook_secret, self.tienda.location_id),
            ("shpat_nuevo", "", "secreto-app", "gid://shopify/Location/77"),
        )
        self.assertIsNotNone(self.tienda.ts_conectada)
        self.assertIn("write_orders", self.tienda.scopes)
        definicion.assert_called_once()
        creados = [c for c in cliente_cls.return_value.graphql.call_args_list if "webhookSubscriptionCreate" in c.args[0]]
        self.assertEqual(len(creados), 4)
        self.assertEqual(creados[0].args[1]["sub"]["callbackUrl"], f"http://127.0.0.1:8380/hooks/shopify/{self.tienda.pk}/")
        evento = EventoAuditoria.objects.get(entidad="tienda", entidad_id="sodamate.myshopify.com", accion="post_conexion")
        self.assertEqual(
            (evento.delta["webhooks"], evento.delta["permisos_faltantes"], evento.delta["location_elegida"]),
            ("4 creados", [], "WOP Fulfillment"),
        )
        self.assertTrue(EventoAuditoria.objects.filter(entidad="tienda", entidad_id="sodamate.myshopify.com", accion="tienda_conectada").exists())
        deltas = str(list(EventoAuditoria.objects.filter(entidad="tienda").values_list("delta", flat=True)))
        self.assertNotIn("shpat_nuevo", deltas)
        self.assertNotIn("secreto-app", deltas)

    def test_callback_con_firma_mala_o_state_ajeno_no_toca_nada(self):
        with patch("apps.integraciones.oauth.requests.post") as post:
            params = self._params_callback()
            params["hmac"] = "0" * 64
            self.assertEqual(self.client.get(reverse("oauth:callback"), params).status_code, 403)
            params = self._params_callback()
            params["state"] = "ajeno"
            params["hmac"] = firmar(params, "secreto-app")
            self.assertEqual(self.client.get(reverse("oauth:callback"), params).status_code, 403)
            post.assert_not_called()
        self.tienda.refresh_from_db()
        self.assertEqual(self.tienda.token, "")

    def test_primera_instalacion_desde_el_link_del_dashboard_entra_sin_state(self):
        """Soda Mate 2026-10-21: una app custom se instala desde el link del Dashboard, que regresa sin el state de Torre."""
        params = {"code": "codigo-dash", "shop": "sodamate.myshopify.com", "timestamp": "1700000000", "host": "aG9zdA"}
        params["hmac"] = firmar(params, "secreto-app")
        respuesta_token = MagicMock(status_code=200)
        respuesta_token.json.return_value = {"access_token": "shpat_dash", "scope": "read_orders"}
        with patch("apps.integraciones.oauth.requests.post", return_value=respuesta_token), \
                patch("apps.integraciones.oauth.ShopifyClient") as cliente_cls, \
                patch("apps.integraciones.services.crear_definicion_link", return_value="creada"):
            cliente_cls.return_value.graphql.side_effect = graphql_simulado
            respuesta = self.client.get(reverse("oauth:callback"), params)
        self.assertEqual(respuesta.status_code, 200)
        self.tienda.refresh_from_db()
        self.assertEqual(self.tienda.token, "shpat_dash")
        evento = EventoAuditoria.objects.get(entidad="tienda", entidad_id="sodamate.myshopify.com", accion="tienda_conectada")
        self.assertTrue(evento.delta["desde_link_dashboard"])
        # Ya con token, una llegada sin state se rechaza: solo el botón de Torre puede reconectar.
        params["code"] = "otro"
        params["hmac"] = firmar(params, "secreto-app")
        with patch("apps.integraciones.oauth.requests.post") as post:
            self.assertEqual(self.client.get(reverse("oauth:callback"), params).status_code, 403)
            post.assert_not_called()

    def test_callback_de_tienda_desconocida_es_404(self):
        respuesta = self.client.get(reverse("oauth:callback"), {"shop": "otra.myshopify.com", "code": "x", "hmac": "y", "state": "z"})
        self.assertEqual(respuesta.status_code, 404)

    def test_webhooks_no_se_tocan_si_la_tienda_firma_con_otro_secret(self):
        """Colima e Infinitea: sus webhooks de Notificaciones siguen igual; migrar es decisión de Mesa."""
        self.tienda.webhook_secret = "secret-de-notificaciones"
        self.tienda.token = "shpat_viejo"
        self.tienda.save()
        api = MagicMock()
        api.graphql.side_effect = graphql_simulado
        with patch("apps.integraciones.oauth.ShopifyClient", return_value=api), \
                patch("apps.integraciones.services.crear_definicion_link", return_value="existia"):
            resumen = oauth.pasos_post_conexion(self.tienda)
        self.assertTrue(resumen["webhooks"].startswith("sin tocar"))
        self.assertFalse(any("webhookSubscription" in c.args[0] for c in api.graphql.call_args_list))
        self.tienda.refresh_from_db()
        self.assertEqual(self.tienda.webhook_secret, "secret-de-notificaciones")

    def test_el_link_para_el_cliente_abre_la_pantalla_de_permisos(self):
        link = oauth.link_de_conexion(self.tienda)
        self.assertTrue(link.startswith("http://127.0.0.1:8380/oauth/conectar/"))
        respuesta = self.client.get(link.replace("http://127.0.0.1:8380", ""))
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(respuesta["Location"].startswith("https://admin.shopify.com/store/sodamate/oauth/authorize?"))
        self.assertEqual(self.client.get("/oauth/conectar/firma-falsa/").status_code, 404)
