"""Reporte de fulfillment (Chema 2026-10-22): una fila por guía de los pedidos
ingresados en el periodo, tipos de envío, pesos y volumétrico, lotes por caja,
tiempos, importes con y sin IVA, filtros, cortes con gráfica y solo Mesa."""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import Caja, Lote
from apps.envios.models import EventoGuia, Guia, LineaManifiesto, LocalidadCP, Manifiesto, Paquete, PaqueteLinea
from apps.finanzas.models import ReembolsoGuia
from apps.pedidos.models import LineaPedido, Pedido, PickLinea
from apps.reportes import fulfillment
from apps.reportes.base import limites

from .base import ReportesTestCase

TORRE_PRUEBA = {**__import__("django.conf").conf.settings.TORRE, "IVA": 0.16}


@override_settings(TORRE=TORRE_PRUEBA)
class FulfillmentTests(ReportesTestCase):
    def setUp(self):
        call_command("cargar_zonas_cp", verbosity=0)  # zonas desde Configuración (origen WOP 01780)
        self.t0 = timezone.now() - timedelta(days=4)
        hoy = timezone.localdate()
        self.inicio, self.fin = limites(hoy - timedelta(days=7), hoy)
        self.tarifas = TORRE_PRUEBA["TARIFARIO_DEFAULT"]
        self.lote = Lote.objects.create(sku=self.six, codigo="L-2026", fecha_caducidad=hoy + timedelta(days=200))
        LocalidadCP.objects.create(cp="76000", localidad="Querétaro", municipio="Querétaro", estado="QR")
        self.caja_cat = Caja.objects.create(cliente=self.colima, nombre="Caja 24", largo_cm=40, ancho_cm=30, alto_cm=26, peso_gr=500)
        # Pedido A: dos cajas; caja 1 con FedEx por envia (crudo refrescado con repesaje), caja 2 con 99minutos.
        self.a = self._pedido("76000", orden="#4074", creado=self.t0)
        self.l_six = LineaPedido.objects.create(pedido=self.a, sku=self.six, cantidad=3, reservada=True, cantidad_pickeada=3)
        self.l_caja = LineaPedido.objects.create(pedido=self.a, sku=self.caja, cantidad=1, reservada=True, cantidad_pickeada=1)
        self.c1 = self._caja(self.a, 1, peso_real_gr=14350, dims=(40, 30, 26), listo=self.t0 + timedelta(hours=5))
        self.c2 = self._caja(self.a, 2, peso_real_gr=4000, dims=(26, 20, 25), listo=self.t0 + timedelta(hours=5))
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=self.l_caja, cantidad=1)
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=self.l_six, cantidad=2)
        PaqueteLinea.objects.create(paquete=self.c2, linea_pedido=self.l_six, cantidad=1)
        PickLinea.objects.create(linea=self.l_six, lote=self.lote, cantidad=2, caja=self.c1)
        PickLinea.objects.create(linea=self.l_six, lote=self.lote, cantidad=1, caja=self.c2)
        self.g1 = Guia.objects.create(
            pedido=self.a, paquete=self.c1, carrier="fedex", servicio="ground", numero="879", proveedor="envia",
            estado=Guia.ENTREGADO, costo_cotizado=Decimal("297"), costo_preferencial=Decimal("276"),
            ts_recolectado_carrier=self.t0 + timedelta(hours=9),
            raw={"total": 276, "grand_total": 300, "shipment_weight": 14.35, "shipment_real_weight": 15,
                 "overcharge_applied": 1, "overcharge_cost": 24, "zone": 0},
        )
        EventoGuia.objects.create(guia=self.g1, estado=Guia.ENTREGADO, crudo="DL", ts_carrier=self.t0 + timedelta(hours=33))
        self.g2 = Guia.objects.create(
            pedido=self.a, paquete=self.c2, carrier="noventa9Minutos", servicio="SPT", numero="2243", proveedor="99minutos",
            estado=Guia.EN_TRANSITO, costo_cotizado=Decimal("131.08"), costo_preferencial=Decimal("131.08"),
            raw={"data": {"comment": ""}, "statusCode": "ok"},
        )
        manifiesto = Manifiesto.objects.create(carrier="fedex", corral="SAL-FEDEX")
        Manifiesto.objects.filter(pk=manifiesto.pk).update(ts=self.t0 + timedelta(hours=7))
        LineaManifiesto.objects.create(manifiesto=manifiesto, pedido=self.a, paquete=self.c1, guia=self.g1, numero_guia="879", caja=1)
        Pedido.objects.filter(pk=self.a.pk).update(ts_picking=self.t0 + timedelta(hours=1), ts_empacado=self.t0 + timedelta(hours=4))
        # Pedido B (local): guía cancelada con reembolso y la que la sustituyó como reposición de la caja 1.
        self.b = self._pedido("06600", orden="#4075", creado=self.t0 + timedelta(days=1))
        lb = LineaPedido.objects.create(pedido=self.b, sku=self.six, cantidad=1, reservada=True, cantidad_pickeada=1)
        cb = self._caja(self.b, 1, peso_real_gr=4100, dims=(26, 20, 25), listo=self.t0 + timedelta(days=1, hours=3))
        PaqueteLinea.objects.create(paquete=cb, linea_pedido=lb, cantidad=1)
        self.gb = Guia.objects.create(pedido=self.b, paquete=cb, carrier="estafeta", numero="CANC", proveedor="envia",
                                      estado=Guia.CANCELADA, costo_preferencial=Decimal("90"))
        ReembolsoGuia.objects.create(guia=self.gb, cliente=self.colima, monto=Decimal(self.tarifas["envio_bloque"]["local"]),
                                     fecha=timezone.now(), origen=ReembolsoGuia.ORIGEN_CANCELACION)
        cb2 = self._caja(self.b, 2, peso_real_gr=4100, dims=(26, 20, 25), listo=self.t0 + timedelta(days=1, hours=6))
        PaqueteLinea.objects.create(paquete=cb2, linea_pedido=lb, cantidad=1, repone_a=cb)
        self.gb2 = Guia.objects.create(pedido=self.b, paquete=cb2, carrier="estafeta", numero="REPO", proveedor="envia",
                                       estado=Guia.GUIA_CREADA, costo_preferencial=Decimal("95"))
        # Pedido viejo, fuera del periodo, con guía dentro: no entra (el periodo es del pedido).
        viejo = self._pedido("06600", orden="#4000", creado=self.t0 - timedelta(days=30))
        Guia.objects.create(pedido=viejo, carrier="estafeta", numero="VIEJA", proveedor="envia")
        self.filtros = {"carrier": [], "tipo": [], "estado_guia": [], "zona": [], "sku": "", "origen": "", "destino": "",
                        "dimensiones": "", "peso": (None, None)}

    def _pedido(self, cp, orden, creado):
        pedido = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp=cp, estado=Pedido.EN_TRANSITO,
                                       shopify_order_name=orden, direccion={"city": "Querétaro", "province": "Querétaro"})
        Pedido.objects.filter(pk=pedido.pk).update(creado=creado)
        return Pedido.objects.get(pk=pedido.pk)

    def _caja(self, pedido, numero, peso_real_gr, dims, listo):
        return Paquete.objects.create(
            pedido=pedido, numero=numero, peso_kg=Decimal(str(peso_real_gr / 1000)), carrier="fedex", caja=self.caja_cat,
            largo_cm=dims[0], ancho_cm=dims[1], alto_cm=dims[2], peso_real_gr=peso_real_gr, estado=Paquete.EMPACADO,
            ts_cierre=listo, ts_listo_salida=listo,
        )

    def _generar(self, es_mesa=True, **cambios):
        return fulfillment.generar(self.colima, self.inicio, self.fin, {**self.filtros, **cambios}, es_mesa)

    def _por_guia(self, resultado):
        return {f[4]: f for f in resultado["filas"]}

    def test_una_fila_por_guia_con_pesos_tiempos_e_importes(self):
        r = self._generar()
        filas = self._por_guia(r)
        self.assertEqual(set(filas), {"879", "2243", "CANC", "REPO"})  # la del pedido viejo no entra
        f = filas["879"]
        col = {n: i for i, (n, _t) in enumerate(fulfillment.COLUMNAS + fulfillment.COLUMNAS_MESA)}
        self.assertEqual((f[col["Pedido"]], f[col["Orden Shopify"]], f[col["Tipo"]], f[col["Paquetería"]], f[col["Proveedor"]], f[col["Servicio"]]),
                         (self.a.folio, "#4074", "original", "fedex", "envia", "ground"))
        self.assertEqual((f[col["Caja"]], f[col["Dimensiones cm"]], f[col["Peso real kg"]], f[col["Peso volumétrico kg"]]),
                         ("1", "40×30×26", Decimal("14.35"), Decimal("6.24")))
        self.assertEqual((f[col["Peso declarado kg"]], f[col["Peso carrier kg"]], f[col["Cobrado por"]]), (Decimal("14.35"), Decimal("15"), "carrier (repesaje)"))
        self.assertEqual((f[col["Piezas"]], f[col["SKUs"]], f[col["Lotes"]]), (3, "PARAMO-C12×1; COLIMITA-SIX×2", f"L-2026 ({self.lote.fecha_caducidad.isoformat()})"))
        self.assertEqual((f[col["CP origen"]], f[col["CP destino"]], f[col["Ciudad"]], f[col["Estado"]], f[col["Zona"]]),
                         ("01780", "76000", "Querétaro", "Querétaro", "Metropolitana"))
        self.assertEqual(f[col["Estado de la guía"]], "Entregado")
        # Tiempos: 5 h en piso; hasta salir (manifiesto 7 h) 2 h; hasta recolección del carrier (9 h) 4 h; entrega 33-9 = 24 h.
        self.assertEqual((f[col["Horas en piso"]], f[col["Horas hasta salir"]], f[col["Horas hasta recolección"]], f[col["Horas de entrega"]]),
                         (5.0, 2.0, 4.0, 24.0))
        metro = Decimal(self.tarifas["envio_bloque"]["metro"])
        self.assertEqual((f[col["Ingreso envío sin IVA"]], f[col["Ingreso envío con IVA"]], f[col["Reembolso al cliente"]]),
                         (metro, (metro * Decimal("1.16")).quantize(Decimal("0.01")), None))
        self.assertEqual((f[col["Ingreso picking"]], f[col["Ingreso empaque"]]), (Decimal(self.tarifas["alistamiento_pedido"]), Decimal(self.tarifas["empaque_pedido"])))
        self.assertEqual((f[col["Egreso cotizado al planear"]], f[col["Egreso inicial (compra)"]], f[col["Egreso final"]], f[col["Egreso final sin IVA"]], f[col["Egreso recargo carrier"]]),
                         (Decimal("297.00"), Decimal("276.00"), Decimal("300.00"), Decimal("258.62"), Decimal("24.00")))
        self.assertEqual((f[col["Margen sin IVA"]], f[col["Margen con IVA"]]),
                         (metro - Decimal("258.62"), (metro * Decimal("1.16")).quantize(Decimal("0.01")) - Decimal("300.00")))
        # 99minutos: sin crudo útil, cobrado por volumétrico vs real, sin manifiesto ni entrega → vacíos.
        g = filas["2243"]
        self.assertEqual((g[col["Peso real kg"]], g[col["Peso volumétrico kg"]], g[col["Peso declarado kg"]], g[col["Peso carrier kg"]], g[col["Cobrado por"]]),
                         (Decimal("4.0"), Decimal("2.6"), Decimal("4.0"), None, "real"))
        self.assertEqual((g[col["Horas hasta salir"]], g[col["Horas hasta recolección"]], g[col["Horas de entrega"]], g[col["Ingreso picking"]], g[col["Ingreso empaque"]]),
                         (None, None, None, None, None))
        self.assertEqual((g[col["Egreso inicial (compra)"]], g[col["Egreso final"]], g[col["Egreso recargo carrier"]]), (Decimal("131.08"), Decimal("131.08"), None))
        self.assertEqual(g[col["Lotes"]], f"L-2026 ({self.lote.fecha_caducidad.isoformat()})")

    def test_cancelada_reposicion_y_resumen(self):
        r = self._generar()
        filas = self._por_guia(r)
        col = {n: i for i, (n, _t) in enumerate(fulfillment.COLUMNAS + fulfillment.COLUMNAS_MESA)}
        local = Decimal(self.tarifas["envio_bloque"]["local"])
        canc, repo = filas["CANC"], filas["REPO"]
        self.assertEqual((canc[col["Tipo"]], canc[col["Ingreso envío sin IVA"]], canc[col["Reembolso al cliente"]], canc[col["Egreso inicial (compra)"]], canc[col["Egreso final"]]),
                         ("cancelada", Decimal("0") - local, local, Decimal("90.00"), Decimal("90.00")))
        self.assertEqual((repo[col["Tipo"]], repo[col["Ingreso envío sin IVA"]], repo[col["Zona"]]), ("reposición", local, "Local"))
        resumen = dict(r["resumen"])
        self.assertEqual((resumen["pedidos"], resumen["guías"], resumen["paquetes"], resumen["piezas"]), (2, 4, 4, 6))  # A: 3+1, B: 1+1
        self.assertEqual((resumen["original"], resumen["cancelada"], resumen["reposición"], resumen["entregadas"]), (2, 1, 1, 1))
        self.assertIn("egreso carriers (costo final) MXN", resumen)
        portal = self._generar(es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(fulfillment.COLUMNAS))
        self.assertNotIn("egreso carriers (costo final) MXN", dict(portal["resumen"]))

    def test_cortes_y_graficas(self):
        r = self._generar()
        grupos = {g["titulo"]: g for g in r["grupos"]}
        productos = grupos["Productos por guía"]["filas"]
        self.assertEqual(len(productos), 5)
        self.assertIn([self.a.folio, "879", "1", "COLIMITA-SIX", "Colimita six", 2, "L-2026", self.lote.fecha_caducidad], productos)
        diario = grupos["Volumen diario"]
        self.assertEqual(len(diario["filas"]), 8)  # todos los días del periodo, con ceros
        dia_a = timezone.localtime(self.a.creado).date().isoformat()
        self.assertIn([dia_a, 1, 2, 2, 4], diario["filas"])
        self.assertEqual(diario["grafica"]["tipo"], "columnas")
        self.assertEqual([f[1:] for f in grupos["Volumen mensual"]["filas"]][-1][1], 4 if len(grupos["Volumen mensual"]["filas"]) == 1 else grupos["Volumen mensual"]["filas"][-1][2])  # guías del mes más reciente
        self.assertEqual(grupos["Guías por pedido"]["filas"], [["2", 2, 100.0, None, None]])
        zonas = {f[0]: f for f in grupos["Guías por zona"]["filas"]}
        self.assertEqual(zonas["Metropolitana"][1:3], [2, 50.0])
        self.assertEqual(zonas["Local"][1], 2)
        self.assertEqual(grupos["Guías por zona"]["grafica"], {"tipo": "barras", "etiqueta": 0, "valor": 1, "serie": "guías"})
        self.assertEqual({f[0] for f in grupos["Guías por ciudad"]["filas"]}, {"Querétaro · Querétaro", "Ciudad de México · Ciudad de México"})
        self.assertEqual({f[0] for f in grupos["Guías por dimensiones de caja"]["filas"]}, {"40×30×26", "26×20×25"})
        self.assertEqual([f[0] for f in grupos["Guías por rango de peso real"]["filas"]], ["3-5 kg", "10-20 kg"])
        self.assertIn("Local · $129", {f[0] for f in grupos["Guías por rango cobrado (zona · tarifa)"]["filas"]})
        self.assertEqual({f[0] for f in grupos["Guías por paquetería"]["filas"]}, {"fedex", "noventa9Minutos", "estafeta"})

    def test_filtros(self):
        guias = lambda **c: set(self._por_guia(self._generar(**c)))  # noqa: E731
        self.assertEqual(guias(carrier=["fedex"]), {"879"})
        self.assertEqual(set(self._por_guia(self._generar(es_mesa=False, carrier=["fedex"]))), {"879", "2243", "CANC", "REPO"})  # solo Mesa
        self.assertEqual(guias(tipo=["cancelada", "reposición"]), {"CANC", "REPO"})
        self.assertEqual(guias(estado_guia=[Guia.ENTREGADO]), {"879"})
        self.assertEqual(guias(zona=["local"]), {"CANC", "REPO"})
        self.assertEqual(guias(sku="paramo"), {"879"})
        self.assertEqual(guias(destino="queretaro"), {"879", "2243"})
        self.assertEqual(guias(destino="76000"), {"879", "2243"})
        self.assertEqual(guias(dimensiones="40 x 30 x 26"), {"879"})
        self.assertEqual(guias(peso=(Decimal("5"), None)), {"879"})
        self.assertEqual(guias(peso=(None, Decimal("4.05"))), {"2243"})
        self.assertEqual(guias(origen="999"), set())
        r = self._generar(carrier=["fedex"])
        self.assertEqual(dict(r["resumen"])["guías"], 1)  # el resumen y los cortes siguen al filtro
        self.assertEqual(sum(f[1] for f in next(g for g in r["grupos"] if g["titulo"] == "Guías por zona")["filas"]), 1)

    def test_solo_mesa_con_csv_por_tabla_y_sin_portal(self):
        self.client.force_login(self.mesa)
        pagina = self.client.get(reverse("mesa:reporte", args=["fulfillment"]), {"cliente": "colima", "desde": (timezone.localdate() - timedelta(days=7)).isoformat()})
        self.assertContains(pagina, "Fulfillment por guía")
        self.assertContains(pagina, '<details class="desplegable" id="f-carrier">')
        self.assertContains(pagina, '<input type="checkbox" name="carrier" value="fedex" >')
        self.assertContains(pagina, "Total · 4 renglones")
        self.assertContains(pagina, '<details class="colapsable mb" name="cortes">')
        self.assertContains(pagina, "<svg xmlns")
        self.assertContains(pagina, "#4074")
        csv = self.client.get(reverse("mesa:reporte_csv", args=["fulfillment"]), {"cliente": "colima", "desde": (timezone.localdate() - timedelta(days=7)).isoformat(), "tabla": "0"})
        self.assertIn("Pedido,Guía,Caja,SKU,Producto,Piezas,Lote,Caducidad", csv.content.decode("utf-8-sig"))
        self.assertContains(self.client.get(reverse("mesa:reportes")), "Fulfillment por guía")
        self.client.force_login(self.portal)
        self.assertNotContains(self.client.get(reverse("portal:reportes")), "Fulfillment por guía")
        self.assertEqual(self.client.get(reverse("portal:reporte", args=["fulfillment"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("portal:reporte_csv", args=["fulfillment"])).status_code, 404)
