"""Reserva atómica: jamás sobrevender, respetar buffer y FEFO."""
import datetime

from apps.inventario import services
from apps.inventario.models import Movimiento, Saldo

from .base import InventarioTestCase


class ReservaTests(InventarioTestCase):
    def test_reserva_no_sobrevende(self):
        """Dos pedidos por el mismo stock: uno reserva, el otro recibe False."""
        self.poner_vendible(5)

        self.assertTrue(services.reservar(self.sku, 3, "PED-00001"))
        self.assertFalse(services.reservar(self.sku, 3, "PED-00002"))

        self.assertEqual(self.suma(Saldo.RESERVADO), 3)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 5)  # capa: el físico no se mueve
        self.assertEqual(services.disponible(self.sku), 2)

    def test_reserva_exacta_la_ultima_unidad(self):
        self.poner_vendible(2)
        self.assertTrue(services.reservar(self.sku, 2, "PED-00003"))
        self.assertEqual(services.disponible(self.sku), 0)
        self.assertFalse(services.reservar(self.sku, 1, "PED-00004"))

    def test_reserva_respeta_buffer_del_cliente(self):
        """El buffer defensivo del cliente no se vende."""
        self.cliente.buffer_stock = 2
        self.cliente.save()
        self.poner_vendible(5)

        self.assertFalse(services.reservar(self.sku, 4, "PED-00005"))
        self.assertTrue(services.reservar(self.sku, 3, "PED-00006"))
        self.assertEqual(services.disponible(self.sku), 0)

    def test_reserva_fefo_toma_el_lote_que_caduca_primero(self):
        lote_pronto = self.crear_lote("L-AGO", datetime.date(2026, 8, 1))
        lote_tarde = self.crear_lote("L-DIC", datetime.date(2026, 12, 1))
        self.poner_vendible(5, lote=lote_tarde)
        self.poner_vendible(5, lote=lote_pronto)

        self.assertTrue(services.reservar(self.sku, 3, "PED-00007"))

        reserva = Saldo.objects.get(sku=self.sku, estado=Saldo.RESERVADO)
        self.assertEqual(reserva.lote, lote_pronto)
        self.assertEqual(reserva.cantidad, 3)

    def test_reserva_deja_rastro_en_kardex(self):
        self.poner_vendible(5)
        services.reservar(self.sku, 2, "PED-00008")
        mov = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.RESERVA)
        self.assertEqual(mov.delta, 2)
        self.assertEqual(mov.referencia, "PED-00008")
        self.assertEqual(mov.estado_origen, Saldo.UBICADO_VENDIBLE)
        self.assertEqual(mov.estado_destino, Saldo.RESERVADO)

    def test_cantidad_invalida(self):
        self.poner_vendible(5)
        with self.assertRaises(ValueError):
            services.reservar(self.sku, 0, "PED-00009")
        with self.assertRaises(ValueError):
            services.reservar(self.sku, -1, "PED-00010")


class LiberarReservaTests(InventarioTestCase):
    def test_liberar_regresa_el_disponible(self):
        self.poner_vendible(5)
        services.reservar(self.sku, 3, "PED-00011")
        self.assertEqual(services.disponible(self.sku), 2)

        services.liberar_reserva(self.sku, 2, "PED-00011")

        self.assertEqual(services.disponible(self.sku), 4)
        self.assertEqual(self.suma(Saldo.RESERVADO), 1)
        mov = Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RESERVA).first()
        self.assertEqual(mov.delta, -2)  # liberación = delta negativo en la capa

    def test_liberar_mas_de_lo_reservado_truena(self):
        self.poner_vendible(5)
        services.reservar(self.sku, 2, "PED-00012")
        with self.assertRaises(ValueError):
            services.liberar_reserva(self.sku, 3, "PED-00012")
