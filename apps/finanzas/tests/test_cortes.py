"""Cortes quincenales: 1–15 y 16–fin de mes real (Chema 2026-10-01), claves
para la URL, navegación entre cortes y límites aware."""
from datetime import date, datetime

from django.test import SimpleTestCase
from django.utils import timezone

from apps.finanzas.cortes import Corte, corte_actual, corte_de, corte_desde_clave


class CortesTests(SimpleTestCase):
    def test_fin_de_mes_real(self):
        self.assertEqual((Corte(2026, 10, 2).inicio, Corte(2026, 10, 2).fin, Corte(2026, 10, 2).dias), (date(2026, 10, 16), date(2026, 10, 31), 16))
        self.assertEqual((Corte(2028, 2, 2).fin, Corte(2028, 2, 2).dias), (date(2028, 2, 29), 14))  # bisiesto
        self.assertEqual((Corte(2027, 2, 2).fin, Corte(2027, 2, 2).dias), (date(2027, 2, 28), 13))
        self.assertEqual((Corte(2026, 9, 1).inicio, Corte(2026, 9, 1).fin, Corte(2026, 9, 1).dias), (date(2026, 9, 1), date(2026, 9, 15), 15))

    def test_corte_de_una_fecha_y_claves(self):
        self.assertEqual(corte_de(date(2026, 9, 15)), Corte(2026, 9, 1))
        self.assertEqual(corte_de(date(2026, 9, 16)), Corte(2026, 9, 2))
        # Un datetime aware se lee en hora local: las 23:30 del 15 (UTC-6) siguen siendo el 15.
        self.assertEqual(corte_de(timezone.make_aware(datetime(2026, 9, 15, 23, 30))), Corte(2026, 9, 1))
        self.assertEqual(Corte(2026, 9, 2).clave, "2026-09-2")
        self.assertEqual(corte_desde_clave("2026-09-2"), Corte(2026, 9, 2))
        self.assertIsNone(corte_desde_clave("2026-13-1"))
        self.assertIsNone(corte_desde_clave("2026-09-3"))
        self.assertIsNone(corte_desde_clave("chorizo"))
        self.assertIsNone(corte_desde_clave(""))
        self.assertEqual(corte_actual(), corte_de(timezone.localdate()))

    def test_etiquetas(self):
        self.assertEqual(Corte(2026, 10, 1).etiqueta, "1ª quincena de octubre 2026")
        self.assertEqual(Corte(2026, 10, 2).etiqueta_corta, "16–31 oct 2026")

    def test_navegacion_cruza_mes_y_anio(self):
        self.assertEqual(Corte(2026, 12, 2).siguiente(), Corte(2027, 1, 1))
        self.assertEqual(Corte(2027, 1, 1).anterior(), Corte(2026, 12, 2))
        self.assertEqual(Corte(2026, 10, 1).siguiente(), Corte(2026, 10, 2))
        self.assertEqual(Corte(2026, 10, 2).anterior(), Corte(2026, 10, 1))
        self.assertEqual(Corte(2026, 10, 1).anterior(), Corte(2026, 9, 2))

    def test_limites_aware_y_cerrado(self):
        inicio, fin = Corte(2026, 9, 2).limites()
        self.assertTrue(timezone.is_aware(inicio) and timezone.is_aware(fin))
        self.assertEqual((timezone.localtime(inicio).date(), timezone.localtime(fin).date()), (date(2026, 9, 16), date(2026, 10, 1)))
        self.assertTrue(Corte(2026, 9, 2).cerrado(hoy=date(2026, 10, 1)))
        self.assertFalse(Corte(2026, 10, 1).cerrado(hoy=date(2026, 10, 15)))
