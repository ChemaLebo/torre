"""El kardex es append-only: la historia no se edita ni se borra."""
from apps.inventario.models import Movimiento, Saldo

from .base import InventarioTestCase


class KardexAppendOnlyTests(InventarioTestCase):
    def _movimiento(self):
        return Movimiento.objects.create(
            sku=self.sku,
            tipo=Movimiento.RECEPCION,
            delta=10,
            estado_destino=Saldo.EN_PUTAWAY,
            referencia="ASN-0001",
            actor="piso1",
        )

    def test_no_se_edita_un_movimiento(self):
        mov = self._movimiento()
        mov.delta = 999
        with self.assertRaisesMessage(ValueError, "append-only"):
            mov.save()
        mov.refresh_from_db()
        self.assertEqual(mov.delta, 10)  # la historia quedó intacta

    def test_no_se_borra_un_movimiento(self):
        mov = self._movimiento()
        with self.assertRaisesMessage(ValueError, "append-only"):
            mov.delete()
        self.assertTrue(Movimiento.objects.filter(pk=mov.pk).exists())

    def test_insertar_si_esta_permitido(self):
        self._movimiento()
        self.assertEqual(Movimiento.objects.count(), 1)
