"""Rotación A/B/C: cortes por acumulado, ventas propias a 90 días, clase
efectiva (forzada manda) y el arranque desde el reporte de Shopify."""
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.catalogo import services
from apps.catalogo.models import SKU
from apps.core.models import Cliente
from apps.pedidos.models import LineaPedido, Pedido


class ClasesPorVolumenTests(TestCase):
    def test_cortes_80_95(self):
        ventas = {"a": 60, "b": 25, "c": 10, "d": 4, "e": 1, "f": 0}
        # b cruza el 80 % (acumulado previo 60): sigue siendo A; c entra con 85 → B; d con 95 → C.
        self.assertEqual(services.clases_por_volumen(ventas), {"a": "A", "b": "A", "c": "B", "d": "C", "e": "C", "f": "C"})

    def test_sin_ventas_todo_c(self):
        self.assertEqual(services.clases_por_volumen({"a": 0, "b": 0}), {"a": "C", "b": "C"})


class RotacionPorVentasPropiasTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Colima", slug="colima", integracion_envios="envia")
        self.top = SKU.objects.create(cliente=self.cliente, codigo="TOP", descripcion="Top", precio_declarado=Decimal(1))
        self.medio = SKU.objects.create(cliente=self.cliente, codigo="MED", descripcion="Medio", precio_declarado=Decimal(1))
        self.nada = SKU.objects.create(cliente=self.cliente, codigo="NADA", descripcion="Nada", precio_declarado=Decimal(1))
        self.forzado = SKU.objects.create(cliente=self.cliente, codigo="FORZ", descripcion="Forzado", precio_declarado=Decimal(1), rotacion="A")
        pedido = Pedido.objects.create(cliente=self.cliente, comprador_nombre="Ana", cp="44100")
        LineaPedido.objects.create(pedido=pedido, sku=self.top, cantidad=90)
        LineaPedido.objects.create(pedido=pedido, sku=self.medio, cantidad=10)
        cancelado = Pedido.objects.create(cliente=self.cliente, comprador_nombre="Eva", cp="44100", estado=Pedido.CANCELADO)
        LineaPedido.objects.create(pedido=cancelado, sku=self.nada, cantidad=500)
        viejo = Pedido.objects.create(cliente=self.cliente, comprador_nombre="Old", cp="44100")
        Pedido.objects.filter(pk=viejo.pk).update(creado=timezone.now() - timedelta(days=120))
        LineaPedido.objects.create(pedido=viejo, sku=self.nada, cantidad=500)

    def test_ventas_ignoran_cancelados_y_fuera_de_ventana(self):
        self.assertEqual(services.ventas_por_sku(self.cliente), {self.top.pk: 90, self.medio.pk: 10})

    def test_clase_efectiva(self):
        clases = services.clases_rotacion(self.cliente)
        self.assertEqual(clases[self.top.pk], "A")
        self.assertEqual(clases[self.medio.pk], "B")
        self.assertEqual(clases[self.nada.pk], "C")
        self.assertEqual(clases[self.forzado.pk], "A")
        self.assertEqual(services.clase_rotacion(self.medio), "B")


class RotacionDesdeVentasTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Colima", slug="colima", integracion_envios="envia")
        crear = lambda codigo, desc, **k: SKU.objects.create(cliente=self.cliente, codigo=codigo, descripcion=desc, precio_declarado=Decimal(1), **k)
        self.lata = crear("CCPL24L", "24 PACK CERVEZA PIEDRA LISA SESSION IPA LATA 355 ML")
        self.play_m = crear("PCOVERM", "PLAYERA — OVERSIZE COLIMITA", variante="M")
        self.play_g = crear("PCOVERG", "PLAYERA — OVERSIZE COLIMITA", variante="G")
        self.pin = crear("PINX", "Pin Colima Cero")  # el reporte lo trae en mayúsculas
        self.sin = crear("NADIE", "PRODUCTO QUE NO VENDE", rotacion="A")
        self.ruta = Path(tempfile.mkdtemp()) / "ventas.csv"
        self.ruta.write_text(
            '"Product title","Net items sold"\n'
            '"24 PACK CERVEZA PIEDRA LISA SESSION IPA LATA 355 ML",920\n'
            '"PLAYERA — OVERSIZE COLIMITA",44\n'
            '"PIN COLIMA CERO",9\n'
            '"TITULO SIN SKU",3\n'
            ',0\n',
            encoding="utf-8",
        )

    def test_dry_run_reporta_y_no_escribe(self):
        salida = StringIO()
        call_command("rotacion_desde_ventas", cliente="colima", archivo=str(self.ruta), stdout=salida)
        texto = salida.getvalue()
        self.assertIn("4 SKUs empatados", texto)
        self.assertIn("TITULO SIN SKU", texto)
        self.assertIn("1 SKUs sin ventas", texto)
        self.sin.refresh_from_db()
        self.assertEqual(self.sin.rotacion, "A")

    def test_aplicar_asigna_variantes_y_sin_ventas(self):
        call_command("rotacion_desde_ventas", cliente="colima", archivo=str(self.ruta), aplicar=True, stdout=StringIO())
        rot = {s.codigo: s.rotacion for s in SKU.objects.filter(cliente=self.cliente)}
        self.assertEqual(rot, {"CCPL24L": "A", "PCOVERM": "B", "PCOVERG": "B", "PINX": "C", "NADIE": "C"})
