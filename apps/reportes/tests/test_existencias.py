"""Existencias por SKU y lote, y lotes por caducidad: piezas por estado,
diferencias de conteo y de recepción, filtros de stock y de caducidad."""
from datetime import timedelta

from django.utils import timezone

from apps.catalogo.models import Lote
from apps.inventario.models import Conteo, LineaASN, OrdenEntrada, Saldo
from apps.reportes import existencias, lotes
from apps.reportes.base import limites

from .base import ReportesTestCase


class ExistenciasTests(ReportesTestCase):
    def setUp(self):
        hoy = timezone.localdate()
        self.lote_pronto = Lote.objects.create(sku=self.six, codigo="LC-01", fecha_caducidad=hoy + timedelta(days=10))
        self.lote_lejos = Lote.objects.create(sku=self.six, codigo="LC-02", fecha_caducidad=hoy + timedelta(days=200))
        self.lote_vacio = Lote.objects.create(sku=self.six, codigo="LC-03", fecha_caducidad=hoy - timedelta(days=1))
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, lote=self.lote_pronto, estado=Saldo.UBICADO_VENDIBLE, cantidad=12)
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, lote=self.lote_pronto, estado=Saldo.RESERVADO, cantidad=3)
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, lote=self.lote_lejos, estado=Saldo.CUARENTENA, cantidad=5)
        Saldo.objects.create(sku=self.caja, ubicacion=self.picking, lote=None, estado=Saldo.EN_PUTAWAY, cantidad=7)
        Conteo.objects.create(sku=self.six, contador="piso1", esperado=20, contado=18)
        orden = OrdenEntrada.objects.create(cliente=self.colima, estado=OrdenEntrada.CERRADA)
        LineaASN.objects.create(orden=orden, sku=self.six, cantidad_anunciada=30, cantidad_recibida=28, cantidad_danada=1, lote_codigo="LC-01")
        self.inicio, self.fin = limites(hoy - timedelta(days=7), hoy)

    def _filas(self, modulo=existencias, **filtros):
        base = {"caduca_dias": None, "con_stock": True}
        base.update(filtros)
        return modulo.generar(self.colima, self.inicio, self.fin, base, es_mesa=False)

    def test_una_fila_por_sku_y_lote_con_piezas_y_diferencias(self):
        r = self._filas()
        filas = {(f[0], f[2]): f for f in r["filas"]}
        self.assertEqual(set(filas), {("COLIMITA-SIX", "LC-01"), ("COLIMITA-SIX", "LC-02"), ("PARAMO-C12", "sin lote")})
        pronto = filas[("COLIMITA-SIX", "LC-01")]
        self.assertEqual(pronto[4], 10)  # días para caducar
        self.assertEqual((pronto[5], pronto[6], pronto[10]), (12, 3, 15))  # vendible, apartado, físico
        self.assertEqual(pronto[12], -2)  # conteo: 18 contra 20
        self.assertEqual(pronto[13], -1)  # recepción: 28 + 1 dañada contra 30
        self.assertEqual(filas[("COLIMITA-SIX", "LC-02")][9], 5)  # cuarentena
        self.assertIsNone(filas[("COLIMITA-SIX", "LC-02")][13])
        self.assertEqual(filas[("PARAMO-C12", "sin lote")][8], 7)  # en recepción
        self.assertIn(("faltante en conteos", -4), r["resumen"])  # −2 en cada fila del SKU

    def test_sin_filtro_de_stock_aparecen_los_lotes_vacios(self):
        con = self._filas(con_stock=False)
        self.assertIn("LC-03", [f[2] for f in con["filas"]])
        self.assertNotIn("LC-03", [f[2] for f in self._filas()["filas"]])

    def test_filtro_de_caducidad(self):
        r = self._filas(caduca_dias=30)
        self.assertEqual([f[2] for f in r["filas"]], ["LC-01"])

    def test_no_mezcla_clientes(self):
        Saldo.objects.create(sku=self.ajeno, ubicacion=self.picking, estado=Saldo.UBICADO_VENDIBLE, cantidad=9)
        self.assertNotIn("MEZ-750", [f[0] for f in self._filas()["filas"]])

    def test_lotes_ordena_por_caducidad_y_cuenta_vencidos(self):
        r = self._filas(lotes, con_stock=False)
        self.assertEqual([f[2] for f in r["filas"]], ["LC-03", "LC-01", "LC-02"])
        self.assertNotIn("sin lote", [f[2] for f in r["filas"]])
        self.assertIn(("vencidos", 1), r["resumen"])
        self.assertIn(("caducan en 30 días", 2), r["resumen"])
