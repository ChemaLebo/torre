"""manage.py crear_racks: todas las combinaciones rack × lado × frente × piso,
dry-run por default, idempotente y sin tocar ubicaciones existentes."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.catalogo.management.commands.crear_racks import codigos_racks
from apps.catalogo.models import Ubicacion


class CrearRacksTests(TestCase):
    def test_combinaciones(self):
        codigos = codigos_racks(racks=2, pisos=4, piso_reserva=4)
        self.assertEqual(len(codigos), 2 * 2 * 2 * 4)
        self.assertIn(("PIC-1-I-F-1", Ubicacion.PICKING), codigos)
        self.assertIn(("RES-2-D-B-4", Ubicacion.RESERVA), codigos)
        self.assertEqual(sum(1 for _c, t in codigos if t == Ubicacion.RESERVA), 8)

    def test_dry_run_no_escribe_y_aplicar_es_idempotente(self):
        existente = Ubicacion.objects.create(codigo="PIC-1-I-F-1", tipo=Ubicacion.RESERVA, activo=False)
        salida = StringIO()
        call_command("crear_racks", racks=4, pisos=4, stdout=salida)
        self.assertIn("63 por crear", salida.getvalue())
        self.assertEqual(Ubicacion.objects.count(), 1)
        call_command("crear_racks", racks=4, pisos=4, aplicar=True, stdout=StringIO())
        self.assertEqual(Ubicacion.objects.count(), 64)
        self.assertEqual(Ubicacion.objects.filter(tipo=Ubicacion.RESERVA, codigo__startswith="RES-").count(), 16)
        existente.refresh_from_db()
        self.assertEqual((existente.tipo, existente.activo), (Ubicacion.RESERVA, False))  # intacta
        salida = StringIO()
        call_command("crear_racks", racks=4, pisos=4, aplicar=True, stdout=salida)
        self.assertIn("0 ubicación(es) creada(s)", salida.getvalue())
        self.assertEqual(Ubicacion.objects.count(), 64)
