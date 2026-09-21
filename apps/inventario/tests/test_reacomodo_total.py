"""Reacomodo total (inventario.replanear_bodega + comando replanear_acomodo):
planea de cero lo vendible de un cliente como si la bodega estuviera vacía de
sus productos, separando lotes; lo de otros clientes y los anaqueles llenos a
mano siguen contando; con aplicar mueve el inventario del sistema."""
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import Saldo
from apps.inventario.services import replanear_bodega, reservar


class ReacomodoTotalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Colima", slug="colima", integracion_envios="envia")
        cls.otro = Cliente.objects.create(nombre="Infinitea", slug="infinitea", integracion_envios="envia")
        cls.anaqueles = {}
        for prioridad in range(1, 5):
            cls.anaqueles[prioridad] = Ubicacion.objects.create(
                codigo=f"PIC-P{prioridad}", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=prioridad,
            )
        cls.lleno = Ubicacion.objects.create(
            codigo="PIC-4-D-F-1", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=9, lleno_manual=True,
        )
        cls.lata = SKU.objects.create(cliente=cls.cliente, codigo="LATA", descripcion="24 latas", codigo_barras="750", largo_cm=36, ancho_cm=24, alto_cm=17, precio_declarado=Decimal(1), rotacion="A")
        cls.te = SKU.objects.create(cliente=cls.otro, codigo="TE", descripcion="té", largo_cm=36, ancho_cm=24, alto_cm=17, precio_declarado=Decimal(1), rotacion="A")
        cls.lote_a = Lote.objects.create(sku=cls.lata, codigo="LA")
        cls.lote_b = Lote.objects.create(sku=cls.lata, codigo="LB")

    def setUp(self):
        # Los dos lotes mezclados en P3 (el error de ASN-0004) y un resto en P4; Infinitea ocupa P1 a la mitad.
        Saldo.objects.create(sku=self.lata, ubicacion=self.anaqueles[3], lote=self.lote_a, estado=Saldo.UBICADO_VENDIBLE, cantidad=10)
        Saldo.objects.create(sku=self.lata, ubicacion=self.anaqueles[3], lote=self.lote_b, estado=Saldo.UBICADO_VENDIBLE, cantidad=8)
        Saldo.objects.create(sku=self.lata, ubicacion=self.anaqueles[4], lote=self.lote_a, estado=Saldo.UBICADO_VENDIBLE, cantidad=5)
        Saldo.objects.create(sku=self.te, ubicacion=self.anaqueles[1], estado=Saldo.UBICADO_VENDIBLE, cantidad=15)

    def test_planea_de_cero_separando_lotes_y_respetando_otros_clientes(self):
        r = replanear_bodega(self.cliente)
        por_lote = {}
        for p in r["pasos"]:
            por_lote.setdefault(p["lote"], []).append(p)
        self.assertEqual(sum(p["cantidad"] for p in por_lote["LA"]), 15)
        self.assertEqual(sum(p["cantidad"] for p in por_lote["LB"]), 8)
        racks_a = {p["ubicacion"] for p in por_lote["LA"]}
        racks_b = {p["ubicacion"] for p in por_lote["LB"]}
        self.assertTrue(racks_a.isdisjoint(racks_b))  # lotes separados
        self.assertNotIn("PIC-4-D-F-1", racks_a | racks_b)  # el rack lleno a mano no entra
        self.assertNotIn("PIC-P1", racks_a | racks_b)  # P1 la tiene Infinitea: no está vacía para el plan
        self.assertEqual(por_lote["LA"][0]["desde"], [("PIC-P3", 10), ("PIC-P4", 5)])
        self.assertEqual(r["sin_espacio"], 0)
        self.assertEqual(r["movidas"], 0)  # sin aplicar no se mueve nada
        self.assertEqual(Saldo.objects.get(sku=self.lata, lote=self.lote_b).ubicacion.codigo, "PIC-P3")

    def test_aplicar_mueve_lo_vendible_y_sus_reservas_sin_tocar_a_otros(self):
        self.assertTrue(reservar(self.lata, 3, "PED-1"))  # capa de reserva FEFO sobre lote A
        r = replanear_bodega(self.cliente, aplicar=True)
        self.assertEqual(r["movidas"], 23)
        for p in r["pasos"]:
            saldo = Saldo.objects.get(sku=self.lata, lote__codigo=p["lote"], ubicacion__codigo=p["ubicacion"], estado=Saldo.UBICADO_VENDIBLE)
            self.assertEqual(saldo.cantidad, p["cantidad"])
        # El lote A salió de P3 y P4 (ya no comparte anaquel con el B); solo quedan las filas del plan.
        self.assertFalse(Saldo.objects.filter(sku=self.lata, lote=self.lote_a, ubicacion__in=[self.anaqueles[3], self.anaqueles[4]]).exists())
        self.assertEqual(Saldo.objects.filter(sku=self.lata, estado=Saldo.UBICADO_VENDIBLE).count(), len(r["pasos"]))
        capa = Saldo.objects.get(sku=self.lata, estado=Saldo.RESERVADO)
        primero_a = next(p["ubicacion"] for p in r["pasos"] if p["lote"] == "LA")
        self.assertEqual((capa.cantidad, capa.ubicacion.codigo), (3, primero_a))
        te = Saldo.objects.get(sku=self.te)
        self.assertEqual((te.ubicacion.codigo, te.cantidad), ("PIC-P1", 15))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="cliente", entidad_id="colima", accion="reacomodo_total").exists())

    def test_comando_escribe_el_csv_y_solo_aplica_con_la_bandera(self):
        ruta = Path(tempfile.mkdtemp()) / "plan.csv"
        salida = StringIO()
        call_command("replanear_acomodo", "--cliente", "colima", "--csv", str(ruta), stdout=salida)
        self.assertIn("Simulación", salida.getvalue())
        contenido = ruta.read_text(encoding="utf-8-sig")
        self.assertEqual(contenido.splitlines()[0], "sku,nombre,codigo_barras,lote,rack,cantidad,desde")
        self.assertIn("LATA,24 latas,750,LA,", contenido)
        self.assertIn("PIC-P3: 10 · PIC-P4: 5", contenido)
        self.assertEqual(Saldo.objects.filter(sku=self.lata, ubicacion=self.anaqueles[3]).count(), 2)  # sin aplicar: igual
        call_command("replanear_acomodo", "--cliente", "colima", "--csv", str(ruta), "--aplicar", stdout=StringIO())
        self.assertFalse(Saldo.objects.filter(sku=self.lata, lote=self.lote_a, ubicacion=self.anaqueles[3]).exists())

    def test_mesa_descarga_el_plan_sin_mover_nada(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from apps.core.models import PerfilUsuario
        mesa = get_user_model().objects.create_user("mesa-reacomodo", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        respuesta = self.client.get(reverse("mesa:inventario_reacomodo_csv"), {"cliente": "colima"})
        self.assertEqual(respuesta["Content-Type"], "text/csv; charset=utf-8")
        cuerpo = respuesta.content.decode("utf-8-sig")
        self.assertEqual(cuerpo.splitlines()[0], "sku,nombre,codigo_barras,lote,rack,cantidad,desde")
        self.assertIn("LATA,24 latas,750,LB,", cuerpo)
        self.assertEqual(Saldo.objects.filter(sku=self.lata, ubicacion=self.anaqueles[3]).count(), 2)  # solo planea
