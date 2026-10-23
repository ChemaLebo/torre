"""Motor de reportes (Chema 2026-10-22, para el reporte de fulfillment):
filtros text / multiselect / rango / solo_mesa, opciones dinámicas, CSV de
una sola tabla y celdas vacías para lo que no aplica."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from apps.reportes.base import filtros_desde_get, opciones_de

from .base import ReportesTestCase

DEFINICIONES = [
    {"nombre": "carrier", "etiqueta": "Paquetería", "tipo": "multiselect", "solo_mesa": True,
     "opciones": lambda: [("fedex", "FedEx"), ("estafeta", "Estafeta")]},
    {"nombre": "sku", "etiqueta": "SKU", "tipo": "text", "default": ""},
    {"nombre": "peso", "etiqueta": "Peso real kg", "tipo": "rango"},
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "select", "default": "", "opciones": [("", "Todas"), ("local", "Local")]},
    {"nombre": "solo_mesa_chk", "etiqueta": "Con costos", "tipo": "checkbox", "default": True, "solo_mesa": True},
]


class FiltrosTests(TestCase):
    def test_multiselect_text_y_rango_desde_un_querydict(self):
        get = QueryDict("carrier=fedex&carrier=estafeta&sku=%20SIX%20&peso_min=1.5&peso_max=abc&zona=local")
        valores = filtros_desde_get(DEFINICIONES, get)
        self.assertEqual(valores["carrier"], ["fedex", "estafeta"])
        self.assertEqual(valores["sku"], "SIX")
        self.assertEqual(valores["peso"], (Decimal("1.5"), None))  # el máximo inválido se ignora
        self.assertEqual(valores["zona"], "local")
        self.assertFalse(valores["solo_mesa_chk"])  # form enviado sin la casilla

    def test_sin_nada_elegido_es_todas_y_defaults(self):
        valores = filtros_desde_get(DEFINICIONES, QueryDict(""))
        self.assertEqual((valores["carrier"], valores["sku"], valores["peso"], valores["zona"]), ([], "", (None, None), ""))
        self.assertTrue(valores["solo_mesa_chk"])  # primera carga: default
        self.assertEqual(filtros_desde_get(DEFINICIONES, {"carrier": "fedex"})["carrier"], ["fedex"])  # dict plano también

    def test_fuera_de_mesa_los_filtros_solo_mesa_se_ignoran(self):
        get = QueryDict("carrier=fedex&solo_mesa_chk=on&sku=SIX")
        valores = filtros_desde_get(DEFINICIONES, get, es_mesa=False)
        self.assertEqual((valores["carrier"], valores["solo_mesa_chk"], valores["sku"]), ([], True, "SIX"))

    def test_opciones_estaticas_o_callable(self):
        self.assertEqual(opciones_de(DEFINICIONES[0]), [("fedex", "FedEx"), ("estafeta", "Estafeta")])
        self.assertEqual(opciones_de(DEFINICIONES[3]), [("", "Todas"), ("local", "Local")])
        self.assertEqual(opciones_de(DEFINICIONES[1]), [])


def _modulo_falso():
    """Un reporte mínimo con todos los tipos de filtro, un grupo y celdas None."""
    def generar(cliente, inicio, fin, filtros, es_mesa):
        return {
            "filas": [["PED-1", filtros["carrier"] and ",".join(filtros["carrier"]) or "todas", None, Decimal("12.50")]],
            "resumen": [("guías", 1)],
            "grupos": [
                {"titulo": "Por zona", "columnas": [("Zona", "texto"), ("Guías", "entero")], "filas": [["local", 1]]},
                {"titulo": "Por peso", "columnas": [("Rango", "texto"), ("Guías", "entero")], "filas": [["0-1 kg", None]]},
            ],
        }
    return SimpleNamespace(
        CLAVE="falso", TITULO="Reporte falso", DESCRIPCION="Para probar el motor.", CON_FECHAS=True,
        FILTROS=DEFINICIONES, COLUMNAS=[("Pedido", "texto"), ("Paqueterías", "texto"), ("Horas", "horas"), ("MXN", "dinero")],
        COLUMNAS_MESA=[], generar=generar,
    )


class VistasMotorTests(ReportesTestCase):
    def setUp(self):
        parche = patch("apps.reportes.views.modulo", side_effect=lambda clave: _modulo_falso() if clave == "falso" else None)
        parche.start()
        self.addCleanup(parche.stop)
        self.url = reverse("mesa:reporte", args=["falso"])
        self.url_csv = reverse("mesa:reporte_csv", args=["falso"])

    def test_pinta_multiselect_rango_text_y_celdas_vacias(self):
        self.client.force_login(self.mesa)
        respuesta = self.client.get(self.url, {"cliente": "colima", "carrier": ["fedex"], "peso_min": "2", "sku": "SIX"})
        html = respuesta.content.decode()
        # Multiselect = dropdown con casillas (Chema 2026-10-22): el resumen dice cuántas van marcadas.
        self.assertIn('<details class="desplegable" id="f-carrier">', html)
        self.assertIn("<summary>1 elegida</summary>", html)
        self.assertIn('<input type="checkbox" name="carrier" value="fedex" checked>', html)
        self.assertIn('<input type="checkbox" name="carrier" value="estafeta" >', html)
        self.assertNotIn("<select id=\"f-carrier\"", html)
        # Tarjetas plegables: el detalle abierto, los cortes como acordeón; totales por tabla.
        self.assertIn('<details class="colapsable mb" open>', html)
        self.assertEqual(html.count('<details class="colapsable mb" name="cortes">'), 2)
        self.assertIn("Detalle <span class=\"small muted\">· 1 renglón</span>", html)
        self.assertIn('<tr class="totales"><td class="">Total · 1 renglón</td><td class="">', html)
        self.assertIn('<td class="derecha mono">$12.50</td></tr></tfoot>', html)
        self.assertIn('<tr class="totales"><td class="">Total · 1 renglón</td><td class="derecha mono">1</td></tr>', html)  # grupo Por zona
        self.assertIn('<tr class="totales"><td class="">Total · 1 renglón</td><td class="derecha mono"></td></tr>', html)  # grupo Por peso: None no suma
        self.assertIn('name="peso_min" min="0" step="any" placeholder="mín." style="width:6.5em" value="2"', html)
        self.assertIn('name="sku" autocomplete="off" placeholder="" value="SIX"', html)
        self.assertIn('<td class="">fedex</td>', html)
        self.assertIn('<td class="derecha mono"></td>', html)  # None = celda vacía, no "—" ni 0
        self.assertNotIn("—", html.split("<tbody>")[1])
        # Botones CSV: todo, la principal y cada grupo, con download (regla del throbber).
        self.assertIn(f'href="{self.url_csv}?cliente=colima&amp;carrier=fedex&amp;peso_min=2&amp;sku=SIX" download', html)
        self.assertIn("&amp;tabla=principal\" download", html)
        self.assertIn("&amp;tabla=0\" download", html)
        self.assertIn("&amp;tabla=1\" download", html)

    def test_csv_de_una_sola_tabla(self):
        self.client.force_login(self.mesa)
        todo = self.client.get(self.url_csv, {"cliente": "colima"}).content.decode("utf-8-sig")
        self.assertIn("Pedido,Paqueterías,Horas,MXN", todo)
        self.assertIn("Por zona", todo)
        self.assertIn("Por peso", todo)
        principal = self.client.get(self.url_csv, {"cliente": "colima", "tabla": "principal"})
        self.assertIn("-principal.csv", principal["Content-Disposition"])
        texto = principal.content.decode("utf-8-sig")
        self.assertIn("PED-1,todas,,12.50", texto)
        self.assertNotIn("Por zona", texto)
        grupo = self.client.get(self.url_csv, {"cliente": "colima", "tabla": "1"})
        self.assertIn("-tabla2.csv", grupo["Content-Disposition"])
        self.assertEqual(grupo.content.decode("utf-8-sig").strip().splitlines(), ["Rango,Guías", "0-1 kg,"])
        self.assertEqual(self.client.get(self.url_csv, {"cliente": "colima", "tabla": "9"}).status_code, 404)

    def test_portal_no_ve_los_filtros_solo_mesa_ni_los_aplica(self):
        self.client.force_login(self.portal)
        respuesta = self.client.get(reverse("portal:reporte", args=["falso"]), {"carrier": ["fedex"]})
        html = respuesta.content.decode()
        self.assertNotIn('name="carrier"', html)
        self.assertNotIn('name="solo_mesa_chk"', html)
        self.assertIn('name="sku"', html)
        self.assertIn('<td class="">todas</td>', html)  # el multiselect de Mesa no se aplicó
