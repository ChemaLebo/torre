"""Reacomodo sugerido: A en anaquel lento con uno mejor libre, C en los mejores; ocupación por anaquel."""

from apps.catalogo.models import SKU, Ubicacion
from apps.inventario.models import Saldo
from apps.reportes import reacomodo

from .base import ReportesTestCase


class ReacomodoTests(ReportesTestCase):
    def setUp(self):
        self.p = {}
        for prioridad in range(1, 7):
            self.p[prioridad] = Ubicacion.objects.create(
                codigo=f"PIC-P{prioridad}", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=prioridad,
            )
        for sku, dims in ((self.six, (24, 18, 17)), (self.caja, (36, 24, 17))):
            sku.largo_cm, sku.ancho_cm, sku.alto_cm = dims
            sku.save()
        SKU.objects.filter(pk=self.six.pk).update(rotacion="A")
        SKU.objects.filter(pk=self.caja.pk).update(rotacion="C")
        Saldo.objects.create(sku=self.six, ubicacion=self.p[5], estado=Saldo.UBICADO_VENDIBLE, cantidad=10)   # A en anaquel lento
        Saldo.objects.create(sku=self.caja, ubicacion=self.p[1], estado=Saldo.UBICADO_VENDIBLE, cantidad=4)   # C en el mejor

    def test_filas_y_ocupacion(self):
        r = reacomodo.generar(self.colima, None, None, {}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        self.assertEqual(filas["COLIMITA-SIX"][2:7], ["A", "PIC-P5", 5, "mover a PIC-P2 (anaquel libre mejor)", 2])
        self.assertEqual(filas["PARAMO-C12"][2], "C")
        self.assertTrue(filas["PARAMO-C12"][5].startswith("liberar PIC-P1: mover a PIC-P6"))
        [g] = r["grupos"]
        ocup = {f[0]: f for f in g["filas"]}
        self.assertEqual((ocup["PIC-P5"][3], ocup["PIC-P5"][4]), ("libre", "COLIMITA-SIX"))
        self.assertIn(("anaqueles libres", 4), r["resumen"])
