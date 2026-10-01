"""Motor de finanzas: factura por tarifario vs costos reales.

Regla bajo prueba (Chema 2026-09-28): el envío se factura POR GUÍA a la
tarifa de la zona del CP destino; cada guía comprada es un cobro. Alistamiento
(picking) y empaque van una vez por pedido, cada uno solo si se hizo (Chema
2026-10-01). El costo es el real de cada guía; la cancelada se reembolsa (0).
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Cliente
from apps.envios.models import Guia, Paquete
from apps.finanzas import services as finanzas

from .base import asn, crear_pedido, guia, paquete


class ResumenMesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.ahora = timezone.now()
        cls.inicio = cls.ahora - timedelta(days=1)
        cls.fin = cls.ahora + timedelta(days=1)

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

        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)

        self.assertEqual(r["pedidos"], 2)
        self.assertEqual(r["paquetes"], 3)
        self.assertEqual(r["guias_zona"], {"local": 2, "metro": 0, "nacional": 1})
        self.assertEqual(r["ingresos"]["envio"], Decimal("477"))          # 129 + 129 + 219
        self.assertEqual(r["ingresos"]["almacenaje"], Decimal("18000"))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("50"))    # 2 × 25: por pedido
        self.assertEqual(r["ingresos"]["empaque"], Decimal("130"))        # 2 × 65: por pedido
        self.assertEqual(r["ingresos"]["total"], Decimal("18657"))
        self.assertEqual(r["costos"]["carrier"], Decimal("413.50"))
        self.assertEqual(r["costos"]["insumos"], Decimal("36"))           # 3 guías × 12
        self.assertEqual(r["margen_bruto"], Decimal("18207.50"))
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
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["guias_zona"]["local"], 1)
        self.assertEqual(r["ingresos"]["envio"], Decimal("129"))
        self.assertAlmostEqual(r["estados"]["Ciudad de México"]["peso"], 37.14, places=1)  # 39 kg sin el +5%

    def test_zona_metro_por_carrier_puntopost(self):
        gdl = crear_pedido(self.cliente, "PED-F0004", es_local=False, cp="44100")
        p1 = paquete(gdl, 1, "5.98", carrier="puntopost", precio="91")
        p2 = paquete(gdl, 2, "5.98", carrier="puntopost", precio="91")
        guia(gdl, "puntopost", "91", p1)
        guia(gdl, "puntopost", "91", p2)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["guias_zona"], {"local": 0, "metro": 2, "nacional": 0})
        self.assertEqual(r["ingresos"]["envio"], Decimal("338"))          # 2 guías metro
        self.assertEqual(r["costos"]["carrier"], Decimal("182"))

    def test_override_de_tarifario_por_cliente(self):
        self.cliente.tarifario = {"almacenaje_mes": 0, "envio_bloque": {"local": 150}}
        self.cliente.save(update_fields=["tarifario"])
        pedido = crear_pedido(self.cliente, "PED-F0005")
        p1 = paquete(pedido, 1, "10.00")
        guia(pedido, "local", "100", p1)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["tarifario"]["envio_bloque"]["local"], 150)
        self.assertEqual(r["tarifario"]["envio_bloque"]["nacional"], 219)  # el resto no se pierde
        self.assertEqual(r["ingresos"]["almacenaje"], Decimal("0"))
        self.assertEqual(r["ingresos"]["envio"], Decimal("150"))

    def test_reexpedicion_suma_costo_e_insumos_pero_no_refactura(self):
        pedido = crear_pedido(self.cliente, "PED-F0006")
        p1 = paquete(pedido, 1, "10.00")
        vieja = guia(pedido, "estafeta", "213.50", p1)
        Guia.objects.filter(pk=vieja.pk).update(creado=self.inicio - timedelta(days=30))
        guia(pedido, "estafeta", "213.50", p1)  # reexpedición dentro del mes
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["pedidos"], 0)
        self.assertEqual(r["reexpediciones"], 1)
        self.assertEqual(r["ingresos"]["envio"], Decimal("0"))
        self.assertEqual(r["costos"]["carrier"], Decimal("213.50"))
        self.assertEqual(r["costos"]["insumos"], Decimal("12"))  # re-empaque sí cuesta

    def test_cancelado_con_guia_viva_paga_picking_y_empaque_pero_no_envio(self):
        # Se empacó (la guía viva es la evidencia) y luego se canceló: el trabajo
        # de piso se cobra, el envío no; la guía que WOP pagó sí cuesta.
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.cliente, "PED-F0007", es_local=False, cp="97203")
        p1 = paquete(pedido, 1, "10.00", carrier="estafeta")
        guia(pedido, "estafeta", "185.20", p1)
        Pedido.objects.filter(pk=pedido.pk).update(estado="CANCELADO")
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["pedidos"], 1)
        self.assertEqual(r["cancelados"], 1)
        self.assertEqual(r["ingresos"]["envio"], Decimal("0"))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("25"))
        self.assertEqual(r["ingresos"]["empaque"], Decimal("65"))
        self.assertEqual(r["costos"]["carrier"], Decimal("185.20"))
        fila = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"][0]
        self.assertEqual((fila["nota"], fila["cobra_envio"]), ("cancelado", False))

    def test_pickeado_y_cancelado_antes_de_empacar_paga_solo_picking(self):
        # La caja nunca tuvo guía: entra al periodo por su fecha de plan, con
        # picking y sin empaque (Chema 2026-10-01: así se ve lo que se pickeó
        # y no se empacó por una cancelación).
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.cliente, "PED-F0011", estado=Pedido.CANCELADO, ts_picking=self.ahora)
        paquete(pedido, 1, "5.00")
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual((r["pedidos"], r["guias"], r["paquetes"]), (1, 0, 0))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("25"))
        self.assertEqual(r["ingresos"]["empaque"], Decimal("0"))
        self.assertEqual((r["ingresos"]["envio"], r["costos"]["total"]), (Decimal("0"), Decimal("0")))
        fila = finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"][0]
        self.assertIsNone(fila["guia"])
        self.assertEqual(fila["nota"], "sin guía · cancelado · pickeado sin empacar")
        # Cerrada la caja (empaque hecho) el empaque también se cobra, aunque se cancele.
        Paquete.objects.filter(pedido=pedido).update(ts_cierre=self.ahora)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual((r["ingresos"]["empaque"], r["paquetes"]), (Decimal("65"), 1))

    def test_zona_sale_del_cp_no_del_carrier(self):
        # Pedido a Guadalajara despachado vía estafeta: se factura METRO igual.
        # El ruteo interno jamás mueve la factura del cliente.
        gdl = crear_pedido(self.cliente, "PED-F0008", es_local=False, cp="44100")
        p1 = paquete(gdl, 1, "14.20", carrier="estafeta")
        guia(gdl, "estafeta", "213.50", p1)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["guias_zona"], {"local": 0, "metro": 1, "nacional": 0})
        self.assertEqual(r["ingresos"]["envio"], Decimal("169"))

    def test_peso_facturable_descuenta_margen_de_empaque(self):
        # 19.5 kg planeados (con +5% de relleno) = 18.57 kg reales en la guía.
        pedido = crear_pedido(self.cliente, "PED-F0009")
        paquete(pedido, 1, "13.20")
        paquete(pedido, 2, "6.30")
        guia(pedido, "local", "200")
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["guias_zona"]["local"], 1)
        self.assertAlmostEqual(r["estados"]["Ciudad de México"]["peso"], 18.57, places=1)

    def test_guia_cancelada_y_caja_de_reposicion_van_sin_cargo(self):
        from apps.catalogo.models import SKU
        from apps.envios.models import PaqueteLinea
        from apps.pedidos.models import LineaPedido

        pedido = crear_pedido(self.cliente, "PED-F0010")
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six")
        original = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=1)
        repuesta = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=1, reposicion_de=original)
        p1 = paquete(pedido, 1, "5.00")
        PaqueteLinea.objects.create(paquete=p1, linea_pedido=original, cantidad=1)
        p2 = paquete(pedido, 2, "5.00")
        PaqueteLinea.objects.create(paquete=p2, linea_pedido=repuesta, cantidad=1)
        cancelada = guia(pedido, "estafeta", "150", p1)
        Guia.objects.filter(pk=cancelada.pk).update(estado=Guia.CANCELADA)
        Guia.objects.create(pedido=pedido, paquete=p1, carrier="local", numero="L-1", costo_preferencial=Decimal("100"))
        Guia.objects.create(pedido=pedido, paquete=p2, carrier="local", numero="L-2", costo_preferencial=Decimal("100"))
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["guias_zona"]["local"], 1)              # solo la caja original viva
        self.assertEqual(r["ingresos"]["envio"], Decimal("129"))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("25"))  # el pedido, una vez
        self.assertEqual(r["ingresos"]["empaque"], Decimal("65"))
        self.assertEqual(r["costos"]["insumos"], Decimal("24"))      # 2 bultos; la cancelada no
        self.assertEqual(r["costos"]["carrier"], Decimal("200"))     # la cancelada se reembolsa: 0
        notas = [f["nota"] for f in finanzas.facturar_guias(self.cliente, self.inicio, self.fin)["filas"]]
        self.assertEqual(notas, ["guía cancelada (sin cargo)", "", "reposición (sin cargo)"])


class RecepcionPorTarimaTests(TestCase):
    """Modelo B: la recepción se factura a $X/tarima por ASN descargada en el mes."""

    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(
            nombre="Marca Modelo B", slug="modelo-b",
            tarifario={"recepcion_tarima": 190},
        )
        cls.ahora = timezone.now()
        cls.inicio = cls.ahora - timedelta(days=1)
        cls.fin = cls.ahora + timedelta(days=1)

    def test_asn_cerrada_factura_lo_contado_en_piso(self):
        # 16 tarimas contadas al cerrar (15 anunciadas: lo contado manda).
        asn(self.cliente, tarimas=15, tarimas_recibidas=16, descarga=self.ahora)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["tarimas"], 16)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("3040"))  # 16 × 190
        # Va DENTRO de fulfillment y del total (almacenaje default 18,000).
        self.assertEqual(r["ingresos"]["fulfillment"], Decimal("21040"))
        self.assertEqual(r["ingresos"]["total"], Decimal("21040"))

    def test_asn_del_mes_anterior_no_factura(self):
        asn(self.cliente, tarimas_recibidas=16, descarga=self.inicio - timedelta(days=30))
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["tarimas"], 0)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("0"))

    def test_asn_anunciada_sin_descarga_no_factura(self):
        asn(self.cliente, estado="ANUNCIADA", tarimas=10, descarga=None)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("0"))

    def test_sin_conteo_de_piso_factura_lo_anunciado(self):
        # tarimas_recibidas=0 (nadie contó al cerrar) → vale lo anunciado.
        asn(self.cliente, estado="RECIBIDA", tarimas=4, tarimas_recibidas=0, descarga=self.ahora)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["tarimas"], 4)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("760"))  # 4 × 190

    def test_cliente_default_no_cobra_recepcion_ni_minimo(self):
        colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        asn(colima, tarimas=15, tarimas_recibidas=16, descarga=self.ahora)
        r = finanzas.resumen_mes(colima, self.inicio, self.fin)
        self.assertEqual(r["ingresos"]["recepcion"], Decimal("0"))
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("0"))
        self.assertEqual(r["ingresos"]["total"], Decimal("18000"))  # solo almacenaje flat


class MinimoMensualTests(TestCase):
    """Modelo B: piso de factura mensual como línea de ajuste aparte."""

    @classmethod
    def setUpTestData(cls):
        cls.ahora = timezone.now()
        cls.inicio = cls.ahora - timedelta(days=1)
        cls.fin = cls.ahora + timedelta(days=1)

    def test_actividad_bajo_el_minimo_ajusta_el_total(self):
        cliente = Cliente.objects.create(
            nombre="Marca Chica", slug="marca-chica",
            tarifario={"minimo_mes": 12000, "almacenaje_mes": 8000},
        )
        r = finanzas.resumen_mes(cliente, self.inicio, self.fin)
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("4000"))
        self.assertEqual(r["ingresos"]["total"], Decimal("12000"))
        # El ajuste es línea aparte: no infla fulfillment ni envío.
        self.assertEqual(r["ingresos"]["fulfillment"], Decimal("8000"))
        self.assertEqual(r["ingresos"]["envio"], Decimal("0"))

    def test_actividad_sobre_el_minimo_no_ajusta(self):
        cliente = Cliente.objects.create(
            nombre="Marca Grande", slug="marca-grande",
            tarifario={"minimo_mes": 12000, "almacenaje_mes": 15000},
        )
        r = finanzas.resumen_mes(cliente, self.inicio, self.fin)
        self.assertEqual(r["ingresos"]["ajuste_minimo"], Decimal("0"))
        self.assertEqual(r["ingresos"]["total"], Decimal("15000"))

    def test_ahorro_vs_melonn_usa_el_total_con_minimo(self):
        # Con 1 pedido facturable el benchmark es 357; el ahorro se calcula
        # sobre lo que el cliente PAGA (total ya con mínimo aplicado).
        cliente = Cliente.objects.create(
            nombre="Marca Piso", slug="marca-piso",
            tarifario={"minimo_mes": 12000, "almacenaje_mes": 0},
        )
        pedido = crear_pedido(cliente, "PED-F0200")
        p1 = paquete(pedido, 1, "10.00")
        guia(pedido, "local", "100", p1)
        r = finanzas.resumen_mes(cliente, self.inicio, self.fin)
        self.assertEqual(r["ingresos"]["total"], Decimal("12000"))
        esperado = round(float((r["benchmark"] - Decimal("12000")) / r["benchmark"]) * 100, 1)
        self.assertEqual(r["ahorro_pct"], esperado)
