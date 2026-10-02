"""Folios consecutivos sin tope (Chema 2026-10-02): el siguiente sale del
máximo numérico, así que pasar de 99,999 pedidos o de 9,999 incidencias o
manifiestos en un año no repite folios ni tira la ingesta."""
from django.test import TestCase
from django.utils import timezone

from apps.core.folios import siguiente_folio
from apps.core.models import Cliente
from apps.envios.models import Manifiesto
from apps.incidencias.models import Incidencia
from apps.inventario.models import OrdenEntrada
from apps.pedidos.models import Pedido


class FoliosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")

    def _pedido(self, folio=""):
        return Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp="06600", folio=folio)

    def test_pedidos_cruzan_los_cien_mil(self):
        self._pedido("PED-99999")
        self.assertEqual(self._pedido().folio, "PED-100000")
        self.assertEqual(self._pedido().folio, "PED-100001")  # el alfabético habría vuelto a PED-100000
        self._pedido("PED-DEMO-7")  # un folio ajeno no descarrila la secuencia
        self.assertEqual(self._pedido().folio, "PED-100002")

    def test_relleno_es_minimo_no_tope(self):
        self.assertEqual(self._pedido().folio, "PED-00001")
        self.assertEqual(siguiente_folio(Pedido, "PED", ancho=5), "PED-00002")

    def test_manifiestos_e_incidencias_cruzan_los_diez_mil_por_anio(self):
        anio = timezone.localtime().year
        Manifiesto.objects.create(carrier="estafeta", folio=f"MAN-{anio}-9999")
        self.assertEqual(Manifiesto.objects.create(carrier="estafeta").folio, f"MAN-{anio}-10000")
        self.assertEqual(Manifiesto.objects.create(carrier="estafeta").folio, f"MAN-{anio}-10001")
        Incidencia.objects.create(cliente=self.colima, tipo="DAN", origen="manual", folio=f"INC-{anio}-9999")
        self.assertEqual(Incidencia._generar_folio(), f"INC-{anio}-10000")
        Incidencia.objects.create(cliente=self.colima, tipo="DAN", origen="manual", folio=f"INC-{anio}-10000")
        self.assertEqual(Incidencia._generar_folio(), f"INC-{anio}-10001")

    def test_inventario_usa_el_mismo_helper(self):
        OrdenEntrada.objects.create(cliente=self.colima, folio="ASN-9999")
        self.assertEqual(OrdenEntrada.objects.create(cliente=self.colima).folio, "ASN-10000")
