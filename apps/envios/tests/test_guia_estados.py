"""Máquina de estados de Guia: transiciones válidas, inválidas y auditoría."""
from django.test import TestCase, override_settings

from apps.core.models import EventoAuditoria
from apps.envios import services
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia

from .base import crear_cliente, crear_pedido, crear_tienda


@override_settings(ENVIA_API_KEY="")
class GuiaEstadosTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        cliente = crear_cliente()
        tienda = crear_tienda(cliente)
        self.pedido = crear_pedido(cliente, tienda)
        self.guia = services.generar_guia(self.pedido)

    def test_ruta_feliz_completa(self):
        for estado in [Guia.RECOLECTADO, Guia.EN_TRANSITO, Guia.EN_RUTA, Guia.ENTREGADO]:
            self.guia.transicionar(estado)
        self.assertEqual(self.guia.estado, Guia.ENTREGADO)

    def test_carrier_puede_saltarse_estados(self):
        # PQX a veces reporta directo en tránsito sin escaneo de recolección.
        self.guia.transicionar(Guia.EN_TRANSITO)
        self.assertEqual(self.guia.estado, Guia.EN_TRANSITO)

    def test_intento_fallido_puede_reintentar_y_entregar(self):
        self.guia.transicionar(Guia.EN_RUTA)
        self.guia.transicionar(Guia.INTENTO_FALLIDO)
        self.guia.transicionar(Guia.EN_RUTA)
        self.guia.transicionar(Guia.ENTREGADO)
        self.assertEqual(self.guia.estado, Guia.ENTREGADO)

    def test_terminales_no_admiten_salida(self):
        self.guia.transicionar(Guia.ENTREGADO)
        with self.assertRaises(ValueError):
            self.guia.transicionar(Guia.EN_TRANSITO)

    def test_transicion_registra_evento_de_auditoria(self):
        self.guia.transicionar(Guia.RECOLECTADO, motivo="Escaneo del carrier")
        evento = EventoAuditoria.objects.filter(
            entidad="guia", entidad_id=str(self.guia.pk), accion="guia_recolectado"
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.delta["de"], Guia.GUIA_CREADA)
        self.assertEqual(evento.delta["a"], Guia.RECOLECTADO)

    def test_transicion_estampa_ts_ultimo_movimiento(self):
        self.guia.transicionar(Guia.RECOLECTADO)
        self.assertIsNotNone(self.guia.ts_ultimo_movimiento)
