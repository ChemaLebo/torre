"""Un caso por tipo y pedido (Chema 2026-09-28): dos reportes del mismo tipo
sobre el mismo pedido viven en la misma incidencia. Tipos distintos, pedidos
distintos, casos cerrados y las internas siguen abriendo folio propio."""
from unittest.mock import patch

from django.test import TestCase

from apps.core.models import EventoAuditoria
from apps.incidencias.models import Incidencia, MensajeIncidencia
from apps.incidencias.services import abrir_incidencia, cerrar, resolver

from .utils import crear_cliente, crear_pedido


class AgrupacionTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente)
        self.primera = abrir_incidencia(
            self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=self.pedido,
            texto="Intento fallido en la guía 1.",
        )

    def test_mismo_tipo_y_pedido_se_suma_al_caso_abierto(self):
        with patch("apps.mensajeria.services.notificar_cliente_incidencia") as avisar:
            segunda = abrir_incidencia(
                self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido,
                texto="El comprador dice que no le llegó.",
            )
        self.assertEqual(segunda.pk, self.primera.pk)
        self.assertTrue(segunda.agrupada)
        self.assertFalse(self.primera.agrupada)
        self.assertEqual(Incidencia.objects.filter(pedido=self.pedido).count(), 1)
        avisar.assert_not_called()  # el cliente ya conoce el caso
        mensajes = list(self.primera.mensajes.order_by("pk"))
        self.assertEqual(len(mensajes), 2)
        self.assertEqual(mensajes[1].texto, "El comprador dice que no le llegó.")
        self.assertEqual(mensajes[1].rol_autor, MensajeIncidencia.ROL_CLIENTE)  # quién lo reportó, no quién abrió
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="incidencia", entidad_id=self.primera.folio, accion="reporte_agrupado",
        ).exists())

    def test_texto_identico_al_ultimo_no_se_repite(self):
        """El poller insiste cada media hora con el mismo aviso."""
        abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=self.pedido,
                         texto="Intento fallido en la guía 1.")
        self.assertEqual(self.primera.mensajes.count(), 1)

    def test_la_prioridad_sube_pero_no_baja(self):
        retraso = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_AUTO, pedido=self.pedido, texto="a")
        self.assertEqual(retraso.prioridad, Incidencia.P2)
        abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido,
                         texto="b", prioridad=Incidencia.P1)
        retraso.refresh_from_db()
        self.assertEqual(retraso.prioridad, Incidencia.P1)
        abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido,
                         texto="c", prioridad=Incidencia.P3)
        retraso.refresh_from_db()
        self.assertEqual(retraso.prioridad, Incidencia.P1)

    def test_caso_resuelto_se_reabre_y_el_cerrado_abre_folio_nuevo(self):
        resolver(self.primera, "Se entregó en el segundo intento.", actor=None)
        self.primera.refresh_from_db()
        self.assertEqual(self.primera.estado, Incidencia.RESUELTA)
        misma = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_COMPRADOR, pedido=self.pedido,
                                 texto="Sigo sin recibirlo.")
        self.assertEqual(misma.pk, self.primera.pk)
        self.primera.refresh_from_db()
        self.assertEqual(self.primera.estado, Incidencia.EN_CURSO)
        self.assertIsNone(self.primera.ts_resolucion)
        resolver(self.primera, "Entregado; ahora sí.", actor=None)
        cerrar(self.primera, actor=None)
        self.pedido.refresh_from_db()
        self.assertFalse(self.pedido.incidencia_activa)
        nueva = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_COMPRADOR, pedido=self.pedido,
                                 texto="Otra vez.")
        self.assertNotEqual(nueva.pk, self.primera.pk)
        self.assertFalse(nueva.agrupada)
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.incidencia_activa)

    def test_tipo_distinto_pedido_distinto_o_sin_pedido_abren_folio_propio(self):
        otro_tipo = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="x")
        self.assertNotEqual(otro_tipo.pk, self.primera.pk)
        otro_pedido = crear_pedido(self.cliente, folio="PED-00002", shopify_order_id="1002")
        ajena = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=otro_pedido, texto="x")
        self.assertNotEqual(ajena.pk, self.primera.pk)
        suelta = abrir_incidencia(self.cliente, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO, texto="descuadre")
        suelta_2 = abrir_incidencia(self.cliente, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO, texto="descuadre 2")
        self.assertNotEqual(suelta.pk, suelta_2.pk)

    def test_interna_y_publica_del_mismo_tipo_no_se_mezclan(self):
        interna = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=self.pedido,
                                   texto="de la bodega", interna=True)
        self.assertNotEqual(interna.pk, self.primera.pk)
        self.assertTrue(interna.interna)
        de_nuevo = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=self.pedido,
                                    texto="otra vez la bodega", interna=True)
        self.assertEqual(de_nuevo.pk, interna.pk)
