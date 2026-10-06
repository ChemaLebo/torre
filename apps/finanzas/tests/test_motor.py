"""Motor de finanzas: factura por tarifario vs costos reales.

Regla bajo prueba (Chema 2026-10-01): TODA guía comprada se cobra en el
corte de su compra, a la tarifa de la zona del CP destino (reposición,
reexpedición y cancelado incluidos); todo reembolso de la paquetería se
descuenta en el corte de su fecha. Alistamiento (picking) y empaque van una
vez por pedido, cada uno solo si se hizo. El costo es el real de cada guía;
la cancelada se reembolsa (0).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Cliente, EventoAuditoria
from apps.envios.models import Guia, Paquete
from apps.finanzas import services as finanzas
from apps.finanzas.cortes import Corte, corte_actual
from apps.finanzas.models import ReembolsoGuia

from .base import asn, crear_pedido, guia, paquete


DESDE_SIEMPRE = date(2026, 1, 1)  # almacenaje completo en cualquier corte de las pruebas


class ResumenCorteTests(TestCase):
    """El corte de hoy: lo creado "ahora" cae dentro."""

    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", facturacion_desde=DESDE_SIEMPRE)
        cls.ahora = timezone.now()
        cls.corte = corte_actual()
        cls.inicio, cls.fin = cls.corte.limites()

    def test_factura_por_guia_segun_la_zona(self):
        # Pedido local de 18.9 kg dividido en 2 paquetes → 2 guías → 2 × $129.
        local = crear_pedido(self.cliente, "PED-F0001")
        p1 = paquete(local, 1, "12.60")
        p2 = paquete(local, 2, "6.30")
        guia(local, "local", "100", p1)
        guia(local, "local", "100", p2)
        # Pedido nacional de 14.2 kg → 1 guía nacional ($219).
        nacional = crear_pedido(self.cliente, "PED-F0002", es_local=False, cp="97203")
        p3 = paquete(nacional, 1, "14.20", carrier="estafeta")
        guia(nacional, "estafeta", "213.50", p3)

        r = finanzas.resumen_corte(self.cliente, self.corte)

        self.assertEqual(r["pedidos"], 2)
        self.assertEqual(r["paquetes"], 3)
        self.assertEqual(r["guias_zona"], {"local": 2, "metro": 0, "nacional": 1})
        self.assertEqual(r["ingresos"]["envio"], Decimal("477"))          # 129 + 129 + 219
        self.assertEqual(r["ingresos"]["almacenaje"], Decimal("9000"))    # la mitad del mes por corte
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("50"))    # 2 × 25: por pedido
        self.assertEqual(r["ingresos"]["empaque"], Decimal("130"))        # 2 × 65: por pedido
        self.assertEqual(r["ingresos"]["total"], Decimal("9657"))
        self.assertEqual((r["ingresos"]["iva"], r["ingresos"]["total_con_iva"]), (Decimal("1545.12"), Decimal("11202.12")))
        self.assertEqual(r["costos"]["carrier"], Decimal("413.50"))
        self.assertEqual(r["costos"]["insumos"], Decimal("36"))           # 3 guías × 12
        self.assertEqual(r["margen_bruto"], Decimal("9207.50"))
        # Dos estados de cuenta: fulfillment y guías, cada uno con IVA.
        ful, gui = r["facturas"]["fulfillment"], r["facturas"]["guias"]
        self.assertEqual([l["concepto"] for l in ful["lineas"]], ["Almacenaje", "Picking (alistamiento)", "Empaque"])
        self.assertEqual([l["importe"] for l in ful["lineas"]], [Decimal("9000"), Decimal("50"), Decimal("130")])
        self.assertEqual((ful["subtotal"], ful["iva"], ful["total"]), (Decimal("9180"), Decimal("1468.80"), Decimal("10648.80")))
        self.assertEqual([(l["guia"].numero, l["tarifa"], l["neto"]) for l in gui["lineas"]], [("G-PED-F0001-local", Decimal("129"), Decimal("129")), ("G-PED-F0001-local", Decimal("129"), Decimal("129")), ("G-PED-F0002-estafeta", Decimal("219"), Decimal("219"))])
        self.assertEqual((gui["subtotal"], gui["iva"], gui["total"], gui["previos"]), (Decimal("477"), Decimal("76.32"), Decimal("553.32"), []))
        # Desglose por estado, con datos reales de guías:
        cdmx = r["estados"]["Ciudad de México"]
        self.assertEqual(cdmx["ordenes"], 1)
        self.assertEqual(cdmx["guias"], 2)
        self.assertEqual(cdmx["zona"], "local")
        self.assertAlmostEqual(cdmx["peso"], 18.0, places=1)              # 18.9 sin el +5%
        self.assertEqual(cdmx["costo"], Decimal("200"))
        self.assertEqual(cdmx["facturado"], Decimal("258"))
        yuc = r["estados"]["Yucatán"]
        self.assertEqual(yuc["ordenes"], 1)
        self.assertEqual(yuc["zona"], "nacional")
        self.assertEqual(yuc["facturado"], Decimal("219"))

    def test_pedido_pesado_con_una_sola_guia_cobra_una(self):
        # 39 kg en una guía sin caja (pedido sin plan): un cobro; los bloques ya no existen.
        pesado = crear_pedido(self.cliente, "PED-F0003")
        for i, peso in enumerate(("18.90", "18.90", "1.20"), start=1):
            paquete(pesado, i, peso)
        guia(pesado, "local", "300")
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["guias_zona"]["local"], 1)
        self.assertEqual(r["ingresos"]["envio"], Decimal("129"))
        self.assertAlmostEqual(r["estados"]["Ciudad de México"]["peso"], 37.14, places=1)  # 39 kg sin el +5%

    def test_zona_metro_por_carrier_puntopost(self):
        gdl = crear_pedido(self.cliente, "PED-F0004", es_local=False, cp="44100")
        p1 = paquete(gdl, 1, "5.98", carrier="puntopost", precio="91")
        guia(gdl, "puntopost", "91", p1)
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["guias_zona"], {"local": 0, "metro": 1, "nacional": 0})
        self.assertEqual(r["ingresos"]["envio"], Decimal("169"))
        jal = r["estados"]["Jalisco"]
        self.assertEqual(jal["zona"], "metro")
        self.assertEqual(jal["facturado"], Decimal("169"))

    def test_override_de_tarifario_por_cliente(self):
        premium = Cliente.objects.create(
            nombre="Marca Premium", slug="premium", facturacion_desde=DESDE_SIEMPRE,
            tarifario={"almacenaje_mes": 25000, "envio_bloque": {"local": 150}},
        )
        pedido = crear_pedido(premium, "PED-F0005")
        p1 = paquete(pedido, 1, "10.00")
        guia(pedido, "local", "100", p1)
        r = finanzas.resumen_corte(premium, self.corte)
        self.assertEqual(r["ingresos"]["almacenaje"], Decimal("12500"))
        self.assertEqual(r["ingresos"]["envio"], Decimal("150"))
        self.assertEqual(r["tarifario"]["envio_bloque"]["nacional"], 219)  # el resto sigue el default

    def test_reexpedicion_se_cobra_pero_no_repite_picking_ni_empaque(self):
        # Chema 2026-10-01: la guía nueva de una caja que ya tuvo guía en un
        # corte anterior se cobra (nosotros la pagamos); el trabajo de piso
        # ya se facturó entonces.
        pedido = crear_pedido(self.cliente, "PED-F0006")
        p1 = paquete(pedido, 1, "10.00")
        vieja = guia(pedido, "estafeta", "213.50", p1)
        Guia.objects.filter(pk=vieja.pk).update(creado=self.inicio - timedelta(days=30))
        guia(pedido, "estafeta", "213.50", p1)  # reexpedición dentro del periodo
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["pedidos"], 1)
        self.assertEqual(r["reexpediciones"], 1)
        self.assertEqual(r["ingresos"]["envio"], Decimal("129"))
        self.assertEqual((r["ingresos"]["alistamiento"], r["ingresos"]["empaque"]), (Decimal("0"), Decimal("0")))
        self.assertEqual(r["costos"]["carrier"], Decimal("213.50"))
        self.assertEqual(r["costos"]["insumos"], Decimal("12"))  # re-empaque sí cuesta
        fila = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"][0]
        self.assertEqual(fila["nota"], "reexpedición")

    def test_cancelado_con_guia_viva_paga_todo(self):
        # Se empacó (la guía viva es la evidencia) y luego se canceló sin
        # cancelar la guía con el carrier: el trabajo de piso y la guía que
        # WOP pagó se cobran; el día que el carrier la reembolse, se descuenta.
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.cliente, "PED-F0007", es_local=False, cp="97203")
        p1 = paquete(pedido, 1, "10.00", carrier="estafeta")
        guia(pedido, "estafeta", "185.20", p1)
        Pedido.objects.filter(pk=pedido.pk).update(estado="CANCELADO")
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual((r["pedidos"], r["cancelados"]), (1, 1))
        self.assertEqual(r["ingresos"]["envio"], Decimal("219"))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("25"))
        self.assertEqual(r["ingresos"]["empaque"], Decimal("65"))
        self.assertEqual(r["costos"]["carrier"], Decimal("185.20"))
        fila = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"][0]
        self.assertEqual((fila["nota"], fila["cobra_envio"]), ("cancelado", True))

    def test_pickeado_y_cancelado_antes_de_empacar_paga_solo_picking(self):
        # La caja nunca tuvo guía: entra al corte de la CANCELACIÓN (evento de
        # auditoría; `actualizado` si no hay), con picking y sin empaque
        # (Chema 2026-10-01: así se ve lo que se pickeó y no se empacó).
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.cliente, "PED-F0011", estado=Pedido.CANCELADO, ts_picking=self.ahora)
        paquete(pedido, 1, "5.00")
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual((r["pedidos"], r["guias"], r["paquetes"]), (1, 0, 0))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("25"))
        self.assertEqual(r["ingresos"]["empaque"], Decimal("0"))
        self.assertEqual((r["ingresos"]["envio"], r["costos"]["total"]), (Decimal("0"), Decimal("0")))
        fila = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"][0]
        self.assertIsNone(fila["guia"])
        self.assertEqual(fila["nota"], "sin guía · cancelado · pickeado sin empacar")
        # Cerrada la caja (empaque hecho) el empaque también se cobra, aunque se cancele.
        Paquete.objects.filter(pedido=pedido).update(ts_cierre=self.ahora)
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual((r["ingresos"]["empaque"], r["paquetes"]), (Decimal("65"), 1))
        # La cancelación registrada en auditoría manda sobre `actualizado`: en
        # otro corte, la caja sale de este.
        EventoAuditoria.objects.create(
            entidad="pedido", entidad_id=str(pedido.pk), accion="cambio_estado",
            delta={"de": "EN_PICKING", "a": "CANCELADO"}, cliente=self.cliente,
        )
        EventoAuditoria.objects.filter(entidad_id=str(pedido.pk)).update(ts=self.inicio - timedelta(days=10))
        self.assertEqual(finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"], [])
        # Un pedido cancelado sin picking no deja fila: no hubo trabajo que cobrar.
        nada = crear_pedido(self.cliente, "PED-F0012", estado=Pedido.CANCELADO)
        paquete(nada, 1, "5.00")
        self.assertEqual(finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"], [])

    def test_zona_sale_del_cp_no_del_carrier(self):
        # Pedido a Guadalajara despachado vía estafeta: se factura METRO igual.
        # El ruteo interno jamás mueve la factura del cliente.
        gdl = crear_pedido(self.cliente, "PED-F0008", es_local=False, cp="44100")
        p1 = paquete(gdl, 1, "14.20", carrier="estafeta")
        guia(gdl, "estafeta", "213.50", p1)
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["guias_zona"], {"local": 0, "metro": 1, "nacional": 0})
        self.assertEqual(r["ingresos"]["envio"], Decimal("169"))

    def test_peso_facturable_descuenta_margen_de_empaque(self):
        # 19.5 kg planeados (con +5% de relleno) = 18.57 kg reales en la guía.
        pedido = crear_pedido(self.cliente, "PED-F0009")
        paquete(pedido, 1, "13.20")
        paquete(pedido, 2, "6.30")
        guia(pedido, "local", "200")
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["guias_zona"]["local"], 1)
        self.assertAlmostEqual(r["estados"]["Ciudad de México"]["peso"], 18.57, places=1)

    def test_guia_cancelada_historica_y_caja_de_reposicion(self):
        # Guía CANCELADA sin registro de reembolso (anterior al módulo): cuenta
        # como reembolsada en su corte, neto 0 y costo 0. La caja de reposición
        # se cobra como cualquier guía (Chema 2026-10-01).
        from apps.catalogo.models import SKU
        from apps.envios.models import PaqueteLinea
        from apps.pedidos.models import LineaPedido

        pedido = crear_pedido(self.cliente, "PED-F0010")
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six")
        original = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=1, cantidad_repuesta=1)
        p1 = paquete(pedido, 1, "5.00")
        PaqueteLinea.objects.create(paquete=p1, linea_pedido=original, cantidad=1)
        p2 = paquete(pedido, 2, "5.00")
        PaqueteLinea.objects.create(paquete=p2, linea_pedido=original, cantidad=1, repone_a=p1)  # misma línea, repone a la caja 1
        cancelada = guia(pedido, "estafeta", "150", p1)
        Guia.objects.filter(pk=cancelada.pk).update(estado=Guia.CANCELADA)
        Guia.objects.create(pedido=pedido, paquete=p1, carrier="local", numero="L-1", costo_preferencial=Decimal("100"))
        Guia.objects.create(pedido=pedido, paquete=p2, carrier="local", numero="L-2", costo_preferencial=Decimal("100"))
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["guias_zona"]["local"], 3)              # las tres guías cobran; la cancelada se neta
        self.assertEqual(r["ingresos"]["envio"], Decimal("258"))   # 129 + 129; la cancelada 129 − 129
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("25"))  # el pedido, una vez
        self.assertEqual(r["ingresos"]["empaque"], Decimal("65"))
        self.assertEqual(r["costos"]["insumos"], Decimal("24"))      # 2 bultos; la cancelada no
        self.assertEqual(r["costos"]["carrier"], Decimal("200"))     # la cancelada se reembolsa: 0
        filas = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"]
        self.assertEqual([f["nota"] for f in filas], ["guía cancelada · reembolsada", "", "reposición"])
        self.assertEqual((filas[0]["transporte"], filas[0]["reembolso"], filas[0]["costo"]), (Decimal("129"), Decimal("129"), Decimal("0")))
        self.assertEqual((filas[0]["picking"], filas[1]["picking"]), (Decimal("0"), Decimal("25")))  # el trabajo va en la primera fila con guía viva


class ReembolsosTests(TestCase):
    """Reembolsos de paquetería por guía: la fecha decide el corte."""

    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", facturacion_desde=DESDE_SIEMPRE)
        cls.ahora = timezone.now()
        cls.corte = corte_actual()
        cls.inicio, cls.fin = cls.corte.limites()

    def test_registrar_valida_y_toma_la_tarifa_por_default(self):
        pedido = crear_pedido(self.cliente, "PED-R0001", es_local=False, cp="97203")
        g = guia(pedido, "estafeta", "185.20", paquete(pedido, 1, "10.00", carrier="estafeta"))
        r = finanzas.registrar_reembolso(g, "mesa1", origen=ReembolsoGuia.ORIGEN_RECLAMACION, nota="99minutos pagó la reclamación")
        self.assertEqual((r.monto, r.origen, r.registrado_por, r.cliente), (Decimal("219"), "reclamacion", "mesa1", self.cliente))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(g.pk), accion="reembolso_registrado").exists())
        with self.assertRaises(ValueError):
            finanzas.registrar_reembolso(g, "mesa1", origen="ajuste", monto=0)
        with self.assertRaises(ValueError):
            finanzas.registrar_reembolso(g, "mesa1", origen="ajuste", fecha=g.creado - timedelta(days=1))
        finanzas.quitar_reembolso(r, "mesa1", motivo="se registró dos veces")
        self.assertFalse(ReembolsoGuia.objects.filter(pk=r.pk).exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(g.pk), accion="reembolso_quitado").exists())

    def test_mismo_corte_se_neta_y_corte_posterior_resta(self):
        pedido = crear_pedido(self.cliente, "PED-R0002")
        g = guia(pedido, "local", "100", paquete(pedido, 1, "5.00"))
        finanzas.registrar_reembolso(g, "mesa1", origen="ajuste", nota="la paquetería la reembolsó")
        f = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)
        fila = f["filas"][0]
        self.assertEqual((fila["transporte"], fila["reembolso"], fila["nota"]), (Decimal("129"), Decimal("129"), "reembolsada"))
        self.assertEqual(f["reembolsos_previos"], [])
        self.assertEqual(finanzas.resumen_corte(self.cliente, self.corte)["ingresos"]["envio"], Decimal("0"))
        # Guía de un corte anterior reembolsada en este: no toca aquel corte y
        # resta en este como "reembolso de corte anterior".
        vieja = crear_pedido(self.cliente, "PED-R0003", es_local=False, cp="97203")
        gv = guia(vieja, "estafeta", "200", paquete(vieja, 1, "5.00", carrier="estafeta"))
        Guia.objects.filter(pk=gv.pk).update(creado=self.inicio - timedelta(days=20))
        gv.refresh_from_db()
        finanzas.registrar_reembolso(gv, "mesa1", origen="reclamacion", monto="219")
        antes = finanzas.facturar_guias(self.cliente, self.inicio - timedelta(days=30), self.inicio)
        self.assertEqual((antes["filas"][0]["transporte"], antes["filas"][0]["reembolso"]), (Decimal("219"), Decimal("0")))
        ahora = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)
        self.assertEqual([r.guia_id for r in ahora["reembolsos_previos"]], [gv.pk])
        self.assertEqual(finanzas.resumen_corte(self.cliente, self.corte)["ingresos"]["envio"], Decimal("-219"))

    def test_cancelar_guia_registra_el_reembolso(self):
        from apps.envios.services import cancelar_guia

        pedido = crear_pedido(self.cliente, "PED-R0004", ts_empacado=self.ahora)
        g = guia(pedido, "local", "100", paquete(pedido, 1, "5.00"))
        cancelar_guia(g, "mesa1", motivo="Cambio de dirección")
        r = g.reembolsos.get()
        self.assertEqual((r.monto, r.origen, r.nota), (Decimal("129"), "cancelacion", "Cambio de dirección"))
        f = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"][0]
        self.assertEqual((f["transporte"], f["reembolso"], f["costo"], f["nota"]), (Decimal("129"), Decimal("129"), Decimal("0"), "guía cancelada · reembolsada"))
        # Con tarifa 0 no hay nada que devolver.
        gratis = Cliente.objects.create(nombre="Gratis", slug="gratis", tarifario={"envio_bloque": {"local": 0}})
        pedido2 = crear_pedido(gratis, "PED-R0005")
        g2 = guia(pedido2, "local", "100", paquete(pedido2, 1, "5.00"))
        cancelar_guia(g2, "mesa1")
        self.assertFalse(g2.reembolsos.exists())

    def test_iva(self):
        self.assertEqual(finanzas.con_iva(Decimal("100")), (Decimal("16.00"), Decimal("116.00")))
        self.assertEqual(finanzas.con_iva(Decimal("0")), (Decimal("0.00"), Decimal("0.00")))


class RecepcionPorTarimaTests(TestCase):
    """Modelo B: la recepción se factura a $X/tarima por ASN descargada en el corte."""

    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(
            nombre="Marca Modelo B", slug="modelo-b", facturacion_desde=DESDE_SIEMPRE,
            tarifario={"recepcion_tarima": 190},
        )
        cls.ahora = timezone.now()
        cls.corte = corte_actual()
        cls.inicio, cls.fin = cls.corte.limites()

    def test_asn_cerrada_factura_lo_contado_en_piso(self):
        # 16 tarimas contadas al cerrar (15 anunciadas: lo contado manda).
        asn(self.cliente, tarimas=15, tarimas_recibidas=16, descarga=self.ahora)
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["tarimas"], 16)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("3040"))  # 16 × 190
        self.assertEqual(r["ingresos"]["fulfillment"], Decimal("9000") + Decimal("3040"))
        self.assertEqual(r["facturas"]["fulfillment"]["lineas"][-1], {"concepto": "Recepción", "detalle": "16 tarimas × $190", "importe": Decimal("3040")})

    def test_asn_del_mes_anterior_no_factura(self):
        asn(self.cliente, tarimas_recibidas=10, descarga=self.inicio - timedelta(days=5))
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["tarimas"], 0)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("0"))

    def test_asn_anunciada_sin_descarga_no_factura(self):
        asn(self.cliente, estado="ANUNCIADA", tarimas=20)
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("0"))

    def test_sin_conteo_de_piso_factura_lo_anunciado(self):
        asn(self.cliente, tarimas=12, tarimas_recibidas=0, descarga=self.ahora)
        r = finanzas.resumen_corte(self.cliente, self.corte)
        self.assertEqual(r["tarimas"], 12)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("2280"))

    def test_cliente_default_no_cobra_recepcion_ni_minimo(self):
        colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        asn(colima, tarimas_recibidas=30, descarga=self.ahora)
        r = finanzas.resumen_corte(colima, self.corte)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("0"))
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("0"))


class MinimoMensualTests(TestCase):
    """Modelo B: mínimo mensual como línea de ajuste, evaluado en el 2º corte
    con el mes completo (los dos cortes)."""

    @classmethod
    def setUpTestData(cls):
        cls.corte = Corte(2026, 9, 2)
        cls.inicio, cls.fin = cls.corte.limites()

    def test_actividad_bajo_el_minimo_ajusta_el_total_en_el_segundo_corte(self):
        cliente = Cliente.objects.create(
            nombre="Marca Chica", slug="marca-chica", facturacion_desde=DESDE_SIEMPRE,
            tarifario={"minimo_mes": 12000, "almacenaje_mes": 8000},
        )
        r = finanzas.resumen_corte(cliente, self.corte)
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("4000"))  # 4000 + 4000 del mes < 12000
        self.assertEqual(r["ingresos"]["total"], Decimal("8000"))
        # El ajuste es línea aparte: no infla fulfillment ni envío.
        self.assertEqual(r["ingresos"]["fulfillment"], Decimal("4000"))
        self.assertEqual(r["ingresos"]["envio"], Decimal("0"))
        self.assertEqual(r["facturas"]["fulfillment"]["lineas"][-1]["concepto"], "Ajuste a mínimo mensual")
        # En el 1er corte no se evalúa.
        self.assertEqual(finanzas.resumen_corte(cliente, self.corte.anterior())["ingresos"]["ajuste_minimo"], Decimal("0"))

    def test_actividad_sobre_el_minimo_no_ajusta(self):
        cliente = Cliente.objects.create(
            nombre="Marca Grande", slug="marca-grande", facturacion_desde=DESDE_SIEMPRE,
            tarifario={"minimo_mes": 12000, "almacenaje_mes": 15000},
        )
        r = finanzas.resumen_corte(cliente, self.corte)
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("0"))  # 7500 + 7500 ≥ 12000
        self.assertEqual(r["ingresos"]["total"], Decimal("7500"))

    def test_ahorro_vs_melonn_usa_el_total_con_minimo(self):
        # Con 1 pedido facturable el benchmark es 357; el ahorro se calcula
        # sobre lo que el cliente PAGA (total ya con mínimo aplicado).
        cliente = Cliente.objects.create(
            nombre="Marca Piso", slug="marca-piso", facturacion_desde=DESDE_SIEMPRE,
            tarifario={"minimo_mes": 12000, "almacenaje_mes": 0},
        )
        pedido = crear_pedido(cliente, "PED-F0200")
        p1 = paquete(pedido, 1, "10.00")
        g = guia(pedido, "local", "100", p1)
        Guia.objects.filter(pk=g.pk).update(creado=self.inicio + timedelta(days=1))
        r = finanzas.resumen_corte(cliente, self.corte)
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("11781"))  # 12000 − (129 + 25 + 65)
        self.assertEqual(r["ingresos"]["total"], Decimal("12000"))
        esperado = round(float((r["benchmark"] - Decimal("12000")) / r["benchmark"]) * 100, 1)
        self.assertEqual(r["ahorro_pct"], esperado)


class AlmacenajePorCorteTests(TestCase):
    """Almacenaje: la mitad de la tarifa mensual por corte, prorrateada por
    los días desde `facturacion_desde` o el primer pedido (Chema 2026-10-01:
    Colima empezó el 21 de septiembre → un tercio del mes)."""

    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        pedido = crear_pedido(cls.colima, "PED-A0001")
        from apps.pedidos.models import Pedido

        Pedido.objects.filter(pk=pedido.pk).update(creado=timezone.make_aware(datetime(2026, 9, 21, 10, 0)))
        cls.tarifas = finanzas.tarifario_de(cls.colima)

    def test_primer_corte_prorrateado_desde_el_primer_pedido(self):
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 9, 1), self.tarifas), (Decimal("0.00"), 0))
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 9, 2), self.tarifas), (Decimal("6000.00"), 10))   # 9000 × 10/15
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 10, 1), self.tarifas), (Decimal("9000.00"), 15))
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 10, 2), self.tarifas, hoy=date(2026, 10, 20)), (Decimal("9000.00"), 16))  # 16 de 16 días
        r = finanzas.resumen_corte(self.colima, Corte(2026, 9, 2))
        self.assertEqual(r["facturas"]["fulfillment"]["lineas"][0], {"concepto": "Almacenaje", "detalle": "½ de $18,000 al mes · 10 de 15 días", "importe": Decimal("6000.00")})

    def test_facturacion_desde_manda_sobre_el_primer_pedido(self):
        self.colima.facturacion_desde = date(2026, 9, 25)
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 9, 2), self.tarifas), (Decimal("3600.00"), 6))  # 9000 × 6/15

    def test_corte_futuro_cliente_inactivo_o_sin_pedidos_no_cobra(self):
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 10, 1), self.tarifas, hoy=date(2026, 9, 1)), (Decimal("0.00"), 15))
        self.colima.activo = False
        self.assertEqual(finanzas.almacenaje_del_corte(self.colima, Corte(2026, 10, 1), self.tarifas), (Decimal("0.00"), 15))
        nuevo = Cliente.objects.create(nombre="Sin pedidos", slug="sin-pedidos")
        self.assertEqual(finanzas.almacenaje_del_corte(nuevo, Corte(2026, 10, 1), self.tarifas), (Decimal("0.00"), 0))
