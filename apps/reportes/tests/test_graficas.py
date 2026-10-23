"""Gráficas SVG de los reportes (Chema 2026-10-22): barras por categoría,
columnas por periodo con un panel por serie, texto escapado, valores en las
puntas y nada cuando no hay datos o la spec no se entiende."""
from decimal import Decimal

from django.test import SimpleTestCase

from apps.reportes import graficas


class GraficasTests(SimpleTestCase):
    def test_barras_por_categoria(self):
        grupo = {
            "titulo": "Guías por zona", "columnas": [("Zona", "texto"), ("Guías", "entero"), ("%", "pct")],
            "filas": [["local", 12, 60.0], ["metro <b>", Decimal("8"), 40.0], ["sin número", None, None]],
            "grafica": {"tipo": "barras", "etiqueta": 0, "valor": 1, "serie": "guías"},
        }
        svg = graficas.svg(grupo)
        self.assertTrue(svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 '))
        self.assertIn('aria-label="Guías por zona"', svg)
        self.assertEqual(svg.count('<g class="marca">'), 2)  # la fila sin número no se dibuja
        self.assertIn("<title>metro &lt;b&gt;: 8 guías</title>", svg)  # escapado + tooltip nativo
        self.assertIn(">metro &lt;b&gt;</text>", svg)
        self.assertIn('class="valor">12</text>', svg)
        self.assertIn('class="serie-1"><path d="M150.0,', svg)  # barra con punta redondeada a la derecha
        self.assertNotIn("fill=\"#", svg)  # el color vive en el CSS por tema, no en el SVG

    def test_columnas_con_varias_series_son_paneles_separados(self):
        filas = [["01/oct", 5, 7, 900], ["02/oct", 3, 4, 1200], ["03/oct", 0, 0, 0]]
        grupo = {
            "titulo": "Volumen diario", "columnas": [("Día", "texto"), ("Pedidos", "entero"), ("Guías", "entero"), ("Piezas", "entero")],
            "filas": filas, "grafica": {"tipo": "columnas", "etiqueta": 0, "series": [(1, "Pedidos"), (2, "Guías"), (3, "Piezas")]},
        }
        svg = graficas.svg(grupo)
        self.assertIn(f'height="{graficas.PANEL_ALTO * 3}"', svg)  # un panel por serie, cada uno con su eje
        self.assertEqual(svg.count('class="leyenda"'), 3)
        self.assertIn(">Pedidos</text>", svg)
        self.assertIn(">Piezas</text>", svg)
        self.assertIn("<title>02/oct: 1,200 Piezas</title>", svg)
        self.assertEqual(svg.count('<g class="marca">'), 9)
        self.assertIn('class="valor" text-anchor="middle"'.replace(' text-anchor="middle"', ''), svg)  # el máximo lleva etiqueta

    def test_una_serie_sin_leyenda_y_ticks_redondos(self):
        grupo = {"titulo": "Pedidos por mes", "columnas": [("Mes", "texto"), ("Pedidos", "entero")],
                 "filas": [["2026-08", 120], ["2026-09", 340], ["2026-10", 95]],
                 "grafica": {"tipo": "columnas", "etiqueta": 0, "series": [(1, "Pedidos")]}}
        svg = graficas.svg(grupo)
        self.assertNotIn('class="leyenda"', svg)  # una sola serie: el título la nombra
        self.assertIn(">400</text>", svg)  # tope redondo arriba del máximo
        self.assertIn(">2026-10</text>", svg)

    def test_sin_datos_o_spec_rara_no_hay_svg(self):
        self.assertEqual(graficas.svg({"titulo": "x", "filas": [], "grafica": {"tipo": "barras", "etiqueta": 0, "valor": 1}}), "")
        self.assertEqual(graficas.svg({"titulo": "x", "filas": [["a", 1]]}), "")
        self.assertEqual(graficas.svg({"titulo": "x", "filas": [["a", 1]], "grafica": {"tipo": "pastel"}}), "")
        self.assertEqual(graficas.svg({"titulo": "x", "filas": [["a"]], "grafica": {"tipo": "barras", "etiqueta": 0, "valor": 5}}), "")

    def test_compacto_y_escala(self):
        self.assertEqual([graficas.compacto(v) for v in (0, 7, 1284, 12900, 4_200_000, Decimal("12.5"), None)],
                         ["0", "7", "1,284", "12.9K", "4.2M", "12.50", ""])
        self.assertEqual(graficas._escala(0), (1, [0, 1]))
        self.assertEqual(graficas._escala(95), (100, [0, 50, 100]))
        self.assertEqual(graficas._escala(1200), (1500, [0, 500.0, 1000.0, 1500.0]))
