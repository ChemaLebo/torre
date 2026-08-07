"""Relojes SLA: cálculo al abrir, propiedades y detección de vencidas."""
from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from apps.incidencias.models import Incidencia, MensajeIncidencia
from apps.incidencias.services import abiertas_fuera_de_sla, abrir_incidencia, responder

from .utils import crear_cliente


class RelojesAlAbrirTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()

    def test_origen_comprador_30_minutos(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_COMPRADOR)
        esperado = incidencia.ts_apertura + timedelta(
            minutes=settings.TORRE["SLA_PRIMERA_RESPUESTA_COMPRADOR_MIN"]
        )
        self.assertEqual(incidencia.sla_respuesta_limite, esperado)

    def test_origen_cliente_2_horas(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        esperado = incidencia.ts_apertura + timedelta(
            hours=settings.TORRE["SLA_PRIMERA_RESPUESTA_CLIENTE_HORAS"]
        )
        self.assertEqual(incidencia.sla_respuesta_limite, esperado)

    def test_resolucion_48_horas(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_FAL, Incidencia.ORIGEN_AUTO)
        esperado = incidencia.ts_apertura + timedelta(hours=settings.TORRE["SLA_RESOLUCION_HORAS"])
        self.assertEqual(incidencia.sla_resolucion_limite, esperado)


class PropiedadesRelojTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()

    def test_minutos_para_sla_respuesta_positivo_dentro_de_sla(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        minutos = incidencia.minutos_para_sla_respuesta
        self.assertIsNotNone(minutos)
        self.assertGreater(minutos, 0)
        self.assertFalse(incidencia.vencida_respuesta)
        self.assertFalse(incidencia.vencida_resolucion)

    def test_vencida_respuesta_y_minutos_negativos(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        incidencia.sla_respuesta_limite = timezone.now() - timedelta(minutes=10)
        incidencia.save(update_fields=["sla_respuesta_limite"])
        self.assertTrue(incidencia.vencida_respuesta)
        self.assertLess(incidencia.minutos_para_sla_respuesta, 0)

    def test_respondida_apaga_el_reloj_de_respuesta(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        incidencia.sla_respuesta_limite = timezone.now() - timedelta(minutes=10)
        incidencia.save(update_fields=["sla_respuesta_limite"])
        responder(incidencia, "mesa1", MensajeIncidencia.ROL_MESA, "Ya lo estamos viendo.")
        incidencia.refresh_from_db()
        self.assertIsNone(incidencia.minutos_para_sla_respuesta)
        self.assertFalse(incidencia.vencida_respuesta)

    def test_vencida_resolucion(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_COMPRADOR)
        incidencia.sla_resolucion_limite = timezone.now() - timedelta(hours=1)
        incidencia.save(update_fields=["sla_resolucion_limite"])
        self.assertTrue(incidencia.vencida_resolucion)


class AbiertasFueraDeSlaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()

    def _vencer_respuesta(self, incidencia):
        incidencia.sla_respuesta_limite = timezone.now() - timedelta(minutes=30)
        incidencia.save(update_fields=["sla_respuesta_limite"])
        return incidencia

    def _vencer_resolucion(self, incidencia):
        incidencia.sla_resolucion_limite = timezone.now() - timedelta(hours=1)
        incidencia.save(update_fields=["sla_resolucion_limite"])
        return incidencia

    def test_detecta_vencida_en_respuesta(self):
        vencida = self._vencer_respuesta(
            abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        )
        self.assertIn(vencida, abiertas_fuera_de_sla())

    def test_detecta_vencida_en_resolucion_aunque_ya_respondida(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_COMPRADOR)
        responder(incidencia, "mesa1", MensajeIncidencia.ROL_MESA, "Estamos rastreando la guía.")
        incidencia.refresh_from_db()
        self._vencer_resolucion(incidencia)
        self.assertIn(incidencia, abiertas_fuera_de_sla())

    def test_ignora_dentro_de_sla(self):
        al_dia = abrir_incidencia(self.cliente, Incidencia.TIPO_FAL, Incidencia.ORIGEN_CLIENTE)
        self.assertNotIn(al_dia, abiertas_fuera_de_sla())

    def test_ignora_resueltas_y_cerradas(self):
        resuelta = self._vencer_respuesta(
            abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE)
        )
        resuelta.transicionar(Incidencia.RESUELTA, actor="mesa1", motivo="Repuesto enviado")
        cerrada = self._vencer_resolucion(
            abrir_incidencia(self.cliente, Incidencia.TIPO_DIR, Incidencia.ORIGEN_COMPRADOR)
        )
        cerrada.transicionar(Incidencia.RESUELTA, actor="mesa1", motivo="Dirección corregida")
        cerrada.transicionar(Incidencia.CERRADA, actor="mesa1")
        fuera = abiertas_fuera_de_sla()
        self.assertNotIn(resuelta, fuera)
        self.assertNotIn(cerrada, fuera)

    def test_en_curso_vencida_si_aparece(self):
        incidencia = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_COMPRADOR)
        incidencia.transicionar(Incidencia.EN_CURSO, actor="mesa1")
        self._vencer_respuesta(incidencia)
        self.assertIn(incidencia, abiertas_fuera_de_sla())
