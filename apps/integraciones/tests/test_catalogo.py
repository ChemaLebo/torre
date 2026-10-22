"""Pull de catálogo desde Shopify (Chema 2026-10-01): productos nuevos nacen
con el peso y las medidas de Shopify (activos si traen peso, inactivos "por
completar" si no; 2026-10-21); en los existentes solo se siguen nombre y
código de SKU, lo físico jamás se toca en actualizaciones."""
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios.tests.base import crear_cliente, crear_tienda
from apps.integraciones.models import SyncLog
from apps.integraciones.services import sincronizar_catalogo


def variante(vid, sku, producto, titulo="Default Title", barcode="", precio="0", estado="ACTIVE"):
    return {"variant_id": vid, "sku": sku, "titulo_variante": titulo, "producto": producto, "tipo": "Cerveza",
            "estado": estado, "codigo_barras": barcode, "precio": precio}


def ficha(vid, sku, peso_gr=0, medidas=(0, 0, 0), con_medidas=True, **extra):
    """Lo que regresa ShopifyClient.fichas_variantes para una variante."""
    return {**variante(vid, sku, extra.get("producto", sku)), "peso_gr": peso_gr,
            "largo_cm": medidas[0], "ancho_cm": medidas[1], "alto_cm": medidas[2], "con_medidas": con_medidas}


class CatalogoTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba")
        self.existente = SKU.objects.create(cliente=self.cliente, codigo="C24CC6L", descripcion="24 pack línea", peso_gr=14700,
                                            largo_cm=40, ancho_cm=30, alto_cm=26, precio_declarado=Decimal("769"))

    def _pull(self, variantes, fichas=None, error_fichas=None):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            api.catalogo.return_value = variantes
            if error_fichas is not None:
                api.fichas_variantes.side_effect = error_fichas
            else:
                api.fichas_variantes.return_value = fichas or {}
            resumen = sincronizar_catalogo(self.tienda)
            self.ids_pedidos = [list(c.args[0]) for c in api.fichas_variantes.call_args_list]
            return resumen

    def test_producto_nuevo_sin_peso_en_shopify_nace_inactivo_por_completar(self):
        resumen = self._pull([
            variante("1", "C24CC6L", "24 PACK CERVEZAS DE LÍNEA", precio="1099"),
            variante("2", "12NZP", "12 PACK NZ PILS", titulo="Botella 355 ml", barcode="750100", precio="420", estado="DRAFT"),
        ], fichas={"2": ficha("2", "12NZP")})
        self.assertEqual((resumen["nuevos"], resumen["activos"], resumen["ligados"], resumen["variantes"]), (1, 0, 1, 2))
        self.assertEqual(self.ids_pedidos, [["2"]])  # la ficha solo se pide para lo que va a nacer
        nuevo = SKU.objects.get(cliente=self.cliente, codigo="12NZP")
        self.assertEqual((nuevo.activo, nuevo.descripcion, nuevo.variante, nuevo.codigo_barras, nuevo.precio_declarado),
                         (False, "12 PACK NZ PILS", "Botella 355 ml", "750100", Decimal("420")))
        self.assertEqual((nuevo.peso_gr, nuevo.largo_cm, nuevo.shopify_variant_id), (0, 0, "2"))  # sin peso allá: lo captura Mesa
        self.existente.refresh_from_db()
        self.assertEqual(self.existente.shopify_variant_id, "1")
        self.assertEqual(self.existente.precio_declarado, Decimal("769"))  # lo existente no se pisa con el precio
        evento = EventoAuditoria.objects.get(entidad="sku", entidad_id="12NZP", accion="sku_creado_desde_shopify")
        self.assertEqual((evento.delta["peso_gr"], evento.delta["activo"]), (0, False))
        self.assertIn("1 nuevas (0 activas con peso de Shopify, 1 por completar)", SyncLog.objects.latest("ts").detalle)
        # Segundo pull igual: nada nuevo ni ficha que pedir.
        resumen = self._pull([variante("1", "C24CC6L", "24 PACK CERVEZAS DE LÍNEA"), variante("2", "12NZP", "12 PACK NZ PILS", titulo="Botella 355 ml")])
        self.assertEqual((resumen["nuevos"], resumen["renombrados"], resumen["recodificados"], self.ids_pedidos), (0, 0, 0, []))

    def test_producto_nuevo_con_peso_y_medidas_nace_activo(self):
        """Chema 2026-10-21: Soda Mate captura peso y medidas en Shopify; con peso el SKU nace listo."""
        resumen = self._pull(
            [variante("5", "SM24LOR", "SODA MATE ORIGINAL LATA | 24 Pack", barcode="7503049667126", precio="1000.00")],
            fichas={"5": ficha("5", "SM24LOR", peso_gr=15000, medidas=(40, 26, 25))},
        )
        self.assertEqual((resumen["nuevos"], resumen["activos"]), (1, 1))
        nuevo = SKU.objects.get(cliente=self.cliente, codigo="SM24LOR")
        self.assertEqual((nuevo.activo, nuevo.peso_gr, nuevo.largo_cm, nuevo.ancho_cm, nuevo.alto_cm, nuevo.codigo_barras),
                         (True, 15000, 40, 26, 25, "7503049667126"))
        evento = EventoAuditoria.objects.get(entidad="sku", entidad_id="SM24LOR", accion="sku_creado_desde_shopify")
        self.assertEqual((evento.delta["peso_gr"], evento.delta["medidas_cm"], evento.delta["activo"]), (15000, [40, 26, 25], True))
        self.assertIn("nace activa", evento.motivo)
        self.assertIn("1 nuevas (1 activas con peso de Shopify, 0 por completar)", SyncLog.objects.latest("ts").detalle)
        self.assertEqual(SyncLog.objects.filter(resultado=SyncLog.RESULTADO_ERROR).count(), 0)

    def test_sin_ficha_el_alta_sigue_inactiva_y_queda_anotado(self):
        resumen = self._pull([variante("5", "SM24LOR", "SODA MATE")], error_fichas=RuntimeError("502"))
        self.assertEqual((resumen["nuevos"], resumen["activos"]), (1, 0))
        nuevo = SKU.objects.get(cliente=self.cliente, codigo="SM24LOR")
        self.assertEqual((nuevo.activo, nuevo.peso_gr), (False, 0))
        error = SyncLog.objects.filter(resultado=SyncLog.RESULTADO_ERROR).get()
        self.assertIn("sin peso ni medidas para 1 variante(s) nueva(s): 502", error.detalle)

    def test_api_sin_medidas_nace_activa_solo_con_peso_y_queda_anotado(self):
        resumen = self._pull(
            [variante("5", "SM24LOR", "SODA MATE")],
            fichas={"5": ficha("5", "SM24LOR", peso_gr=15000, con_medidas=False)},
        )
        self.assertEqual((resumen["nuevos"], resumen["activos"]), (1, 1))
        nuevo = SKU.objects.get(cliente=self.cliente, codigo="SM24LOR")
        self.assertEqual((nuevo.activo, nuevo.peso_gr, nuevo.largo_cm), (True, 15000, 0))
        self.assertIn("no dio medidas empacadas", SyncLog.objects.filter(resultado=SyncLog.RESULTADO_ERROR).get().detalle)

    def test_cambio_de_nombre_y_de_codigo_se_siguen_por_la_variante(self):
        self._pull([variante("1", "C24CC6L", "24 pack línea")])
        self.existente.peso_gr = 14700
        resumen = self._pull(
            [variante("1", "C24CC6L-NUEVO", "24 PACK CERVEZAS DE LÍNEA BOTELLA 355 ML", titulo="Caja")],
            fichas={"1": ficha("1", "C24CC6L-NUEVO", peso_gr=99, medidas=(9, 9, 9))},  # aunque viniera, en actualización no se usa
        )
        self.assertEqual((resumen["renombrados"], resumen["recodificados"], self.ids_pedidos), (1, 1, []))
        self.existente.refresh_from_db()
        self.assertEqual((self.existente.codigo, self.existente.descripcion, self.existente.variante),
                         ("C24CC6L-NUEVO", "24 PACK CERVEZAS DE LÍNEA BOTELLA 355 ML", "Caja"))
        self.assertEqual((self.existente.peso_gr, self.existente.largo_cm), (14700, 40))  # lo físico intacto
        evento = EventoAuditoria.objects.get(entidad="sku", accion="sku_actualizado_desde_shopify")
        self.assertEqual(evento.delta["codigo"], ["C24CC6L", "C24CC6L-NUEVO"])

    def test_sin_token_o_shopify_caido_no_rompe(self):
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        self.assertEqual(sincronizar_catalogo(self.tienda)["variantes"], 0)
        self.tienda.token = "shpat_prueba"
        self.tienda.save(update_fields=["token"])
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.catalogo.side_effect = RuntimeError("502")
            self.assertEqual(sincronizar_catalogo(self.tienda)["nuevos"], 0)
        self.assertEqual(SyncLog.objects.latest("ts").resultado, SyncLog.RESULTADO_ERROR)

    def test_sync_shopify_lo_corre_y_mesa_avisa_los_por_completar(self):
        from apps.core.models import PerfilUsuario
        from django.contrib.auth import get_user_model

        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            cliente_cls.return_value.catalogo.return_value = [variante("9", "NUEVO-1", "Producto nuevo")]
            cliente_cls.return_value.fichas_variantes.return_value = {}
            cliente_cls.return_value.obtener_pedidos.return_value = []
            cliente_cls.return_value.pedidos_actualizados.return_value = []
            call_command("sync_shopify", verbosity=0)
        self.assertTrue(SKU.objects.filter(codigo="NUEVO-1", activo=False).exists())
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        html = self.client.get(reverse("mesa:cliente_skus", args=[self.cliente.pk])).content.decode()
        self.assertIn("por completar", html)
        self.assertIn("NUEVO-1", html)
