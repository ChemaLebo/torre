"""Capacidad y ocupación con producto parado: huella × niveles con apilado
máximo, SKU que no cabe, sin medidas, reserva sin tope y el aviso al ubicar."""
from decimal import Decimal

from django.conf import settings
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente
from apps.inventario.models import Saldo
from apps.inventario.services import (
    aviso_capacidad,
    capacidad_sku_en,
    ocupacion,
    ocupaciones,
)


class CapacidadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Colima", slug="colima", integracion_envios="envia")
        cls.anaquel = Ubicacion.objects.create(codigo="PIC-1-I-F-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=1)
        cls.bajo = Ubicacion.objects.create(codigo="PIC-1-I-F-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=44, prioridad=3)
        cls.reserva = Ubicacion.objects.create(codigo="RES-1-I-F-4", tipo=Ubicacion.RESERVA, largo_cm=180, ancho_cm=58, alto_cm=0)
        cls.sin_medidas = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        cls.lata24 = SKU.objects.create(cliente=cls.cliente, codigo="CCPL24L", descripcion="24 latas", largo_cm=36, ancho_cm=24, alto_cm=17, precio_declarado=Decimal(1))
        cls.bot24 = SKU.objects.create(cliente=cls.cliente, codigo="CC24", descripcion="24 botellas", largo_cm=40, ancho_cm=27, alto_cm=25, precio_declarado=Decimal(1))
        cls.agua = SKU.objects.create(cliente=cls.cliente, codigo="C12BLAM500", descripcion="agua", largo_cm=29, ancho_cm=22, alto_cm=28, precio_declarado=Decimal(1))
        cls.pin = SKU.objects.create(cliente=cls.cliente, codigo="PIN", descripcion="pin", precio_declarado=Decimal(1))

    def test_capacidad_huella_por_niveles_con_giro(self):
        # 24 latas 36×24×17 en 180×58×52: 5×2 (o girado 7×1) = 10 por nivel × 3 niveles.
        self.assertEqual(capacidad_sku_en(self.anaquel, self.lata24), 30)
        # 24 botellas 40×27×25: 4×2=8 (girado 6×1=6) × 2 niveles.
        self.assertEqual(capacidad_sku_en(self.anaquel, self.bot24), 16)
        # Piso 3 (44 cm): las botellas solo caben en 1 nivel; el agua de 28 cm 1 nivel.
        self.assertEqual(capacidad_sku_en(self.bajo, self.bot24), 8)
        self.assertEqual(capacidad_sku_en(self.bajo, self.agua), 8 * 2 * 1)  # girada: 8 a lo largo × 2

    def test_no_cabe_sin_medidas_y_reserva(self):
        alto = SKU.objects.create(cliente=self.cliente, codigo="ALTO", descripcion="alto", largo_cm=10, ancho_cm=10, alto_cm=60, precio_declarado=Decimal(1))
        self.assertEqual(capacidad_sku_en(self.anaquel, alto), 0)
        self.assertIsNone(capacidad_sku_en(self.anaquel, self.pin))
        self.assertIsNone(capacidad_sku_en(self.reserva, self.lata24))
        self.assertIsNone(capacidad_sku_en(self.sin_medidas, self.lata24))

    @override_settings(TORRE={**settings.TORRE, "APILADO_MAX": 2})
    def test_apilado_maximo(self):
        self.assertEqual(capacidad_sku_en(self.anaquel, self.lata24), 20)

    def test_ocupacion_suma_fracciones_por_sku(self):
        Saldo.objects.create(sku=self.lata24, ubicacion=self.anaquel, estado=Saldo.UBICADO_VENDIBLE, cantidad=15)  # 50 %
        Saldo.objects.create(sku=self.bot24, ubicacion=self.anaquel, estado=Saldo.RESERVADO, cantidad=4)          # 25 %
        Saldo.objects.create(sku=self.pin, ubicacion=self.anaquel, estado=Saldo.UBICADO_VENDIBLE, cantidad=9)
        o = ocupacion(self.anaquel)
        self.assertEqual((o["pct"], o["estado"], o["sin_medidas"]), (75, "medio", ["PIN"]))
        lleno = ocupacion(self.anaquel, extra=(self.lata24, 6))  # +20 % → 95
        self.assertEqual((lleno["pct"], lleno["estado"]), (95, "lleno"))
        self.assertEqual(ocupacion(self.reserva)["estado"], "ilimitado")
        self.assertEqual(ocupacion(self.sin_medidas)["estado"], "sin_medidas")
        todas = ocupaciones([self.anaquel, self.bajo])
        self.assertEqual((todas["PIC-1-I-F-2"]["pct"], todas["PIC-1-I-F-3"]["pct"]), (75, 0))

    def test_aviso_al_ubicar(self):
        self.assertEqual(aviso_capacidad(self.anaquel, self.lata24, 10), "")
        self.assertIn("queda al 100 %", aviso_capacidad(self.anaquel, self.lata24, 30))
        alto = SKU.objects.create(cliente=self.cliente, codigo="ALTO", descripcion="alto", largo_cm=10, ancho_cm=10, alto_cm=60, precio_declarado=Decimal(1))
        self.assertIn("no cabe parado", aviso_capacidad(self.anaquel, alto, 1))
        self.assertEqual(aviso_capacidad(self.reserva, self.lata24, 500), "")
        self.assertEqual(aviso_capacidad(self.anaquel, self.pin, 500), "")
