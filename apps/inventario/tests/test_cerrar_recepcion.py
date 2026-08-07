"""Cierre de recepción: tarimas en la ASN y confirmación honesta.

`cerrar_recepcion` es la única puerta para cerrar una ASN desde la operación:
valida el put-away, guarda tarimas recibidas, abre incidencia DES si hubo
faltantes/sobrantes y siempre avisa al cliente (mensajería lazy).
"""
from datetime import timedelta

from django.utils import timezone

from apps.core.models import EventoAuditoria
from apps.incidencias.models import Incidencia
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada
from apps.inventario.services import cerrar_recepcion, recibir, ubicar
from apps.mensajeria.models import NotificacionEnviada

from .base import InventarioTestCase


class TarimasModeloTests(InventarioTestCase):
    def test_tarimas_default_cero(self):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        self.assertEqual(orden.tarimas, 0)
        self.assertEqual(orden.tarimas_recibidas, 0)

    def test_tarimas_se_guardan(self):
        orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=4)
        orden.refresh_from_db()
        self.assertEqual(orden.tarimas, 4)


class CerrarRecepcionTests(InventarioTestCase):
    def _orden_recibida(self, anunciada=10, ok=10, danada=0, ubicar_cantidad=None, tarimas=2):
        """ASN por la puerta oficial: anunciar → recibir → ubicar."""
        orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=tarimas)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=anunciada)
        recibir(linea, ok, danada, "piso1")
        a_ubicar = ok if ubicar_cantidad is None else ubicar_cantidad
        if a_ubicar:
            ubicar(self.sku, a_ubicar, self.ubic_picking, None, "piso1")
        orden.refresh_from_db()
        return orden

    def test_sin_discrepancia_cierra_sin_incidencia_ni_evento(self):
        orden = self._orden_recibida()
        cerrar_recepcion(orden, "piso1")
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.CERRADA)
        self.assertIsNotNone(orden.ts_vendible)
        self.assertEqual(Incidencia.objects.count(), 0)
        self.assertFalse(
            EventoAuditoria.objects.filter(
                entidad="asn", entidad_id=orden.folio, accion="cerrada_con_discrepancia",
            ).exists()
        )

    def test_faltante_abre_incidencia_des_y_registra_evento(self):
        # Llegaron 7 de 10: la línea nunca se completa y la orden se cierra
        # desde EN_RECEPCION con la decisión honesta de "ya no llega más".
        orden = self._orden_recibida(anunciada=10, ok=7)
        self.assertEqual(orden.estado, OrdenEntrada.EN_RECEPCION)
        cerrar_recepcion(orden, "piso1")
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.CERRADA)

        evento = EventoAuditoria.objects.get(
            entidad="asn", entidad_id=orden.folio, accion="cerrada_con_discrepancia",
        )
        desglose = evento.delta["discrepancias"]
        self.assertEqual(len(desglose), 1)
        self.assertEqual(desglose[0]["sku"], "COLIMITA-SIX")
        self.assertEqual(desglose[0]["faltante"], 3)
        self.assertEqual(desglose[0]["sobrante"], 0)

        incidencia = Incidencia.objects.get()
        self.assertEqual(incidencia.tipo, "DES")
        self.assertEqual(incidencia.cliente, self.cliente)
        apertura = incidencia.mensajes.first()
        self.assertIn("COLIMITA-SIX", apertura.texto)
        self.assertIn("anunciadas 10", apertura.texto)
        self.assertIn("faltaron 3", apertura.texto)

    def test_sobrante_abre_una_sola_incidencia(self):
        orden = self._orden_recibida(anunciada=10, ok=12)
        cerrar_recepcion(orden, "piso1")
        self.assertEqual(Incidencia.objects.filter(tipo="DES").count(), 1)
        apertura = Incidencia.objects.get().mensajes.first()
        self.assertIn("sobraron 2", apertura.texto)

    def test_danadas_completas_no_abren_incidencia(self):
        # 8 buenas + 2 dañadas = 10 anunciadas: no hay faltante ni sobrante.
        orden = self._orden_recibida(anunciada=10, ok=8, danada=2)
        cerrar_recepcion(orden, "piso1")
        self.assertEqual(Incidencia.objects.count(), 0)

    def test_tarimas_recibidas_se_guardan(self):
        orden = self._orden_recibida(tarimas=2)
        cerrar_recepcion(orden, "piso1", tarimas_recibidas=3)
        orden.refresh_from_db()
        self.assertEqual(orden.tarimas_recibidas, 3)
        self.assertEqual(orden.tarimas, 2)  # lo anunciado no se pisa

    def test_tarimas_recibidas_negativas_rechazadas(self):
        orden = self._orden_recibida()
        with self.assertRaises(ValueError):
            cerrar_recepcion(orden, "piso1", tarimas_recibidas=-1)
        orden.refresh_from_db()
        self.assertNotEqual(orden.estado, OrdenEntrada.CERRADA)

    def test_putaway_pendiente_bloquea_el_cierre(self):
        orden = self._orden_recibida(ok=10, ubicar_cantidad=6)
        with self.assertRaisesMessage(ValueError, "sin ubicar"):
            cerrar_recepcion(orden, "piso1")
        orden.refresh_from_db()
        self.assertNotEqual(orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(Incidencia.objects.count(), 0)

    def test_anunciada_sin_recibir_no_se_cierra(self):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=5)
        with self.assertRaisesMessage(ValueError, "no ha recibido nada"):
            cerrar_recepcion(orden, "piso1")

    def test_cierre_avisa_al_cliente(self):
        orden = self._orden_recibida()
        cerrar_recepcion(orden, "piso1")
        notificacion = NotificacionEnviada.objects.get(plantilla_clave="REC")
        self.assertEqual(notificacion.referencia, orden.folio)
        self.assertIn(orden.folio, notificacion.cuerpo)

    def test_cierre_con_faltantes_estampa_descarga_fin_del_ultimo_movimiento(self):
        # La descarga real terminó horas antes del cierre: ts_descarga_fin debe
        # ser el ts del último movimiento de recepción, no el momento del
        # cierre (si no, el SLA de recepción quedaría siempre en ~0).
        orden = self._orden_recibida(anunciada=10, ok=7)
        self.assertIsNone(orden.ts_descarga_fin)
        hace_horas = timezone.now() - timedelta(hours=6)
        Movimiento.objects.filter(
            tipo=Movimiento.RECEPCION, referencia=orden.folio,
        ).update(ts=hace_horas)

        cerrar_recepcion(orden, "piso1")
        orden.refresh_from_db()
        self.assertEqual(orden.ts_descarga_fin, hace_horas)
        # SLA transcurrido real (~6 h), ya no descarga_fin == vendible.
        self.assertGreaterEqual(orden.ts_vendible - orden.ts_descarga_fin, timedelta(hours=6))

    def test_recerrar_una_cerrada_no_muta_tarimas(self):
        orden = self._orden_recibida(tarimas=2)
        cerrar_recepcion(orden, "piso1", tarimas_recibidas=16)
        with self.assertRaisesMessage(ValueError, "ya está cerrada"):
            cerrar_recepcion(orden, "piso1", tarimas_recibidas=2)
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(orden.tarimas_recibidas, 16)  # el dato de facturación no se pisa

    def test_recerrar_no_duplica_incidencia_ni_evento(self):
        orden = self._orden_recibida(anunciada=10, ok=7)
        cerrar_recepcion(orden, "piso1")
        with self.assertRaisesMessage(ValueError, "ya está cerrada"):
            cerrar_recepcion(orden, "piso1")
        self.assertEqual(Incidencia.objects.filter(tipo="DES").count(), 1)
        self.assertEqual(
            EventoAuditoria.objects.filter(
                entidad="asn", entidad_id=orden.folio, accion="cerrada_con_discrepancia",
            ).count(),
            1,
        )
