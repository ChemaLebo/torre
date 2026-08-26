"""Transformador puro del products_export.csv de Shopify → CSV de Torre."""
from django.test import SimpleTestCase

from ..transformador import COLUMNAS_TORRE, filas_a_csv, transformar_export_shopify

ENCABEZADO = (
    "Handle,Title,Type,Option1 Name,Option1 Value,Option2 Name,Option2 Value,"
    "Option3 Name,Option3 Value,Variant SKU,Variant Grams,Variant Price,Variant Barcode"
)


def _csv(*filas):
    return "\n".join((ENCABEZADO,) + filas) + "\n"


class TransformadorTests(SimpleTestCase):
    def test_forward_fill_de_titulo_y_type_por_handle(self):
        resultado = transformar_export_shopify(_csv(
            "te-negro,Té negro,Tés,Tamaño,500 g,,,,,TN-500,520,250.00,750111",
            "te-negro,,,Tamaño,1 kg,,,,,TN-1K,1050,450.00,750222",
        ))
        por_codigo = {f["codigo"]: f for f in resultado["filas"]}
        self.assertEqual(por_codigo["TN-1K"]["descripcion"], "Té negro")
        self.assertEqual(por_codigo["TN-1K"]["categoria"], "Tés")
        self.assertEqual(por_codigo["TN-1K"]["variante"], "1 kg")

    def test_default_title_no_es_variante(self):
        resultado = transformar_export_shopify(_csv(
            "chai,Chai masala,Tés,Title,Default Title,,,,,CHAI,300,199.00,750333",
        ))
        self.assertEqual(resultado["filas"][0]["variante"], "")

    def test_multiples_options_se_juntan(self):
        resultado = transformar_export_shopify(_csv(
            "te,Té verde,Tés,Tamaño,500 g,Molienda,Molido,,,TV-500M,500,300.00,",
        ))
        self.assertEqual(resultado["filas"][0]["variante"], "500 g / Molido")

    def test_variante_sin_sku_avisa_y_fila_de_imagen_calla(self):
        resultado = transformar_export_shopify(_csv(
            "te-negro,Té negro,Tés,Tamaño,500 g,,,,,TN-500,520,250.00,",
            "te-negro,,,,,,,,,,,,",          # fila de imagen: todo vacío → silencio
            "sin-sku,Sin SKU,Tés,Title,Default Title,,,,,,100,99.00,",  # variante real sin SKU
        ))
        self.assertEqual(len(resultado["filas"]), 1)
        avisos = " | ".join(resultado["avisos"])
        self.assertIn("sin SKU", avisos)
        self.assertNotIn("fila 3", avisos)  # la de imagen no ensucia el reporte

    def test_sku_repetido_conserva_el_primero(self):
        resultado = transformar_export_shopify(_csv(
            "a,Primero,Tés,Title,Default Title,,,,,REP-1,100,10.00,",
            "b,Segundo,Tés,Title,Default Title,,,,,REP-1,999,99.00,",
        ))
        self.assertEqual(len(resultado["filas"]), 1)
        self.assertEqual(resultado["filas"][0]["descripcion"], "Primero")
        self.assertIn("SKU repetido", resultado["avisos"][0])

    def test_barcode_compartido_avisa(self):
        resultado = transformar_export_shopify(_csv(
            "a,Uno,Tés,Title,Default Title,,,,,BC-1,100,10.00,750999",
            "b,Dos,Tés,Title,Default Title,,,,,BC-2,100,10.00,750999",
        ))
        self.assertTrue(any("750999" in a and "compartido" in a for a in resultado["avisos"]))

    def test_gramos_cero_queda_vacio(self):
        resultado = transformar_export_shopify(_csv(
            "a,Con peso,Tés,Title,Default Title,,,,,P-1,520,10.00,",
            "b,Sin peso,Tés,Title,Default Title,,,,,P-0,0,10.00,",
        ))
        por_codigo = {f["codigo"]: f for f in resultado["filas"]}
        self.assertEqual(por_codigo["P-1"]["peso_gr"], "520")
        self.assertEqual(por_codigo["P-0"]["peso_gr"], "")  # 0 = no capturado

    def test_csv_de_salida_trae_el_contrato_del_import(self):
        resultado = transformar_export_shopify(_csv(
            "a,Uno,Tés,Title,Default Title,,,,,X-1,100,10.00,",
        ))
        salida = filas_a_csv(resultado["filas"])
        self.assertTrue(salida.startswith(",".join(COLUMNAS_TORRE)))
        self.assertIn("X-1,Uno,,,Tés,100", salida)

    def test_archivo_ajeno_truena_claro(self):
        with self.assertRaises(ValueError):
            transformar_export_shopify("codigo,descripcion\nX,Y\n")
