"""Finanzas de Mesa: factura por tarifario (por pedido/bloque) vs costos reales.

Regla bajo prueba: el envío se factura por PEDIDO por bloque de 20 kg según
zona — nunca por guía — para que dividir/consolidar paquetes no mueva la
factura del cliente. El costo sí es el real de cada guía.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Cliente, PerfilUsuario
from apps.envios.models import Guia, Paquete
from apps.mesa import finanzas


def crear_usuario(username, rol, cliente=None):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


def crear_pedido(cliente, folio, **extra):
    from apps.pedidos.models import Pedido

    defaults = {
        "cliente": cliente,
        "origen": "manual",
        "folio": folio,
        "comprador_nombre": "Comprador de Prueba",
        "direccion": {},
        "cp": "01780",
        "es_local": True,
        "valor_declarado": Decimal("500.00"),
        "estado": Pedido.PENDIENTE,
        "corte_vigente_al_ingreso": time(14, 0),
    }
    defaults.update(extra)
    return Pedido.objects.create(**defaults)


def paquete(pedido, numero, peso, carrier="local", precio="100"):
    return Paquete.objects.create(
        pedido=pedido, numero=numero, peso_kg=Decimal(str(peso)),
        carrier=carrier, precio_cotizado=Decimal(precio),
    )


def guia(pedido, carrier, costo, paquete=None):
    return Guia.objects.create(
        pedido=pedido, paquete=paquete, carrier=carrier,
        numero=f"G-{pedido.folio}-{carrier}", costo_preferencial=Decimal(str(costo)),
    )


def asn(cliente, estado="CERRADA", tarimas=0, tarimas_recibidas=0, descarga=None):
    from apps.inventario.models import OrdenEntrada

    return OrdenEntrada.objects.create(
        cliente=cliente, estado=estado, tarimas=tarimas,
        tarimas_recibidas=tarimas_recibidas, ts_descarga_fin=descarga,
    )


class ResumenMesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.ahora = timezone.now()
        cls.inicio = cls.ahora - timedelta(days=1)
        cls.fin = cls.ahora + timedelta(days=1)

    def test_factura_por_pedido_y_bloque_no_por_guia(self):
        # Pedido local de 18.9 kg dividido en 2 paquetes → 2 guías de $100,
        # pero UN solo bloque de envío local ($129).
        local = crear_pedido(self.cliente, "PED-F0001")
        p1 = paquete(local, 1, "12.60")
        p2 = paquete(local, 2, "6.30")
        guia(local, "local", "100", p1)
        guia(local, "local", "100", p2)
        # Pedido nacional de 14.2 kg → 1 bloque nacional ($219).
        nacional = crear_pedido(self.cliente, "PED-F0002", es_local=False, cp="97203")
        p3 = paquete(nacional, 1, "14.20", carrier="estafeta")
        guia(nacional, "estafeta", "213.50", p3)

        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)

        self.assertEqual(r["pedidos"], 2)
        self.assertEqual(r["paquetes"], 3)
        self.assertEqual(r["bloques"], {"local": 1, "metro": 0, "nacional": 1})
        self.assertEqual(r["ingresos"]["envio"], Decimal("348"))          # 129 + 219
        self.assertEqual(r["ingresos"]["almacenaje"], Decimal("18000"))
        self.assertEqual(r["ingresos"]["alistamiento"], Decimal("50"))    # 2 × 25
        self.assertEqual(r["ingresos"]["empaque"], Decimal("130"))        # 2 × 65
        self.assertEqual(r["ingresos"]["total"], Decimal("18528"))
        self.assertEqual(r["costos"]["carrier"], Decimal("413.50"))
        self.assertEqual(r["costos"]["insumos"], Decimal("36"))           # 3 × 12
        self.assertEqual(r["margen_bruto"], Decimal("18078.50"))
        # Desglose por estado, con datos reales de guías:
        cdmx = r["estados"]["Ciudad de México"]
        self.assertEqual(cdmx["ordenes"], 1)
        self.assertEqual(cdmx["guias"], 2)
        self.assertEqual(cdmx["zona"], "local")
        self.assertAlmostEqual(cdmx["peso"], 18.0, places=1)              # 18.9 sin el +5%
        self.assertEqual(cdmx["costo"], Decimal("200"))
        self.assertEqual(cdmx["facturado"], Decimal("129"))
        yuc = r["estados"]["Yucatán"]
        self.assertEqual(yuc["ordenes"], 1)
        self.assertEqual(yuc["zona"], "nacional")
        self.assertEqual(yuc["facturado"], Decimal("219"))

    def test_pedido_pesado_cobra_bloques_multiples(self):
        pesado = crear_pedido(self.cliente, "PED-F0003")
        for i, peso in enumerate(("18.90", "18.90", "1.20"), start=1):
            paquete(pesado, i, peso)
        guia(pesado, "local", "300")
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["bloques"]["local"], 2)                        # 39 kg → 2 bloques de 20
        self.assertEqual(r["ingresos"]["envio"], Decimal("258"))

    def test_zona_metro_por_carrier_puntopost(self):
        gdl = crear_pedido(self.cliente, "PED-F0004", es_local=False, cp="44100")
        p1 = paquete(gdl, 1, "5.98", carrier="puntopost", precio="91")
        p2 = paquete(gdl, 2, "5.98", carrier="puntopost", precio="91")
        guia(gdl, "puntopost", "91", p1)
        guia(gdl, "puntopost", "91", p2)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["bloques"], {"local": 0, "metro": 1, "nacional": 0})
        self.assertEqual(r["ingresos"]["envio"], Decimal("169"))          # 1 bloque metro, no 2 guías
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

    def test_cancelado_con_guia_no_se_factura_pero_su_costo_si(self):
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.cliente, "PED-F0007", es_local=False, cp="97203")
        p1 = paquete(pedido, 1, "10.00", carrier="estafeta")
        guia(pedido, "estafeta", "185.20", p1)
        Pedido.objects.filter(pk=pedido.pk).update(estado="CANCELADO")
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["pedidos"], 0)
        self.assertEqual(r["cancelados"], 1)
        self.assertEqual(r["ingresos"]["envio"], Decimal("0"))
        self.assertEqual(r["costos"]["carrier"], Decimal("185.20"))

    def test_zona_sale_del_cp_no_del_carrier(self):
        # Pedido a Guadalajara despachado vía estafeta: se factura METRO igual.
        # El ruteo interno jamás mueve la factura del cliente.
        gdl = crear_pedido(self.cliente, "PED-F0008", es_local=False, cp="44100")
        p1 = paquete(gdl, 1, "14.20", carrier="estafeta")
        guia(gdl, "estafeta", "213.50", p1)
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["bloques"], {"local": 0, "metro": 1, "nacional": 0})
        self.assertEqual(r["ingresos"]["envio"], Decimal("169"))

    def test_peso_facturable_descuenta_margen_de_empaque(self):
        # 19.5 kg planeados (con +5% de relleno) = 18.57 kg reales -> 1 bloque,
        # aunque el plan lo haya partido en 2 bultos.
        pedido = crear_pedido(self.cliente, "PED-F0009")
        paquete(pedido, 1, "13.20")
        paquete(pedido, 2, "6.30")
        guia(pedido, "local", "200")
        r = finanzas.resumen_mes(self.cliente, self.inicio, self.fin)
        self.assertEqual(r["bloques"]["local"], 1)


class VistaFinanzasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.cliente)
        pedido = crear_pedido(cls.cliente, "PED-F0100")
        p1 = paquete(pedido, 1, "12.00")
        guia(pedido, "local", "100", p1)

    def test_mesa_ve_finanzas_con_numeros(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Finanzas")
        self.assertContains(respuesta, "Cervecería Colima")
        # Tabla estado × volumen alimentada por las guías del mes
        self.assertContains(respuesta, "Estado × volumen")
        self.assertContains(respuesta, "Ciudad de México")

    def test_portal_no_entra(self):
        self.client.login(username="karina", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertNotEqual(respuesta.status_code, 200)

    def test_mes_invalido_cae_al_mes_actual(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"mes": "chorizo"})
        self.assertEqual(respuesta.status_code, 200)

    def test_mes_sin_actividad_no_truena(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"mes": "2020-01"})
        self.assertEqual(respuesta.status_code, 200)


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


class VistaFinanzasRecepcionTests(TestCase):
    """Un mes de SOLO recepción (sin guías) también aparece en la vista."""

    @classmethod
    def setUpTestData(cls):
        cls.mesa = crear_usuario("mesa2", "mesa")
        # Cliente inactivo (sin pedidos ni guías): solo recibió tarimas este mes.
        cls.cliente = Cliente.objects.create(
            nombre="Mayorista Tarimas", slug="mayorista-tarimas", activo=False,
            tarifario={"recepcion_tarima": 190, "minimo_mes": 12000},
        )
        asn(cls.cliente, tarimas_recibidas=16, descarga=timezone.now())

    def test_mes_solo_recepcion_aparece_con_su_facturacion(self):
        self.client.login(username="mesa2", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Mayorista Tarimas")
        self.assertContains(respuesta, "3,040")  # 16 tarimas × $190
        self.assertContains(respuesta, "La recepción se factura por tarima recibida")
        # Facturó 3,040 < mínimo 12,000 → la línea de ajuste sale con su pill.
        self.assertContains(respuesta, "Ajuste a mínimo mensual")
        self.assertContains(respuesta, "8,960")  # 12,000 − 3,040
