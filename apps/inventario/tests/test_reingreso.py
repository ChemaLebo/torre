"""reingresar_desde_pedido (desde empaque y desde carrito), cierre de reingresos sin
aviso al cliente, y el comando limpiar_en_empaque."""
from io import StringIO

from django.core.management import call_command

from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import (
    cerrar_recepcion, confirmar_pick, reingresar_desde_pedido, reservar, ubicar,
)
from apps.mensajeria.models import NotificacionEnviada

from .base import InventarioTestCase


class ReingresarDesdePedidoTests(InventarioTestCase):
    def test_desde_empaque_saca_de_en_empaque_y_entra_a_put_away(self):
        self.poner_vendible(10)
        self.assertTrue(reservar(self.sku, 4, "PED-1"))
        confirmar_pick(self.sku, 4, "PED-1")
        reingresar_desde_pedido(self.sku, 4, "PED-1", "piso1", desde_empaque=True)
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 0)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 4)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 6)
        mov = Movimiento.objects.get(tipo=Movimiento.RETORNO)
        self.assertEqual((mov.estado_origen, mov.estado_destino, mov.delta), (Saldo.EN_EMPAQUE, Saldo.EN_PUTAWAY, 4))
        self.assertEqual(Saldo.objects.get(estado=Saldo.EN_PUTAWAY).ubicacion, self.ubic_recepcion)

    def test_desde_carrito_libera_reserva_y_resta_vendible(self):
        self.poner_vendible(10)
        self.assertTrue(reservar(self.sku, 4, "PED-1"))
        reingresar_desde_pedido(self.sku, 1, "PED-1", "piso1", desde_empaque=False)
        self.assertEqual(self.suma(Saldo.RESERVADO), 3)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 9)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 1)

    def test_mas_de_lo_que_hay_en_empaque_truena_sin_tocar_nada(self):
        self.poner_vendible(3)
        with self.assertRaisesMessage(ValueError, "Inconsistencia"):
            reingresar_desde_pedido(self.sku, 2, "PED-1", "piso1", desde_empaque=True)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 0)


class CierreDeReingresoTests(InventarioTestCase):
    def test_reingreso_cerrado_no_avisa_al_cliente(self):
        self.poner_vendible(5)
        self.assertTrue(reservar(self.sku, 2, "PED-1"))
        confirmar_pick(self.sku, 2, "PED-1")
        reingresar_desde_pedido(self.sku, 2, "PED-1", "piso1", desde_empaque=True)
        orden = OrdenEntrada.objects.create(
            cliente=self.cliente, tipo=OrdenEntrada.TIPO_REINGRESO, estado=OrdenEntrada.RECIBIDA,
        )
        LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=2, cantidad_recibida=2)
        ubicar(self.sku, 2, self.ubic_picking, None, "piso1")
        cerrar_recepcion(orden, "piso1")
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(NotificacionEnviada.objects.count(), 0)


class LimpiarEnEmpaqueTests(InventarioTestCase):
    def test_diagnostico_y_aplicar(self):
        Saldo.objects.create(sku=self.sku, ubicacion=self.ubic_picking, estado=Saldo.EN_EMPAQUE, cantidad=7)
        salida = StringIO()
        call_command("limpiar_en_empaque", stdout=salida)
        self.assertIn("excedente=7", salida.getvalue())
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 7)
        salida = StringIO()
        call_command("limpiar_en_empaque", "--aplicar", stdout=salida)
        self.assertIn("retiradas 7", salida.getvalue())
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 0)
        self.assertTrue(Movimiento.objects.filter(referencia="LIMPIEZA-EMPAQUE", delta=-7).exists())
        salida = StringIO()
        call_command("limpiar_en_empaque", stdout=salida)
        self.assertIn("nada que limpiar", salida.getvalue())

    def test_en_empaque_respaldado_por_pedido_empacado_no_se_toca(self):
        from django.utils import timezone

        from apps.pedidos.models import LineaPedido, Pedido

        pedido = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", estado=Pedido.EMPACADO, ts_empacado=timezone.now())
        LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=3, cantidad_pickeada=3)
        Saldo.objects.create(sku=self.sku, ubicacion=self.ubic_picking, estado=Saldo.EN_EMPAQUE, cantidad=3)
        salida = StringIO()
        call_command("limpiar_en_empaque", "--aplicar", stdout=salida)
        self.assertIn("nada que limpiar", salida.getvalue())
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 3)

    def test_parcialmente_despachado_respalda_lo_que_sigue_en_empaque(self):
        from django.utils import timezone

        from apps.pedidos.models import LineaPedido, Pedido

        pedido = Pedido.objects.create(
            cliente=self.cliente, tienda=None, origen="manual",
            estado=Pedido.PARCIALMENTE_DESPACHADO, ts_empacado=timezone.now(),
        )
        LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2)
        Saldo.objects.create(sku=self.sku, ubicacion=self.ubic_picking, estado=Saldo.EN_EMPAQUE, cantidad=2)
        salida = StringIO()
        call_command("limpiar_en_empaque", "--aplicar", stdout=salida)
        self.assertIn("nada que limpiar", salida.getvalue())
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 2)
