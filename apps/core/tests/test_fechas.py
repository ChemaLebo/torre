"""Fechas de CSV capturadas a mano: ISO, DD/MM/AAAA, DD-MM-AAAA, placeholder."""
from datetime import date

from django.test import SimpleTestCase

from apps.core.fechas import PLACEHOLDER_FECHA, parsear_fecha_csv


class ParsearFechaCsvTests(SimpleTestCase):
    def test_acepta_los_tres_formatos(self):
        for crudo in ("2027-05-01", "01/05/2027", "01-05-2027", " 2027-05-01 "):
            self.assertEqual(parsear_fecha_csv(crudo), date(2027, 5, 1), crudo)

    def test_vacio_y_placeholder_son_none(self):
        for crudo in ("", "   ", None, PLACEHOLDER_FECHA, "aaaa-mm-dd"):
            self.assertIsNone(parsear_fecha_csv(crudo))

    def test_rechaza_lo_demas(self):
        for crudo in ("2027-13-40", "mayo 2027", "05/01/27", "2027/05/01"):
            with self.assertRaises(ValueError):
                parsear_fecha_csv(crudo)
