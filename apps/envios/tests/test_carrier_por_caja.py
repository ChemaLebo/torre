"""Paquetería forzada POR CAJA (Chema 2026-09-30): Paquete.carrier_forzado
manda sobre la del pedido al cotizar y comprar la guía de esa caja; "local" =
sale sin guía de carrier aunque no haya flota propia ni el pedido lo tenga."""
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.envios.models import Paquete
from apps.envios.services import _carrier_de_paquete, carriers_de_paquete, carriers_del_pedido
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda


@override_settings(ENVIA_API_KEY="")
class CarrierPorCajaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente, crear_tienda(self.cliente))
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta")

    def test_sin_forzado_la_caja_sigue_al_pedido(self):
        self.assertEqual(carriers_de_paquete(self.pedido, self.caja), carriers_del_pedido(self.pedido))
        self.assertEqual(_carrier_de_paquete(self.pedido, self.caja), ("estafeta", ""))

    def test_local_forzado_en_la_caja_sale_sin_guia_de_carrier(self):
        self.caja.carrier_forzado = "local"
        self.caja.save(update_fields=["carrier_forzado"])
        self.assertEqual(carriers_de_paquete(self.pedido, self.caja), ["local"])
        self.assertEqual(_carrier_de_paquete(self.pedido, self.caja), ("local", "entrega_local"))
        self.assertEqual(self.pedido.carrier_forzado, "")  # el pedido no se toca

    def test_carrier_forzado_distinto_al_del_plan_recotiza_la_caja(self):
        self.caja.carrier_forzado = "fedex"
        self.caja.save(update_fields=["carrier_forzado"])
        carrier, _servicio = _carrier_de_paquete(self.pedido, self.caja)
        self.caja.refresh_from_db()
        self.assertEqual((carrier, self.caja.carrier), ("fedex", "fedex"))


@override_settings(ENVIA_API_KEY="")
class GuiasSoloEnBodegaTests(TestCase):
    """PED-00031 (Chema 2026-10-05): al comprar las guías de la ola siguiente,
    una caja ya despachada cuya guía se canceló después (el carrier la perdió)
    NO recibe guía nueva; solo las cajas en bodega."""

    def setUp(self):
        from apps.envios.adapters import MockAdapter

        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente, crear_tienda(self.cliente), estado="EMPACADO")
        self.fuera = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.DESPACHADO)
        self.en_bodega = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)

    def test_no_compra_guia_para_la_caja_despachada_sin_guia_viva(self):
        from apps.envios.models import Guia
        from apps.envios.services import generar_guias

        Guia.objects.create(pedido=self.pedido, paquete=self.fuera, carrier="imile", numero="IM-1", estado=Guia.CANCELADA)
        guias = generar_guias(self.pedido)
        self.assertEqual([g.paquete_id for g in guias], [self.en_bodega.pk])
        self.assertIsNone(self.fuera.guia_activa)
        self.assertEqual(Guia.objects.filter(pedido=self.pedido).count(), 2)  # la cancelada y la nueva de la caja 2
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, "GUIA_GENERADA")
        # Reintentar no duplica nada.
        self.assertEqual([g.pk for g in generar_guias(self.pedido)], [g.pk for g in guias])
