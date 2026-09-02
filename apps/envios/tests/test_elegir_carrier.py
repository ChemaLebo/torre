"""elegir_carrier: reglas por prioridad, alcance por cliente y defaults.

TORRE["FLOTA_PROPIA"]=False (default): el carrier "local" no es elegible —
los comportamientos de flota propia se fijan con override del flag.
"""
from django.conf import settings
from django.test import TestCase, override_settings

from apps.envios import services
from apps.envios.models import ReglaEnvio

from .base import crear_cliente, crear_pedido, crear_tienda

TORRE_CON_FLOTA = {**settings.TORRE, "FLOTA_PROPIA": True}


class ElegirCarrierTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)

    def _pedido(self, **kwargs):
        return crear_pedido(self.cliente, self.tienda, **kwargs)

    def test_default_foraneo_usa_carrier_preferente_del_cliente(self):
        pedido = self._pedido(es_local=False)
        carrier, servicio = services.elegir_carrier(pedido)
        self.assertEqual(carrier, "paquetexpress")
        self.assertEqual(servicio, services.SERVICIO_DEFAULT)

    @override_settings(TORRE=TORRE_CON_FLOTA)
    def test_default_local_con_flota_es_entrega_local_sin_guia_externa(self):
        pedido = self._pedido(es_local=True, cp="28017")
        self.assertEqual(services.elegir_carrier(pedido), ("local", "entrega_local"))

    def test_sin_flota_el_pedido_local_usa_su_carrier_real(self):
        # TORRE["FLOTA_PROPIA"]=False (default): no hay flota → el es_local
        # viaja con el carrier preferente del cliente, jamás "local".
        pedido = self._pedido(es_local=True, cp="28017")
        self.assertEqual(
            services.elegir_carrier(pedido), ("paquetexpress", services.SERVICIO_DEFAULT),
        )

    def test_sin_flota_la_regla_local_se_salta(self):
        ReglaEnvio.objects.create(
            cliente=None, prioridad=1, condicion={"es_local": True}, carrier="local", servicio="entrega_local"
        )
        ReglaEnvio.objects.create(
            cliente=None, prioridad=5, condicion={"es_local": True}, carrier="estafeta", servicio="ground"
        )
        pedido = self._pedido(es_local=True, cp="28017")
        # La regla de flota propia es carril muerto sin flota: gana la siguiente.
        self.assertEqual(services.elegir_carrier(pedido), ("estafeta", "ground"))

    def test_regla_global_vacia_aplica_a_todo(self):
        ReglaEnvio.objects.create(cliente=None, prioridad=10, condicion={}, carrier="estafeta", servicio="ground")
        pedido = self._pedido()
        self.assertEqual(services.elegir_carrier(pedido), ("estafeta", "ground"))

    def test_prioridad_menor_gana(self):
        ReglaEnvio.objects.create(cliente=None, prioridad=50, condicion={}, carrier="estafeta", servicio="ground")
        ReglaEnvio.objects.create(cliente=None, prioridad=5, condicion={}, carrier="paquetexpress", servicio="ground")
        pedido = self._pedido()
        self.assertEqual(services.elegir_carrier(pedido), ("paquetexpress", "ground"))

    @override_settings(TORRE=TORRE_CON_FLOTA)
    def test_condicion_es_local_solo_aplica_a_pedidos_locales(self):
        ReglaEnvio.objects.create(
            cliente=None, prioridad=1, condicion={"es_local": True}, carrier="local", servicio="entrega_local"
        )
        local = self._pedido(es_local=True, cp="28017")
        foraneo = self._pedido(es_local=False)
        self.assertEqual(services.elegir_carrier(local), ("local", "entrega_local"))
        self.assertEqual(services.elegir_carrier(foraneo), ("paquetexpress", services.SERVICIO_DEFAULT))

    def test_condicion_cp_prefijo(self):
        ReglaEnvio.objects.create(
            cliente=None, prioridad=1, condicion={"cp_prefijo": "28"}, carrier="paquetexpress", servicio="ground"
        )
        colima = self._pedido(cp="28017")
        cdmx = self._pedido(cp="06600")
        self.assertEqual(services.elegir_carrier(colima), ("paquetexpress", "ground"))
        # El CP 06600 no matchea la regla: cae al default del cliente.
        self.assertEqual(services.elegir_carrier(cdmx), ("paquetexpress", services.SERVICIO_DEFAULT))

    def test_regla_del_cliente_gana_a_la_global_en_empate_de_prioridad(self):
        ReglaEnvio.objects.create(cliente=None, prioridad=10, condicion={}, carrier="estafeta", servicio="ground")
        ReglaEnvio.objects.create(
            cliente=self.cliente, prioridad=10, condicion={}, carrier="paquetexpress", servicio="ground"
        )
        pedido = self._pedido()
        self.assertEqual(services.elegir_carrier(pedido), ("paquetexpress", "ground"))

    def test_regla_de_otro_cliente_se_ignora(self):
        otro = crear_cliente()
        ReglaEnvio.objects.create(cliente=otro, prioridad=1, condicion={}, carrier="dhl", servicio="express")
        pedido = self._pedido()
        self.assertEqual(services.elegir_carrier(pedido), ("paquetexpress", services.SERVICIO_DEFAULT))


class IntegracionPorClienteTests(TestCase):
    """El flip de 99minutos es POR CLIENTE: integracion_envios corto-circuita
    el default (tras reglas y flota), jamás a las ReglaEnvio explícitas."""

    def test_cliente_99minutos_va_directo_a_noventa9(self):
        cliente = crear_cliente(integracion_envios="99minutos")
        tienda = crear_tienda(cliente)
        pedido = crear_pedido(cliente, tienda, es_local=False)
        self.assertEqual(
            services.elegir_carrier(pedido),
            ("noventa9Minutos", services.SERVICIO_DEFAULT),
        )

    def test_regla_envio_le_gana_a_la_integracion(self):
        cliente = crear_cliente(integracion_envios="99minutos")
        tienda = crear_tienda(cliente)
        ReglaEnvio.objects.create(
            cliente=cliente, prioridad=1, condicion={}, carrier="fedex", servicio="ground",
        )
        pedido = crear_pedido(cliente, tienda)
        self.assertEqual(services.elegir_carrier(pedido), ("fedex", "ground"))
