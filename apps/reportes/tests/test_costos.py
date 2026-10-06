"""Costo por entrega: una fila por caja, transporte por guía según la zona
del destino menos reembolsos, picking y empaque una vez por pedido y solo si
se hicieron, toda guía se cobra (reposición, reexpedición, cancelado) y los
reembolsos de cortes anteriores van al final; filtro de zona; subtotal, IVA y
total; Mesa ve costo real y margen."""
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from apps.envios.models import Guia, Paquete
from apps.finanzas.services import registrar_reembolso
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
        self.assertEqual((primera[8], primera[9], primera[10], primera[11], primera[12]), (tarifa_nacional, Decimal("0.00"), picking, empaque, tarifa_nacional + picking + empaque))
        self.assertEqual((segunda[8], segunda[10], segunda[11]), (tarifa_nacional, Decimal("0.00"), Decimal("0.00")))  # una vez por pedido
        self.assertEqual((primera[14], primera[15]), (Decimal("200.00"), Decimal("10.00")))  # costo real e insumo por guía
        self.assertEqual(primera[16], primera[12] - Decimal(200) - Decimal(10))
        l = next(f for f in r["filas"] if f[0] == local.folio)
        self.assertEqual((l[5], l[8]), ("local", Decimal(self.tarifas["envio_bloque"]["local"])))
        self.assertIn(("guías facturables", 3), r["resumen"])
        self.assertIn(("pedidos facturables", 2), r["resumen"])
        self.assertIn(("picking MXN", picking * 2), r["resumen"])
        self.assertIn(("empaque MXN", empaque * 2), r["resumen"])
        subtotal = tarifa_nacional * 2 + Decimal(self.tarifas["envio_bloque"]["local"]) + (picking + empaque) * 2
        self.assertIn(("subtotal sin IVA MXN", subtotal), r["resumen"])
        self.assertIn(("IVA MXN", (subtotal * Decimal("0.16")).quantize(Decimal("0.01"))), r["resumen"])
        self.assertIn(("total con IVA MXN", (subtotal * Decimal("1.16")).quantize(Decimal("0.01"))), r["resumen"])
        portal = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(costos.COLUMNAS))
        self.assertFalse(any("costo real" in e for e, _v in portal["resumen"]))

    def test_caja_pickeada_sin_empacar_y_guia_cancelada(self):
        # Pickeado y cancelado antes de empacar: fila sin guía, picking sí, empaque no.
        cancelado = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp="06600",
                                          estado=Pedido.CANCELADO, ts_picking=timezone.now())
        Paquete.objects.create(pedido=cancelado, numero=1, peso_kg=Decimal("3"), carrier="local")
        # Guía cancelada (histórica, sin registro) y repuesta: la cancelada se cobra y se neta; no cuesta.
        repuesto = self._pedido("06600", [3.0], [90])
        Guia.objects.filter(pedido=repuesto).update(estado=Guia.CANCELADA)
        caja = repuesto.paquetes.get()
        Guia.objects.create(pedido=repuesto, paquete=caja, carrier="local", numero="L-2", proveedor="mock")
        Pedido.objects.filter(pk=repuesto.pk).update(costo_entrega_propia=Decimal(80))  # lo que costó llevarlo (2026-10-06)
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        picking, empaque = Decimal(self.tarifas["alistamiento_pedido"]), Decimal(self.tarifas["empaque_pedido"])
        local = Decimal(self.tarifas["envio_bloque"]["local"])
        sin_guia = next(f for f in r["filas"] if f[0] == cancelado.folio)
        self.assertEqual((sin_guia[1], sin_guia[2], sin_guia[4]), ("", "1", "local"))
        self.assertEqual((sin_guia[8], sin_guia[9], sin_guia[10], sin_guia[11], sin_guia[12]), (Decimal("0.00"), Decimal("0.00"), picking, Decimal("0.00"), picking))
        self.assertEqual(sin_guia[13], "sin guía · cancelado · pickeado sin empacar")
        self.assertEqual((sin_guia[14], sin_guia[15]), (Decimal("0.00"), Decimal("0.00")))
        cancelada, viva = [f for f in r["filas"] if f[0] == repuesto.folio]
        self.assertEqual((cancelada[8], cancelada[9], cancelada[12], cancelada[13], cancelada[14]), (local, local, Decimal("0.00"), "guía cancelada · reembolsada", Decimal("0.00")))
        self.assertEqual((viva[1], viva[8], viva[10], viva[11], viva[14]), ("L-2", local, picking, empaque, Decimal("80.00")))
        self.assertIn(("guías facturables", 2), r["resumen"])  # la cancelada y la viva; la caja sin guía no
        self.assertIn(("pedidos facturables", 2), r["resumen"])

    def test_reembolso_de_corte_anterior_va_al_final_y_resta(self):
        viejo = self._pedido("83000", [12.0], [200], creado_guia=self.inicio - timedelta(days=40))
        g = viejo.guias.get()
        registrar_reembolso(g, "mesa1", origen="reclamacion", nota="pagó la reclamación")
        self._pedido("06600", [3.0], [90])
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        nacional = Decimal(self.tarifas["envio_bloque"]["nacional"])
        ultima = r["filas"][-1]
        self.assertEqual((ultima[0], ultima[1], ultima[2], ultima[5]), (viejo.folio, g.numero, "1", "nacional"))
        self.assertEqual((ultima[8], ultima[9], ultima[12], ultima[16]), (Decimal("0.00"), nacional, -nacional, -nacional))
        self.assertIn("reembolso de corte anterior", ultima[13])
        self.assertIn("reclamación pagada", ultima[13])
        self.assertIn(("reembolsos MXN", -nacional), r["resumen"])
        self.assertIn(("guías facturables", 1), r["resumen"])
        # Con el filtro de zona local, el reembolso nacional no aparece.
        self.assertFalse(any(f[0] == viejo.folio for f in costos.generar(self.colima, self.inicio, self.fin, {"zona": "local"}, es_mesa=False)["filas"]))

    def test_filtro_de_zona(self):
        self._pedido("83000", [12.0], [200])
        self._pedido("06600", [3.0], [90])
        r = costos.generar(self.colima, self.inicio, self.fin, {"zona": "local"}, es_mesa=False)
        self.assertEqual([f[5] for f in r["filas"]], ["local"])
        self.assertIn(("guías facturables", 1), r["resumen"])
        self.assertEqual(costos.FILTROS[0]["nombre"], "zona")

    def test_reexpedicion_y_cancelado_se_cobran(self):
        reexp = self._pedido("44100", [4.0], [150])
        Guia.objects.create(pedido=reexp, carrier="estafeta", numero="VIEJA", proveedor="mock", estado=Guia.RETORNO,
                            costo_preferencial=Decimal(140))
        Guia.objects.filter(numero="VIEJA").update(creado=timezone.now() - timedelta(days=40))
        cancelado = self._pedido("44100", [4.0], [150], estado=Pedido.CANCELADO)
        r = costos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        metro = Decimal(self.tarifas["envio_bloque"]["metro"])
        # Reexpedición: la guía nueva se cobra; picking y empaque ya se cobraron en su corte.
        self.assertEqual((filas[reexp.folio][8], filas[reexp.folio][12], filas[reexp.folio][13]), (metro, metro, "reexpedición"))
        # Cancelado con guía viva: pagó el trabajo de piso (picking + empaque) y la guía que WOP pagó.
        self.assertEqual((filas[cancelado.folio][8], filas[cancelado.folio][12], filas[cancelado.folio][13]), (metro, metro + Decimal("90"), "cancelado"))
        self.assertEqual(filas[reexp.folio][14], Decimal("150.00"))  # el costo real sí cuenta
        self.assertIn(("guías facturables", 2), r["resumen"])
        self.assertIn(("pedidos facturables", 2), r["resumen"])
