"""Costo por entrega: una fila por guía, transporte por zona del destino,
almacén una vez por pedido, sin cargo en guías canceladas, reexpediciones y
cancelados; filtro de zona; Mesa ve costo real y margen."""
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
        self.tarifas = __import__("django.conf").conf.settings.TORRE["TARIFARIO_DEFAULT"]

    def _pedido(self, cp, kgs, costos_guias, carrier="estafeta", estado=Pedido.RECOLECTADO, creado_guia=None):
        """Una caja y una guía por peso, con su costo real."""
        pedido = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp=cp, estado=estado)
        for n, (kg, costo) in enumerate(zip(kgs, costos_guias), start=1):
            caja = Paquete.objects.create(pedido=pedido, numero=n, peso_kg=Decimal(str(kg)), carrier=carrier, peso_real_gr=int(kg * 1000))
            guia = Guia.objects.create(pedido=pedido, paquete=caja, carrier=carrier, numero=f"G-{pedido.pk}-{n}", proveedor="mock",
                                       costo_preferencial=Decimal(str(costo)))
            if creado_guia is not None:
                Guia.objects.filter(pk=guia.pk).update(creado=creado_guia)
        return pedido

    def test_una_fila_por_guia_con_transporte_por_zona_y_almacen_por_pedido(self):
        nacional = self._pedido("83000", [12.0, 9.5], [200, 180])   # 2 guías nacional
        local = self._pedido("06600", [3.0], [90])                  # 1 guía local
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        filas = [f for f in r["filas"] if f[0] == nacional.folio]
        almacen = Decimal(self.tarifas["alistamiento_pedido"]) + Decimal(self.tarifas["empaque_pedido"])
        tarifa_nacional = Decimal(self.tarifas["envio_bloque"]["nacional"])
        self.assertEqual(len(filas), 2)
        primera, segunda = filas
        self.assertEqual((primera[1], primera[2], primera[5], primera[6], primera[7]), (f"G-{nacional.pk}-1", "1", "nacional", "Sonora", Decimal("12.0")))
        self.assertEqual((primera[8], primera[9], primera[10]), (tarifa_nacional, almacen, tarifa_nacional + almacen))
        self.assertEqual((segunda[8], segunda[9]), (tarifa_nacional, Decimal("0.00")))  # el almacén va una vez
        self.assertEqual((primera[12], primera[13]), (Decimal("200.00"), Decimal("10.00")))  # costo real e insumo por guía
        self.assertEqual(primera[14], primera[10] - Decimal(200) - Decimal(10))
        l = [f for f in r["filas"] if f[0] == local.folio][0]
        self.assertEqual((l[5], l[8]), ("local", Decimal(self.tarifas["envio_bloque"]["local"])))
        self.assertIn(("guías facturables", 3), r["resumen"])
        self.assertIn(("pedidos facturables", 2), r["resumen"])
        portal = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(costos.COLUMNAS))
        self.assertFalse(any("costo real" in e for e, _v in portal["resumen"]))

    def test_filtro_de_zona(self):
        self._pedido("83000", [12.0], [200])
        self._pedido("06600", [3.0], [90])
        r = costos.generar(self.colima, self.inicio, self.fin, {"zona": "local"}, es_mesa=False)
        self.assertEqual([f[5] for f in r["filas"]], ["local"])
        self.assertIn(("guías facturables", 1), r["resumen"])
        self.assertEqual(costos.FILTROS[0]["nombre"], "zona")

    def test_reexpedicion_y_cancelado_sin_cargo(self):
        reexp = self._pedido("44100", [4.0], [150])
        Guia.objects.create(pedido=reexp, carrier="estafeta", numero="VIEJA", proveedor="mock", estado=Guia.RETORNO,
                            costo_preferencial=Decimal(140))
        Guia.objects.filter(numero="VIEJA").update(creado=timezone.now() - timedelta(days=40))
        cancelado = self._pedido("44100", [4.0], [150], estado=Pedido.CANCELADO)
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        self.assertEqual((filas[reexp.folio][10], filas[reexp.folio][11]), (Decimal("0.00"), "reexpedición (sin cargo)"))
        self.assertEqual((filas[cancelado.folio][10], filas[cancelado.folio][11]), (Decimal("0.00"), "cancelado (sin cargo)"))
        self.assertEqual(filas[reexp.folio][12], Decimal("150.00"))  # el costo real sí cuenta
        self.assertIn(("pedidos facturables", 0), r["resumen"])
