"""Máquinas de estado: Incidencia, Compensacion y ReclamacionCarrier."""
from decimal import Decimal

from django.test import TestCase

from apps.core.models import EventoAuditoria
from apps.incidencias.models import Compensacion, Incidencia, ReclamacionCarrier
from apps.incidencias.services import abrir_incidencia

from .utils import crear_cliente


class IncidenciaEstadosTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.incidencia = abrir_incidencia(
            self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE
        )

    def test_cadena_canonica_completa(self):
        for estado in (
            Incidencia.EN_CURSO,
            Incidencia.RESOLUCION_PROPUESTA,
            Incidencia.RESUELTA,
            Incidencia.CERRADA,
        ):
            self.incidencia.transicionar(estado, actor="mesa1")
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.estado, Incidencia.CERRADA)
        self.assertIsNotNone(self.incidencia.ts_resolucion)
        self.assertIsNotNone(self.incidencia.ts_cierre)

    def test_transicion_invalida_lanza_valueerror(self):
        with self.assertRaises(ValueError):
            self.incidencia.transicionar(Incidencia.CERRADA)  # ABIERTA → CERRADA prohibido

    def test_cerrada_es_terminal(self):
        self.incidencia.transicionar(Incidencia.RESUELTA, actor="mesa1")
        self.incidencia.transicionar(Incidencia.CERRADA, actor="mesa1")
        with self.assertRaises(ValueError):
            self.incidencia.transicionar(Incidencia.EN_CURSO)

    def test_reabrir_desde_resuelta_limpia_resolucion(self):
        self.incidencia.transicionar(Incidencia.RESUELTA, actor="mesa1")
        self.assertIsNotNone(self.incidencia.ts_resolucion)
        self.incidencia.transicionar(Incidencia.EN_CURSO, actor="mesa1", motivo="Cliente rechazó la propuesta")
        self.incidencia.refresh_from_db()
        self.assertIsNone(self.incidencia.ts_resolucion)
        self.assertEqual(self.incidencia.estado, Incidencia.EN_CURSO)

    def test_transicion_registra_evento_auditoria(self):
        self.incidencia.transicionar(Incidencia.EN_CURSO, actor="mesa1", motivo="La tomó la Mesa")
        evento = EventoAuditoria.objects.get(
            entidad="incidencia",
            entidad_id=self.incidencia.folio,
            accion="transicion_en_curso",
        )
        self.assertEqual(evento.delta, {"de": Incidencia.ABIERTA, "a": Incidencia.EN_CURSO})
        self.assertEqual(evento.cliente, self.cliente)


class CompensacionTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        self.compensacion = Compensacion.objects.create(
            incidencia=incidencia,
            tipo=Compensacion.TIPO_REPOSICION,
            monto=Decimal("350.00"),
        )

    def test_flujo_completo_con_referencia(self):
        self.compensacion.transicionar(Compensacion.APROBADA, actor="karina")
        self.assertEqual(self.compensacion.aprobo, "karina")
        self.compensacion.referencia_pago = "SPEI-123456"
        self.compensacion.save(update_fields=["referencia_pago"])
        self.compensacion.transicionar(Compensacion.PAGADA, actor="mesa1")
        self.compensacion.refresh_from_db()
        self.assertEqual(self.compensacion.estado, Compensacion.PAGADA)
        self.assertIsNotNone(self.compensacion.fecha_pago)

    def test_pagada_sin_referencia_lanza_valueerror(self):
        self.compensacion.transicionar(Compensacion.APROBADA, actor="karina")
        with self.assertRaises(ValueError):
            self.compensacion.transicionar(Compensacion.PAGADA)
        self.compensacion.refresh_from_db()
        self.assertEqual(self.compensacion.estado, Compensacion.APROBADA)  # no se movió

    def test_no_se_paga_sin_aprobar(self):
        self.compensacion.referencia_pago = "SPEI-999"
        self.compensacion.save(update_fields=["referencia_pago"])
        with self.assertRaises(ValueError):
            self.compensacion.transicionar(Compensacion.PAGADA)


class ReclamacionCarrierTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_COMPRADOR)
        self.reclamacion = ReclamacionCarrier.objects.create(
            incidencia=incidencia,
            carrier="paquetexpress",
            monto_reclamado=Decimal("1200.00"),
        )

    def test_flujo_aceptada_hasta_pagada_estampa_fechas(self):
        self.reclamacion.transicionar(ReclamacionCarrier.PRESENTADA, actor="mesa1")
        self.assertIsNotNone(self.reclamacion.fecha_presentacion)
        self.reclamacion.transicionar(ReclamacionCarrier.ACEPTADA, actor="mesa1")
        self.assertIsNotNone(self.reclamacion.fecha_resolucion)
        self.reclamacion.monto_recuperado = Decimal("900.00")
        self.reclamacion.save(update_fields=["monto_recuperado"])
        self.reclamacion.transicionar(ReclamacionCarrier.PAGADA, actor="mesa1")
        self.reclamacion.refresh_from_db()
        self.assertEqual(self.reclamacion.estado, ReclamacionCarrier.PAGADA)
        self.assertIsNotNone(self.reclamacion.fecha_pago)

    def test_rechazada_es_terminal(self):
        self.reclamacion.transicionar(ReclamacionCarrier.PRESENTADA, actor="mesa1")
        self.reclamacion.transicionar(ReclamacionCarrier.RECHAZADA, actor="mesa1")
        self.assertIsNotNone(self.reclamacion.fecha_resolucion)
        with self.assertRaises(ValueError):
            self.reclamacion.transicionar(ReclamacionCarrier.PAGADA)

    def test_no_se_presenta_dos_veces(self):
        self.reclamacion.transicionar(ReclamacionCarrier.PRESENTADA, actor="mesa1")
        with self.assertRaises(ValueError):
            self.reclamacion.transicionar(ReclamacionCarrier.PRESENTADA)
