"""Link al pedido del portal en la orden de Shopify (Chema 2026-09-23): el
metafield `torre.pedido_url` que servicio al cliente abre desde el admin; se
escribe al ingerir (best-effort) y por backfill con el command."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.integraciones.models import SyncLog
from apps.integraciones.services import crear_definicion_link, escribir_link_pedido, url_pedido_portal
from apps.integraciones.shopify import ShopifyClient, ShopifyError

BASE = "https://torre.ejemplo.mx"


@override_settings(DEBUG=False)
@patch.dict("os.environ", {"BASE_URL_PUBLICA": BASE + "/"})
class EscribirLinkPedidoTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba")
        self.pedido = crear_pedido(self.cliente, self.tienda, cp="06600")
        self.pedido.shopify_order_id = "5479812345678"
        self.pedido.save(update_fields=["shopify_order_id"])

    def test_la_url_es_la_del_portal_con_la_base_publica(self):
        self.assertEqual(
            url_pedido_portal(self.pedido),
            BASE + reverse("portal:pedido_detalle", args=[self.pedido.pk]),
        )

    def test_escribe_el_metafield_en_la_orden(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.set_metafield_orden.return_value = "gid://shopify/Metafield/1"
            self.assertTrue(escribir_link_pedido(self.pedido))
        api.set_metafield_orden.assert_called_once_with(
            "5479812345678", "torre", "pedido_url", "url", url_pedido_portal(self.pedido),
        )
        log = SyncLog.objects.get()
        self.assertEqual((log.direccion, log.resultado), (SyncLog.DIRECCION_PUSH, SyncLog.RESULTADO_OK))
        self.assertIn(self.pedido.folio, log.detalle)

    def test_error_de_shopify_queda_en_synclog_y_no_levanta(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.set_metafield_orden.side_effect = ShopifyError("metafieldsSet: INVALID_VALUE")
            self.assertFalse(escribir_link_pedido(self.pedido))
        log = SyncLog.objects.get()
        self.assertEqual(log.resultado, SyncLog.RESULTADO_ERROR)
        self.assertIn("INVALID_VALUE", log.detalle)

    def test_sin_token_en_produccion_es_error_visible(self):
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        self.assertFalse(escribir_link_pedido(self.pedido))
        self.assertEqual(SyncLog.objects.get().resultado, SyncLog.RESULTADO_ERROR)

    def test_pedido_sin_orden_de_shopify_no_hace_nada(self):
        self.pedido.shopify_order_id = ""
        self.pedido.save(update_fields=["shopify_order_id"])
        self.assertFalse(escribir_link_pedido(self.pedido))
        self.assertFalse(SyncLog.objects.exists())

    def test_definicion_creada_o_existente(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.crear_definicion_metafield.return_value = "gid://shopify/MetafieldDefinition/7"
            self.assertEqual(crear_definicion_link(self.tienda), "creada")
            api.crear_definicion_metafield.return_value = None
            self.assertEqual(crear_definicion_link(self.tienda), "existia")
        definicion = api.crear_definicion_metafield.call_args.args[0]
        self.assertEqual(
            (definicion["namespace"], definicion["key"], definicion["type"], definicion["ownerType"], definicion["pin"]),
            ("torre", "pedido_url", "url", "ORDER", True),
        )
        self.assertEqual(SyncLog.objects.filter(resultado=SyncLog.RESULTADO_OK).count(), 2)


class ClienteMetafieldTests(TestCase):
    def setUp(self):
        self.tienda = crear_tienda(crear_cliente(), token="shpat_prueba")
        self.api = ShopifyClient(self.tienda)

    def test_set_metafield_orden_manda_el_gid_y_regresa_el_id(self):
        respuesta = {"metafieldsSet": {
            "metafields": [{"id": "gid://shopify/Metafield/9", "namespace": "torre", "key": "pedido_url", "value": BASE}],
            "userErrors": [],
        }}
        with patch.object(self.api, "graphql", return_value=respuesta) as gql:
            mid = self.api.set_metafield_orden("5479812345678", "torre", "pedido_url", "url", BASE)
        self.assertEqual(mid, "gid://shopify/Metafield/9")
        entrada = gql.call_args.args[1]["metafields"][0]
        self.assertEqual(entrada["ownerId"], "gid://shopify/Order/5479812345678")
        self.assertEqual((entrada["type"], entrada["value"]), ("url", BASE))

    def test_user_errors_levantan(self):
        respuesta = {"metafieldsSet": {"metafields": [], "userErrors": [
            {"field": ["metafields", "0", "value"], "message": "Value is not a valid URL", "code": "INVALID_VALUE"},
        ]}}
        with patch.object(self.api, "graphql", return_value=respuesta), self.assertRaises(ShopifyError) as ctx:
            self.api.set_metafield_orden("1", "torre", "pedido_url", "url", "no-es-url")
        self.assertIn("INVALID_VALUE", str(ctx.exception))

    def test_definicion_ya_existente_no_es_error(self):
        respuesta = {"metafieldDefinitionCreate": {"createdDefinition": None, "userErrors": [
            {"field": ["definition", "key"], "message": "Key is in use for this namespace and owner type.", "code": "TAKEN"},
        ]}}
        with patch.object(self.api, "graphql", return_value=respuesta):
            self.assertIsNone(self.api.crear_definicion_metafield({"key": "pedido_url"}))
        respuesta = {"metafieldDefinitionCreate": {"createdDefinition": {"id": "gid://shopify/MetafieldDefinition/7", "name": "Pedido en Torre"}, "userErrors": []}}
        with patch.object(self.api, "graphql", return_value=respuesta):
            self.assertEqual(self.api.crear_definicion_metafield({"key": "pedido_url"}), "gid://shopify/MetafieldDefinition/7")


@patch.dict("os.environ", {"BASE_URL_PUBLICA": BASE})
class CommandMetafieldTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba")
        self.pedido = crear_pedido(self.cliente, self.tienda, cp="06600")
        self.manual = crear_pedido(self.cliente, None, shopify_order_id="", origen="manual")

    def _correr(self, *args):
        salida = StringIO()
        call_command("shopify_metafield_torre", *args, stdout=salida)
        return salida.getvalue()

    def test_crear_definicion_por_tienda(self):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.crear_definicion_metafield.return_value = "gid://shopify/MetafieldDefinition/1"
            salida = self._correr("--crear-definicion")
        self.assertIn(f"{self.tienda.dominio}: definición creada y fijada", salida)

    def test_backfill_sin_aplicar_solo_imprime_el_plan(self):
        with patch("apps.integraciones.management.commands.shopify_metafield_torre.escribir_link_pedido") as link:
            salida = self._correr("--backfill")
        link.assert_not_called()
        self.assertIn(self.pedido.folio, salida)
        self.assertNotIn(self.manual.folio, salida)
        self.assertIn("--aplicar", salida)

    def test_backfill_aplica_solo_a_pedidos_con_orden(self):
        with patch("apps.integraciones.management.commands.shopify_metafield_torre.escribir_link_pedido",
                   return_value=True) as link:
            salida = self._correr("--backfill", "--aplicar")
        link.assert_called_once_with(self.pedido)
        self.assertIn("Link escrito en 1 orden(es); 0 con error.", salida)

    def test_backfill_por_folio(self):
        otro = crear_pedido(self.cliente, self.tienda, cp="06600")
        with patch("apps.integraciones.management.commands.shopify_metafield_torre.escribir_link_pedido",
                   return_value=True) as link:
            self._correr("--backfill", "--aplicar", "--folio", otro.folio)
        link.assert_called_once_with(otro)
