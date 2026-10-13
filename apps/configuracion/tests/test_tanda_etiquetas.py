"""Tanda de etiquetas: un renglón en configuración; sin renglón, el default."""
from django.test import TestCase

from apps.configuracion.models import TandaEtiquetas
from apps.configuracion.services import TANDA_ETIQUETAS_DEFAULT, tanda_etiquetas


class TandaEtiquetasTests(TestCase):
    def test_default_y_renglon(self):
        self.assertEqual(tanda_etiquetas(), TANDA_ETIQUETAS_DEFAULT)
        fila = TandaEtiquetas.objects.create(cajas=25)
        self.assertEqual(tanda_etiquetas(), 25)
        self.assertEqual(str(fila), "25 etiquetas por tanda")
        fila.cajas = 0
        fila.save()
        self.assertEqual(tanda_etiquetas(), TANDA_ETIQUETAS_DEFAULT)
