"""Reacomodo de racks: mover_ubicacion renombra (mismo inventario, misma ficha)
o fusiona (suma saldos en el destino, origen inactiva); el comando valida el
mapa completo, es dry-run por default y escribe todo o nada."""
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import Movimiento, Saldo
from apps.inventario.services import mover_ubicacion


class MoverUbicacionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        cls.sku = SKU.objects.create(cliente=cls.cliente, codigo="SIX", descripcion="Six", peso_gr=4000, precio_declarado=Decimal(300))
        cls.lote = Lote.objects.create(sku=cls.sku, codigo="LC-01")
        cls.vieja = Ubicacion.objects.create(codigo="PIC-6-3", tipo=Ubicacion.PICKING)
        Saldo.objects.create(sku=cls.sku, ubicacion=cls.vieja, lote=cls.lote, estado=Saldo.UBICADO_VENDIBLE, cantidad=487)
        Saldo.objects.create(sku=cls.sku, ubicacion=cls.vieja, lote=cls.lote, estado=Saldo.RESERVADO, cantidad=9)

    def test_renombra_cuando_el_destino_no_existe(self):
        pk_saldo = Saldo.objects.get(ubicacion=self.vieja, estado=Saldo.UBICADO_VENDIBLE).pk
        r = mover_ubicacion("PIC-6-3", "RES-4-D-F-4", actor="mesa1")
        self.assertEqual(r, {"modo": "renombrada", "piezas": 496, "saldos": 2})
        self.vieja.refresh_from_db()
        self.assertEqual((self.vieja.codigo, self.vieja.tipo, self.vieja.activo), ("RES-4-D-F-4", Ubicacion.RESERVA, True))
        self.assertEqual(Saldo.objects.get(pk=pk_saldo).ubicacion, self.vieja)  # mismo saldo, misma ficha
        self.assertEqual(Movimiento.objects.count(), 0)
        evento = EventoAuditoria.objects.get(entidad="ubicacion", entidad_id="PIC-6-3", accion="ubicacion_movida")
        self.assertEqual(evento.delta["modo"], "renombrada")

    def test_fusiona_cuando_el_destino_existe(self):
        destino = Ubicacion.objects.create(codigo="PIC-4-D-F-3", tipo=Ubicacion.PICKING)
        Saldo.objects.create(sku=self.sku, ubicacion=destino, lote=self.lote, estado=Saldo.UBICADO_VENDIBLE, cantidad=13)
        r = mover_ubicacion("PIC-6-3", "PIC-4-D-F-3")
        self.assertEqual(r["modo"], "fusionada")
        self.vieja.refresh_from_db()
        self.assertFalse(self.vieja.activo)
        self.assertEqual(Saldo.objects.filter(ubicacion=self.vieja).count(), 0)
        saldos = {s.estado: s.cantidad for s in Saldo.objects.filter(ubicacion=destino)}
        self.assertEqual(saldos, {Saldo.UBICADO_VENDIBLE: 500, Saldo.RESERVADO: 9})
        self.assertEqual(Movimiento.objects.count(), 0)

    def test_destino_inactivo_o_mismo_codigo_truena(self):
        Ubicacion.objects.create(codigo="PIC-4-D-F-3", tipo=Ubicacion.PICKING, activo=False)
        with self.assertRaises(ValueError):
            mover_ubicacion("PIC-6-3", "PIC-4-D-F-3")
        with self.assertRaises(ValueError):
            mover_ubicacion("PIC-6-3", "PIC-6-3")


class ComandoMoverUbicacionesTests(TestCase):
    def setUp(self):
        cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        sku = SKU.objects.create(cliente=cliente, codigo="SIX", descripcion="Six", peso_gr=4000, precio_declarado=Decimal(300))
        for codigo, piezas in (("PIC-1-1", 100), ("PIC-5-1", 9)):
            u = Ubicacion.objects.create(codigo=codigo, tipo=Ubicacion.PICKING)
            Saldo.objects.create(sku=sku, ubicacion=u, estado=Saldo.UBICADO_VENDIBLE, cantidad=piezas)

    def test_dry_run_muestra_y_no_escribe(self):
        salida = StringIO()
        call_command("mover_ubicaciones", "PIC-1-1=PIC-3-D-B-1", "PIC-5-1=PIC-4-I-F-1", stdout=salida)
        self.assertIn("PIC-1-1 → PIC-3-D-B-1: 100 pieza(s), renombrar", salida.getvalue())
        self.assertTrue(Ubicacion.objects.filter(codigo="PIC-1-1").exists())

    def test_aplicar_mueve_todo(self):
        call_command("mover_ubicaciones", "PIC-1-1=PIC-3-D-B-1", "PIC-5-1=PIC-4-I-F-1", aplicar=True, stdout=StringIO())
        self.assertEqual(set(Ubicacion.objects.values_list("codigo", flat=True)), {"PIC-3-D-B-1", "PIC-4-I-F-1"})
        self.assertEqual(Saldo.objects.get(ubicacion__codigo="PIC-3-D-B-1").cantidad, 100)

    def test_mapa_con_error_no_escribe_nada(self):
        with self.assertRaises(CommandError):
            call_command("mover_ubicaciones", "PIC-1-1=PIC-3-D-B-1", "NO-EXISTE=PIC-4-I-F-1", aplicar=True, stdout=StringIO())
        self.assertTrue(Ubicacion.objects.filter(codigo="PIC-1-1").exists())
        with self.assertRaises(CommandError):
            call_command("mover_ubicaciones", "PIC-1-1=X", "PIC-5-1=X", aplicar=True, stdout=StringIO())

    def test_archivo_csv(self):
        import tempfile
        from pathlib import Path

        ruta = Path(tempfile.mkdtemp()) / "mapa.csv"
        ruta.write_text("viejo,nuevo\nPIC-1-1,PIC-3-D-B-1\n", encoding="utf-8")
        call_command("mover_ubicaciones", archivo=str(ruta), aplicar=True, stdout=StringIO())
        self.assertTrue(Ubicacion.objects.filter(codigo="PIC-3-D-B-1").exists())
