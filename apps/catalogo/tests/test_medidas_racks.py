"""Medidas y prioridad por código de rack doble, y crear_racks llenándolas."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.catalogo.models import Ubicacion
from apps.catalogo.services import aplicar_medidas, medidas_de_codigo


class MedidasDeCodigoTests(TestCase):
    def test_medidas_por_piso_y_reserva_sin_tope(self):
        self.assertEqual(medidas_de_codigo("PIC-1-I-F-1"), {"largo_cm": 180, "ancho_cm": 58, "alto_cm": 52, "prioridad": 5})
        self.assertEqual(medidas_de_codigo("PIC-1-I-F-3")["alto_cm"], 44)
        reserva = medidas_de_codigo("RES-4-D-B-4")
        self.assertEqual((reserva["alto_cm"], reserva["prioridad"]), (0, None))
        self.assertIsNone(medidas_de_codigo("A-01-1"))
        self.assertIsNone(medidas_de_codigo("PIC-1-1"))

    def test_orden_de_acceso(self):
        orden = [
            "PIC-1-I-F-2", "PIC-1-I-B-2", "PIC-1-I-F-3", "PIC-1-I-B-3", "PIC-1-I-F-1", "PIC-1-I-B-1",
            "PIC-1-D-F-2", "PIC-1-D-B-2",
        ]
        self.assertEqual([medidas_de_codigo(c)["prioridad"] for c in orden], list(range(1, 9)))
        self.assertEqual(medidas_de_codigo("PIC-2-I-F-2")["prioridad"], 13)
        self.assertEqual(medidas_de_codigo("PIC-4-D-B-1")["prioridad"], 48)

    def test_crear_racks_llena_medidas_y_refresca_existentes(self):
        vieja = Ubicacion.objects.create(codigo="PIC-1-I-F-2", tipo=Ubicacion.PICKING)
        call_command("crear_racks", racks=1, pisos=4, aplicar=True, medidas=True, stdout=StringIO())
        nueva = Ubicacion.objects.get(codigo="PIC-1-D-B-1")
        self.assertEqual((nueva.largo_cm, nueva.ancho_cm, nueva.alto_cm, nueva.prioridad), (180, 58, 52, 12))
        vieja.refresh_from_db()
        self.assertEqual(vieja.prioridad, 1)
        self.assertEqual(Ubicacion.objects.get(codigo="RES-1-I-F-4").prioridad, None)
        self.assertEqual(aplicar_medidas(Ubicacion.objects.all()), 0)  # ya todas al día
