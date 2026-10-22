"""Ficha física de variantes (Chema 2026-10-21): peso y medidas empacadas de
Shopify en gramos y centímetros, pedidas en la versión 2027-01 con respaldo
sin medidas en la versión configurada."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.envios.tests.base import crear_cliente, crear_tienda
from apps.integraciones.shopify import ShopifyClient, ShopifyError, centimetros_de, gramos_de


def nodo(vid, sku, peso=None, medidas=None, **extra):
    medicion = {"weight": peso}
    if medidas is not None:
        medicion["packedDimensions"] = medidas
    return {
        "id": f"gid://shopify/ProductVariant/{vid}", "sku": sku, "title": extra.get("titulo", "Default Title"),
        "barcode": extra.get("barcode", ""), "price": extra.get("precio", "0"),
        "product": {"title": extra.get("producto", "Producto"), "status": "ACTIVE", "productType": "Bebida"},
        "inventoryItem": {"measurement": medicion},
    }


class ConversionesTests(TestCase):
    def test_peso_a_gramos(self):
        self.assertEqual(gramos_de({"value": 15.0, "unit": "KILOGRAMS"}), 15000)
        self.assertEqual(gramos_de({"value": 355, "unit": "GRAMS"}), 355)
        self.assertEqual(gramos_de({"value": 12, "unit": "OUNCES"}), 340)
        self.assertEqual(gramos_de({"value": 2.5, "unit": "POUNDS"}), 1134)
        self.assertEqual(gramos_de(None), 0)
        self.assertEqual(gramos_de({"value": 0, "unit": "GRAMS"}), 0)
        self.assertEqual(gramos_de({"value": "x", "unit": "GRAMS"}), 0)
        self.assertEqual(gramos_de({"value": 3, "unit": "STONES"}), 0)

    def test_medidas_a_centimetros_hacia_arriba(self):
        self.assertEqual(centimetros_de({"length": 40.0, "width": 26.0, "height": 25.0, "unit": "CENTIMETERS"}), (40, 26, 25))
        self.assertEqual(centimetros_de({"length": 10, "width": 4.5, "height": 1, "unit": "INCHES"}), (26, 12, 3))
        self.assertEqual(centimetros_de({"length": 25.4, "width": 0, "height": 0.2, "unit": "CENTIMETERS"}), (26, 0, 1))
        self.assertEqual(centimetros_de({"length": 0, "width": 0, "height": 0, "unit": "CENTIMETERS"}), (0, 0, 0))
        self.assertEqual(centimetros_de(None), (0, 0, 0))
        self.assertEqual(centimetros_de({"length": 1, "width": 1, "height": 1, "unit": "CUBITS"}), (0, 0, 0))


@override_settings(SHOPIFY_API_VERSION="2026-07")
class FichasVariantesTests(TestCase):
    def setUp(self):
        self.tienda = crear_tienda(crear_cliente(), token="shpat_prueba")
        self.api = ShopifyClient(self.tienda)

    def test_fichas_en_2027_01_con_peso_y_medidas(self):
        llamadas = []

        def graphql(query, variables=None, version=None):
            llamadas.append((version, "packedDimensions" in query, variables))
            return {"nodes": [
                nodo("1", "SM24LOR", {"value": 15.0, "unit": "KILOGRAMS"}, {"length": 40.0, "width": 26.0, "height": 25.0, "unit": "CENTIMETERS"},
                     barcode="7503049667126", precio="1000.00", producto="SODA MATE ORIGINAL LATA | 24 Pack"),
                nodo("2", "SINPESO", None, {"length": 0, "width": 0, "height": 0, "unit": "CENTIMETERS"}),
                None,
            ]}

        with patch.object(ShopifyClient, "graphql", side_effect=graphql):
            fichas = self.api.fichas_variantes(["1", "gid://shopify/ProductVariant/2"])
        self.assertEqual(llamadas, [("2027-01", True, {"ids": ["gid://shopify/ProductVariant/1", "gid://shopify/ProductVariant/2"]})])
        self.assertEqual(
            {k: fichas["1"][k] for k in ("sku", "codigo_barras", "precio", "producto", "peso_gr", "largo_cm", "ancho_cm", "alto_cm", "con_medidas")},
            {"sku": "SM24LOR", "codigo_barras": "7503049667126", "precio": "1000.00", "producto": "SODA MATE ORIGINAL LATA | 24 Pack",
             "peso_gr": 15000, "largo_cm": 40, "ancho_cm": 26, "alto_cm": 25, "con_medidas": True},
        )
        self.assertEqual((fichas["2"]["peso_gr"], fichas["2"]["largo_cm"], fichas["2"]["con_medidas"]), (0, 0, True))

    def test_si_2027_01_falla_repite_sin_medidas_en_la_version_configurada(self):
        llamadas = []

        def graphql(query, variables=None, version=None):
            llamadas.append((version, "packedDimensions" in query))
            if version == "2027-01":
                raise ShopifyError("Field 'packedDimensions' doesn't exist")
            return {"nodes": [nodo("1", "SM24LOR", {"value": 15000, "unit": "GRAMS"})]}

        with patch.object(ShopifyClient, "graphql", side_effect=graphql):
            fichas = self.api.fichas_variantes(["1"])
        self.assertEqual(llamadas, [("2027-01", True), (None, False)])
        self.assertEqual((fichas["1"]["peso_gr"], fichas["1"]["largo_cm"], fichas["1"]["con_medidas"]), (15000, 0, False))

    def test_version_configurada_mas_nueva_manda(self):
        with override_settings(SHOPIFY_API_VERSION="2027-04"):
            self.assertEqual(ShopifyClient(self.tienda)._version_medidas(), "2027-04")
        with override_settings(SHOPIFY_API_VERSION="unstable"):
            self.assertEqual(ShopifyClient(self.tienda)._version_medidas(), "unstable")
        self.assertEqual(self.api._version_medidas(), "2027-01")

    def test_ficha_por_codigo_exige_match_exacto(self):
        def graphql(query, variables=None, version=None):
            self.assertEqual(variables, {"consulta": 'sku:"SM24LOR"'})
            return {"productVariants": {"edges": [
                {"node": nodo("9", "SM24LOR-X", {"value": 1, "unit": "KILOGRAMS"})},
                {"node": nodo("1", "SM24LOR", {"value": 15, "unit": "KILOGRAMS"}, {"length": 40, "width": 26, "height": 25, "unit": "CENTIMETERS"})},
            ]}}

        with patch.object(ShopifyClient, "graphql", side_effect=graphql):
            ficha = self.api.ficha_por_codigo("SM24LOR")
            self.assertEqual((ficha["variant_id"], ficha["peso_gr"], ficha["alto_cm"]), ("1", 15000, 25))
        with patch.object(ShopifyClient, "graphql", return_value={"productVariants": {"edges": []}}):
            self.assertIsNone(self.api.ficha_por_codigo("SM24LOR"))

    def test_graphql_cambia_de_version_solo_en_esa_llamada(self):
        urls = []

        class Respuesta:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"ok": True}}

        def post(url, json=None, timeout=None):
            urls.append(url)
            return Respuesta()

        with patch.object(self.api.sesion, "post", side_effect=post):
            self.api.graphql("query { shop { name } }", version="2027-01")
            self.api.graphql("query { shop { name } }")
        self.assertEqual(urls, [
            f"https://{self.tienda.dominio}/admin/api/2027-01/graphql.json",
            f"https://{self.tienda.dominio}/admin/api/2026-07/graphql.json",
        ])
