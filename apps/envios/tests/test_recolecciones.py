"""Recolecciones envia (Lote E): agendar, dedup por carrier+día, gating."""
from datetime import date

from django.test import TestCase, override_settings

from apps.core.models import EventoAuditoria
from apps.envios import services
from apps.envios.adapters import MockAdapter
from apps.envios.models import Recoleccion

from .base import crear_cliente, crear_pedido, crear_tienda


@override_settings(ENVIA_API_KEY="")
class RecoleccionesTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        pedido = crear_pedido(self.cliente, self.tienda)
        self.guia = services.generar_guia(pedido)  # mock, carrier paquetexpress

    def test_agenda_con_folio_costo_guias_y_evento(self):
        rec = services.agendar_recoleccion(
            "paquetexpress", date(2026, 9, 2), 10, 18, [self.guia], actor=None,
        )
        self.assertTrue(rec.folio_carrier.startswith("PU-MOCK-"))
        self.assertIsNotNone(rec.costo)
        self.assertEqual(list(rec.guias.all()), [self.guia])
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="recoleccion", entidad_id=str(rec.pk),
                accion="recoleccion_agendada",
            ).exists()
        )

    def test_dedup_jamas_doble_booking_mismo_dia(self):
        services.agendar_recoleccion(
            "paquetexpress", date(2026, 9, 2), 10, 18, [self.guia], actor=None,
        )
        with self.assertRaises(ValueError) as ctx:
            services.agendar_recoleccion(
                "paquetexpress", date(2026, 9, 2), 11, 15, [self.guia], actor=None,
            )
        self.assertIn("doble booking", str(ctx.exception))
        self.assertEqual(Recoleccion.objects.count(), 1)

    def test_carrier_sin_pickup_programado_truena(self):
        with self.assertRaises(ValueError):
            services.agendar_recoleccion(
                "noventa9Minutos", date(2026, 9, 2), 10, 18, [self.guia], actor=None,
            )

    def test_ventana_volteada_truena(self):
        with self.assertRaises(ValueError):
            services.agendar_recoleccion(
                "paquetexpress", date(2026, 9, 2), 18, 10, [self.guia], actor=None,
            )
