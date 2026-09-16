"""Inventario por bodega: disponible con buffer, piezas por estado, en
tránsito desde ASN pendientes y alertas de reorden."""
from apps.core.models import Cliente
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo
from apps.reportes import inventario

from .base import ReportesTestCase


class InventarioTests(ReportesTestCase):
    def setUp(self):
        Cliente.objects.filter(pk=self.colima.pk).update(buffer_stock=2)
        self.colima.refresh_from_db()
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, estado=Saldo.UBICADO_VENDIBLE, cantidad=20)
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, estado=Saldo.RESERVADO, cantidad=5)
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, estado=Saldo.CUARENTENA, cantidad=1)
        pendiente = OrdenEntrada.objects.create(cliente=self.colima, estado=OrdenEntrada.EN_RECEPCION)
        LineaASN.objects.create(orden=pendiente, sku=self.six, cantidad_anunciada=24, cantidad_recibida=4)
        LineaASN.objects.create(orden=pendiente, sku=self.caja, cantidad_anunciada=6)
        cerrada = OrdenEntrada.objects.create(cliente=self.colima, estado=OrdenEntrada.CERRADA)
        LineaASN.objects.create(orden=cerrada, sku=self.caja, cantidad_anunciada=6, cantidad_recibida=6)
        self.caja.punto_reorden = 3
        self.caja.save()

    def test_disponible_transito_y_alertas(self):
        r = inventario.generar(self.colima, None, None, {"solo_alertas": False}, es_mesa=False)
        filas = {f[0]: f for f in r["filas"]}
        six = filas["COLIMITA-SIX"]
        self.assertEqual((six[2], six[3], six[4], six[5], six[8], six[9], six[10]), ("Torre", 13, 20, 5, 1, 20, 2))
        self.assertEqual(six[12], "")
        caja = filas["PARAMO-C12"]
        self.assertEqual((caja[3], caja[9], caja[11], caja[12]), (-2, 6, 3, "bajo punto de reorden"))
        self.assertEqual(dict(r["resumen"])["en tránsito"], 26)
        solo = inventario.generar(self.colima, None, None, {"solo_alertas": True}, es_mesa=False)
        self.assertEqual([f[0] for f in solo["filas"]], ["PARAMO-C12"])
