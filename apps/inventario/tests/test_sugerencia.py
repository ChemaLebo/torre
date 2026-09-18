"""Acomodo sugerido: no dispersar, anaqueles vacíos por clase de rotación,
nunca la reserva ni los marcados llenos, y el resto sin lugar."""
from decimal import Decimal

from django.test import TestCase

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import Saldo
from apps.inventario.services import marcar_anaquel, sugerir_anaquel


class SugerirAnaquelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Colima", slug="colima", integracion_envios="envia")
        cls.anaqueles = {}
        for prioridad in range(1, 7):
            cls.anaqueles[prioridad] = Ubicacion.objects.create(
                codigo=f"PIC-P{prioridad}", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=prioridad,
            )
        cls.reserva = Ubicacion.objects.create(codigo="RES-1-I-F-4", tipo=Ubicacion.RESERVA, largo_cm=180, ancho_cm=58, alto_cm=0)
        cls.lata = SKU.objects.create(cliente=cls.cliente, codigo="LATA", descripcion="24 latas", largo_cm=36, ancho_cm=24, alto_cm=17, precio_declarado=Decimal(1), rotacion="A")
        cls.lenta = SKU.objects.create(cliente=cls.cliente, codigo="LENTA", descripcion="lenta", largo_cm=36, ancho_cm=24, alto_cm=17, precio_declarado=Decimal(1), rotacion="C")
        cls.media = SKU.objects.create(cliente=cls.cliente, codigo="MEDIA", descripcion="media", largo_cm=36, ancho_cm=24, alto_cm=17, precio_declarado=Decimal(1), rotacion="B")
        cls.pin = SKU.objects.create(cliente=cls.cliente, codigo="PIN", descripcion="pin", precio_declarado=Decimal(1))

    def test_clase_a_toma_el_mejor_vacio_y_reparte_el_resto(self):
        # Caben 30 por anaquel: 70 piezas → 30 + 30 + 10 en los tres mejores.
        plan = sugerir_anaquel(self.lata, 70)
        self.assertEqual([(p["ubicacion"].codigo, p["cantidad"]) for p in plan], [("PIC-P1", 30), ("PIC-P2", 30), ("PIC-P3", 10)])
        self.assertIn("clase A", plan[0]["motivo"])

    def test_primero_donde_ya_vive_el_sku(self):
        Saldo.objects.create(sku=self.lata, ubicacion=self.anaqueles[4], estado=Saldo.UBICADO_VENDIBLE, cantidad=20)
        plan = sugerir_anaquel(self.lata, 15)
        self.assertEqual((plan[0]["ubicacion"].codigo, plan[0]["cantidad"]), ("PIC-P4", 10))  # caben 10 más
        self.assertIn("ya tiene este SKU", plan[0]["motivo"])
        self.assertEqual((plan[1]["ubicacion"].codigo, plan[1]["cantidad"]), ("PIC-P1", 5))

    def test_clase_c_al_peor_y_b_desde_la_mitad(self):
        self.assertEqual(sugerir_anaquel(self.lenta, 5)[0]["ubicacion"].codigo, "PIC-P6")
        self.assertEqual(sugerir_anaquel(self.media, 5)[0]["ubicacion"].codigo, "PIC-P4")

    def test_nunca_reserva_ni_lleno_manual_y_sin_lugar(self):
        Saldo.objects.create(sku=self.lenta, ubicacion=self.anaqueles[1], estado=Saldo.UBICADO_VENDIBLE, cantidad=1)  # ocupado por otro SKU
        marcar_anaquel(self.anaqueles[2], True, actor="piso1")
        for p in (3, 4, 5):
            marcar_anaquel(self.anaqueles[p], True)
        plan = sugerir_anaquel(self.lata, 40)
        self.assertEqual([(p["ubicacion"].codigo if p["ubicacion"] else None, p["cantidad"]) for p in plan], [("PIC-P6", 30), (None, 10)])
        self.assertEqual(plan[-1]["motivo"], "sin anaquel con espacio")
        self.assertTrue(EventoAuditoria.objects.filter(entidad="ubicacion", entidad_id="PIC-P2", accion="anaquel_lleno").exists())
        self.assertTrue(marcar_anaquel(self.anaqueles[2], False))
        self.assertFalse(marcar_anaquel(self.anaqueles[2], False))

    def test_los_lotes_no_se_mezclan_en_un_anaquel(self):
        from apps.catalogo.models import Lote

        lote_a = Lote.objects.create(sku=self.lata, codigo="LA")
        Saldo.objects.create(sku=self.lata, ubicacion=self.anaqueles[3], lote=lote_a, estado=Saldo.UBICADO_VENDIBLE, cantidad=5)
        # Mismo lote: primero donde ya vive; otro lote: jamás a ese anaquel, va a uno vacío.
        self.assertEqual(sugerir_anaquel(self.lata, 5, lote="LA")[0]["ubicacion"].codigo, "PIC-P3")
        plan_b = sugerir_anaquel(self.lata, 5, lote="LB")
        self.assertEqual(plan_b[0]["ubicacion"].codigo, "PIC-P1")
        self.assertNotIn("PIC-P3", [p["ubicacion"].codigo for p in plan_b])
        # Reservas virtuales con lote (plan en curso): el lote B no cae en el anaquel apartado para A.
        reservas = {"PIC-P1": [(self.lata, 10, "LA")]}
        self.assertEqual(sugerir_anaquel(self.lata, 5, reservas, lote="LB")[0]["ubicacion"].codigo, "PIC-P2")
        # El mismo lote se consolida donde ya está apartado (P1, mejor prioridad) antes que donde vive (P3).
        self.assertEqual(sugerir_anaquel(self.lata, 5, reservas, lote="LA")[0]["ubicacion"].codigo, "PIC-P1")

    def test_sin_medidas_no_hay_plan(self):
        self.assertEqual(sugerir_anaquel(self.pin, 10), [])
        self.assertEqual(sugerir_anaquel(self.lata, 0), [])
