"""Costo por entrega: transporte por bloque y zona, almacén por pedido, sin
cargo en reexpediciones y cancelados; Mesa ve costo real y margen."""
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from apps.envios.models import Guia, Paquete
from apps.pedidos.models import Pedido
from apps.reportes import costos
from apps.reportes.base import limites

from .base import ReportesTestCase


@override_settings(TORRE={**__import__("django.conf").conf.settings.TORRE, "INSUMO_PAQUETE_MXN": 10})
class CostosTests(ReportesTestCase):
    def setUp(self):
        hoy = timezone.localdate()
        self.inicio, self.fin = limites(hoy - timedelta(days=7), hoy)
        self.tarifas = costos.settings.TORRE["TARIFARIO_DEFAULT"]

    def _pedido(self, cp, kgs, costo, carrier="estafeta", estado=Pedido.RECOLECTADO, creado_guia=None):
        pedido = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp=cp, estado=estado)
        for n, kg in enumerate(kgs, start=1):
            Paquete.objects.create(pedido=pedido, numero=n, peso_kg=Decimal(str(kg)), carrier=carrier, peso_real_gr=int(kg * 1000))
        guia = Guia.objects.create(pedido=pedido, carrier=carrier, numero=f"G-{pedido.pk}", proveedor="mock", costo_preferencial=Decimal(str(costo)))
        if creado_guia is not None:
            Guia.objects.filter(pk=guia.pk).update(creado=creado_guia)
        return pedido

    def test_transporte_por_bloque_y_zona_mas_almacen(self):
        nacional = self._pedido("83000", [12.0, 9.5], costo=380)   # 21.5 kg → 2 bloques nacional
        local = self._pedido("06600", [3.0], costo=90)              # 1 bloque local
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        almacen = Decimal(self.tarifas["alistamiento_pedido"]) + Decimal(self.tarifas["empaque_pedido"])
        n = filas[nacional.folio]
        self.assertEqual((n[3], n[4], n[5], n[6], n[7]), ("nacional", "Sonora", 2, Decimal("21.5"), 2))
        self.assertEqual(n[8], Decimal(2 * self.tarifas["envio_bloque"]["nacional"]))
        self.assertEqual(n[9], almacen)
        self.assertEqual(n[12], Decimal("380.00"))  # costo real (solo Mesa)
        self.assertEqual(n[13], Decimal("20.00"))   # insumos: 2 cajas × 10
        self.assertEqual(n[14], n[10] - Decimal(380) - Decimal(20))
        l = filas[local.folio]
        self.assertEqual((l[3], l[7], l[8]), ("local", 1, Decimal(self.tarifas["envio_bloque"]["local"])))
        self.assertIn(("pedidos facturables", 2), r["resumen"])
        portal = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(costos.COLUMNAS))
        self.assertFalse(any("costo real" in e for e, _v in portal["resumen"]))

    def test_reexpedicion_y_cancelado_sin_cargo(self):
        reexp = self._pedido("44100", [4.0], costo=150)
        Guia.objects.create(pedido=reexp, carrier="estafeta", numero="VIEJA", proveedor="mock", estado=Guia.RETORNO,
                            costo_preferencial=Decimal(140))
        Guia.objects.filter(numero="VIEJA").update(creado=timezone.now() - timedelta(days=40))
        cancelado = self._pedido("44100", [4.0], costo=150, estado=Pedido.CANCELADO)
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        self.assertEqual((filas[reexp.folio][10], filas[reexp.folio][11]), (Decimal("0.00"), "reexpedición (sin cargo)"))
        self.assertEqual((filas[cancelado.folio][10], filas[cancelado.folio][11]), (Decimal("0.00"), "cancelado (sin cargo)"))
        self.assertEqual(filas[reexp.folio][12], Decimal("150.00"))  # el costo real sí cuenta
        self.assertIn(("pedidos facturables", 0), r["resumen"])
