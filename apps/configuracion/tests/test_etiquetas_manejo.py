"""Etiquetas de manejo: un renglón en configuración; sin renglón, el default; 0 apaga un tipo."""
from django.test import TestCase

from apps.configuracion.models import EtiquetasManejo
from apps.configuracion.services import ETIQUETAS_MANEJO_DEFAULT, etiquetas_manejo


class EtiquetasManejoTests(TestCase):
    def test_default_renglon_y_cero(self):
        self.assertEqual(etiquetas_manejo(), ETIQUETAS_MANEJO_DEFAULT)
        self.assertEqual(ETIQUETAS_MANEJO_DEFAULT, (4, 3))
        fila = EtiquetasManejo.objects.create(fragil=2, orientacion=1)
        self.assertEqual(etiquetas_manejo(), (2, 1))
        self.assertEqual(str(fila), "2 FRÁGIL · 1 de orientación por caja")
        fila.fragil = 0
        fila.save()
        self.assertEqual(etiquetas_manejo(), (0, 1))  # cero = no imprimir FRÁGIL; la orientación sigue
