"""Costo por entrega: una fila por caja, transporte por guía según la zona
del destino, picking y empaque una vez por pedido y solo si se hicieron, sin
envío en guías canceladas, reexpediciones y cancelados; filtro de zona; Mesa
ve costo real y margen."""
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

    def test_una_fila_por_caja_con_transporte_por_guia_y_picking_y_empaque_por_pedido(self):
        nacional = self._pedido("83000", [12.0, 9.5], [200, 180])   # 2 guías nacional
        local = self._pedido("06600", [3.0], [90])                  # 1 guía local
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        filas = [f for f in r["filas"] if f[0] == nacional.folio]
        picking, empaque = Decimal(self.tarifas["alistamiento_pedido"]), Decimal(self.tarifas["empaque_pedido"])
        tarifa_nacional = Decimal(self.tarifas["envio_bloque"]["nacional"])
        self.assertEqual(len(filas), 2)
        primera, segunda = filas
        self.assertEqual((primera[1], primera[2], primera[5], primera[6], primera[7]), (f"G-{nacional.pk}-1", "1", "nacional", "Sonora", Decimal("12.0")))
        self.assertEqual((primera[8], primera[9], primera[10], primera[11]), (tarifa_nacional, picking, empaque, tarifa_nacional + picking + empaque))
        self.assertEqual((segunda[8], segunda[9], segunda[10]), (tarifa_nacional, Decimal("0.00"), Decimal("0.00")))  # una vez por pedido
        self.assertEqual((primera[13], primera[14]), (Decimal("200.00"), Decimal("10.00")))  # costo real e insumo por guía
        self.assertEqual(primera[15], primera[11] - Decimal(200) - Decimal(10))
        l = next(f for f in r["filas"] if f[0] == local.folio)
        self.assertEqual((l[5], l[8]), ("local", Decimal(self.tarifas["envio_bloque"]["local"])))
        self.assertIn(("guías facturables", 3), r["resumen"])
        self.assertIn(("pedidos facturables", 2), r["resumen"])
        self.assertIn(("picking MXN", picking * 2), r["resumen"])
        self.assertIn(("empaque MXN", empaque * 2), r["resumen"])
        portal = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(costos.COLUMNAS))
        self.assertFalse(any("costo real" in e for e, _v in portal["resumen"]))

    def test_caja_pickeada_sin_empacar_y_guia_cancelada(self):
        # Pickeado y cancelado antes de empacar: fila sin guía, picking sí, empaque no.
        cancelado = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp="06600",
                                          estado=Pedido.CANCELADO, ts_picking=timezone.now())
        Paquete.objects.create(pedido=cancelado, numero=1, peso_kg=Decimal("3"), carrier="local")
        # Guía cancelada y repuesta: la cancelada no cuesta (se reembolsa) ni cobra.
        repuesto = self._pedido("06600", [3.0], [90])
        Guia.objects.filter(pedido=repuesto).update(estado=Guia.CANCELADA)
        caja = repuesto.paquetes.get()
        Guia.objects.create(pedido=repuesto, paquete=caja, carrier="local", numero="L-2", proveedor="mock",
                            costo_preferencial=Decimal(80))
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        picking, empaque = Decimal(self.tarifas["alistamiento_pedido"]), Decimal(self.tarifas["empaque_pedido"])
        sin_guia = next(f for f in r["filas"] if f[0] == cancelado.folio)
        self.assertEqual((sin_guia[1], sin_guia[2], sin_guia[4]), ("", "1", "local"))
        self.assertEqual((sin_guia[8], sin_guia[9], sin_guia[10], sin_guia[11]), (Decimal("0.00"), picking, Decimal("0.00"), picking))
        self.assertEqual(sin_guia[12], "sin guía · cancelado · pickeado sin empacar")
        self.assertEqual((sin_guia[13], sin_guia[14]), (Decimal("0.00"), Decimal("0.00")))
        cancelada, viva = [f for f in r["filas"] if f[0] == repuesto.folio]
        self.assertEqual((cancelada[8], cancelada[9], cancelada[12], cancelada[13]), (Decimal("0.00"), Decimal("0.00"), "guía cancelada (sin cargo)", Decimal("0.00")))
        self.assertEqual((viva[1], viva[8], viva[9], viva[10], viva[13]), ("L-2", Decimal(self.tarifas["envio_bloque"]["local"]), picking, empaque, Decimal("80.00")))
        self.assertIn(("guías facturables", 1), r["resumen"])
        self.assertIn(("pedidos facturables", 2), r["resumen"])

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
        self.assertEqual((filas[reexp.folio][11], filas[reexp.folio][12]), (Decimal("0.00"), "reexpedición (sin cargo)"))
        # Cancelado con guía viva: pagó el trabajo de piso (picking + empaque), no el envío.
        self.assertEqual((filas[cancelado.folio][8], filas[cancelado.folio][11], filas[cancelado.folio][12]), (Decimal("0.00"), Decimal("90.00"), "cancelado"))
        self.assertEqual(filas[reexp.folio][13], Decimal("150.00"))  # el costo real sí cuenta
        self.assertIn(("guías facturables", 0), r["resumen"])
        self.assertIn(("pedidos facturables", 1), r["resumen"])
