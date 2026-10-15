"""Regularizar salidas sin registro (Chema 2026-10-14): pedidos en la calle
cuyas piezas siguen "en empaque" porque la salida nunca se registró."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import EventoAuditoria
from apps.envios.models import Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.inventario.models import Movimiento, Saldo
from apps.pedidos.models import LineaPedido, Pedido


class RegularizarSalidasTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        tienda = crear_tienda(self.cliente)
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="CT12CAR", descripcion="Ticús 12")
        self.pos = Ubicacion.objects.create(codigo="C-6", tipo=Ubicacion.PICKING)
        # En tránsito por el rastreo del carrier, sin pasar por Salida: pickeadas 2, despachadas 0, caja EMPACADA.
        self.pedido = crear_pedido(self.cliente, tienda, estado=Pedido.EN_TRANSITO, ts_recolectado=None)
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=self.sku, cantidad=2, reservada=True, cantidad_pickeada=2)
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.linea, cantidad=2)
        Saldo.objects.create(sku=self.sku, ubicacion=self.pos, estado=Saldo.EN_EMPAQUE, cantidad=2)
        # Uno sano (ya despachado) y uno parcial legítimo: no se tocan.
        sano = crear_pedido(self.cliente, tienda, estado=Pedido.ENTREGADO)
        LineaPedido.objects.create(pedido=sano, sku=self.sku, cantidad=1, reservada=True, cantidad_pickeada=1, cantidad_despachada=1)
        parcial = crear_pedido(self.cliente, tienda, estado=Pedido.PARCIALMENTE_DESPACHADO)
        LineaPedido.objects.create(pedido=parcial, sku=self.sku, cantidad=1, reservada=True, cantidad_pickeada=1)

    def test_diagnostico_y_aplicar(self):
        salida = StringIO()
        call_command("regularizar_salidas", stdout=salida)
        texto = salida.getvalue()
        self.assertIn(f"{self.pedido.folio} (En tránsito): CT12CAR pickeadas 2, despachadas 0 · cajas sin despachar: 1 (empacado)", texto)
        self.assertIn("1 pedido(s) por regularizar", texto)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_despachada, 0)  # sin --aplicar nada cambia
        salida = StringIO()
        call_command("regularizar_salidas", "--aplicar", stdout=salida)
        self.assertIn(f"{self.pedido.folio}: regularizado.", salida.getvalue())
        self.linea.refresh_from_db()
        self.caja.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual((self.linea.cantidad_despachada, self.caja.estado, self.pedido.estado), (2, Paquete.DESPACHADO, Pedido.EN_TRANSITO))
        self.assertIsNotNone(self.pedido.ts_recolectado)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_EMPAQUE, cantidad__gt=0).exists())
        self.assertEqual(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.SALIDA).count(), 1)
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(self.pedido.pk), accion="salida_regularizada")
        self.assertEqual(evento.delta, {"lineas": [{"sku": "CT12CAR", "despachadas": 2}], "cajas": [1], "estado": "EN_TRANSITO"})
        salida = StringIO()
        call_command("regularizar_salidas", stdout=salida)
        self.assertIn("Nada que regularizar", salida.getvalue())  # idempotente; el parcial legítimo no aparece
