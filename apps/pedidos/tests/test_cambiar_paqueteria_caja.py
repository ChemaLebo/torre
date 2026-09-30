"""pedidos.cambiar_paqueteria_caja (Chema 2026-09-30): la paquetería se cambia
POR CAJA antes de salir; si la nueva no cotiza, nada cambia."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.envios.adapters import ErrorCarrier, MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class CambiarPaqueteriaCajaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        self.pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = self.pedido.lineas.get()
        self.c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        self.c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=self.c2, linea_pedido=linea, cantidad=2)

    def test_caja_empacada_sin_guia_solo_cambia_carrier_y_precio(self):
        caja = services.cambiar_paqueteria_caja(self.pedido, self.c1, "local", self.mesa)
        self.pedido.refresh_from_db()
        self.assertEqual((caja.carrier, caja.carrier_forzado, caja.precio_cotizado), ("local", "local", Decimal("100.00")))
        self.assertEqual(self.pedido.estado, Pedido.EMPACADO)  # sin guía que cancelar: mismo estado
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.carrier, "estafeta")

    def test_con_guia_comprada_la_cancela_borra_el_cierre_y_regresa_a_empaque(self):
        services.generar_guia(self.pedido)
        self.pedido.refresh_from_db()
        self.c1.refresh_from_db()
        vieja = self.c1.guia_activa
        Paquete.objects.filter(pk=self.c1.pk).update(ts_cierre=self.pedido.creado)
        Pedido.objects.filter(pk=self.pedido.pk).update(asignado_a=self.operador)
        self.pedido.refresh_from_db()
        self.c1.refresh_from_db()
        caja = services.cambiar_paqueteria_caja(self.pedido, self.c1, "local", self.mesa)
        self.pedido.refresh_from_db()
        vieja.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.pedido.asignado_a), (Pedido.EMPACADO, None))
        self.assertEqual((vieja.estado, caja.ts_cierre, caja.carrier), (Guia.CANCELADA, None, "local"))
        self.assertEqual(self.c2.guia_activa.estado, Guia.GUIA_CREADA)  # la otra caja ni se entera

    def test_sin_carrier_cancela_y_recotiza_con_las_reglas_del_pedido(self):
        services.generar_guia(self.pedido)
        self.pedido.refresh_from_db()
        self.c1.refresh_from_db()
        vieja = self.c1.guia_activa
        caja = services.cambiar_paqueteria_caja(self.pedido, self.c1, None, self.mesa)
        vieja.refresh_from_db()
        self.assertEqual((vieja.estado, caja.carrier_forzado), (Guia.CANCELADA, ""))
        self.assertIsNone(caja.guia_activa)

    def test_si_no_cotiza_nada_cambia(self):
        with patch("apps.envios.services._replan_paquete", side_effect=ErrorCarrier("sin tarifa")):
            with self.assertRaises(ValueError) as ctx:
                services.cambiar_paqueteria_caja(self.pedido, self.c1, "fedex", self.mesa)
        self.assertIn("no cotiza la caja 1", str(ctx.exception))
        self.c1.refresh_from_db()
        self.assertEqual((self.c1.carrier, self.c1.carrier_forzado), ("estafeta", ""))

    def test_validaciones(self):
        with self.assertRaises(ValueError):
            services.cambiar_paqueteria_caja(self.pedido, self.c1, "inexistente", self.mesa)
        Paquete.objects.filter(pk=self.c1.pk).update(estado=Paquete.DESPACHADO)
        self.c1.refresh_from_db()
        with self.assertRaises(ValueError):
            services.cambiar_paqueteria_caja(self.pedido, self.c1, "local", self.mesa)
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.EN_TRANSITO)
        self.pedido.refresh_from_db()
        with self.assertRaises(ValueError):
            services.cambiar_paqueteria_caja(self.pedido, self.c2, "local", self.mesa)
