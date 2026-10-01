"""Zonas de cobro por CP desde config/zonas_cp.csv (Chema 2026-09-28): rangos
inclusivos, primer match manda, fuera de rango = nacional, sin CP = None."""
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from apps.finanzas import zonas
from apps.finanzas.services import zona_de_cp


class ZonasCPTests(TestCase):
    def tearDown(self):
        zonas.recargar()

    def test_archivo_del_repo_conserva_las_zonas_de_siempre(self):
        zonas.recargar()
        self.assertEqual(zona_de_cp("06600"), "local")
        self.assertEqual(zona_de_cp("53390"), "local")
        self.assertEqual(zona_de_cp("44100"), "metro")
        self.assertEqual(zona_de_cp("76000"), "metro")
        self.assertEqual(zona_de_cp("64000"), "nacional")
        self.assertIsNone(zona_de_cp(""))
        self.assertIsNone(zona_de_cp("123"))

    def test_archivo_propio_rangos_comentarios_y_default_nacional(self):
        ruta = Path(tempfile.mkdtemp()) / "zonas.csv"
        ruta.write_text(
            "# comentario\ncp_desde,cp_hasta,zona\n20000,20999,metro\n64000,64999,METRO\n"
            "00000,16999,local\nabc,def,local\n99000,98000,local\n", encoding="utf-8",
        )
        with override_settings(TORRE={**settings.TORRE, "ZONAS_CP_ARCHIVO": str(ruta)}):
            zonas.recargar()
            self.assertEqual(zona_de_cp("20100"), "metro")   # Aguascalientes según 99minutos
            self.assertEqual(zona_de_cp("64460"), "metro")   # mayúsculas toleradas
            self.assertEqual(zona_de_cp("01780"), "local")
            self.assertEqual(zona_de_cp("83000"), "nacional")  # fuera de todo rango
            self.assertEqual(len(zonas.rangos()), 3)  # las filas inválidas se ignoran

    def test_sin_archivo_todo_es_nacional(self):
        with override_settings(TORRE={**settings.TORRE, "ZONAS_CP_ARCHIVO": "/no/existe.csv"}):
            zonas.recargar()
            self.assertEqual(zona_de_cp("06600"), "nacional")
