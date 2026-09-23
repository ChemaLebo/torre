"""Cambio de dirección con guía comprada (Chema 2026-09-23): la guía se
cancela por API y el pedido regresa a empaque para comprar la nueva con la
dirección corregida; con algo ya en la calle no se toca."""
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from apps.core.models import EventoAuditoria, EvidenciaFoto
from apps.core.services import registrar_evento
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.services import cancelar_guia
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class RegresarAEmpaqueTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=20)

    def _pedido_con_guia(self):
        """Pedido empacado en una caja, con guía y foto de cierre: listo en Salida."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        caja = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO,
        )
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        caja.refresh_from_db()
        services.cerrar_caja(caja, self.operador, self.foto("cerrada.jpg"))
        pedido.refresh_from_db()
        caja.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.empaque_completo), (Pedido.GUIA_GENERADA, True))
        return pedido, caja

    def test_cancela_la_guia_y_regresa_a_empaque_para_comprar_otra(self):
        pedido, caja = self._pedido_con_guia()
        vieja = caja.guia_activa
        services.regresar_a_empaque(pedido, self.operador, motivo="Cambio de dirección (INC-x)")
        pedido.refresh_from_db()
        caja.refresh_from_db()
        vieja.refresh_from_db()
        self.assertEqual(vieja.estado, Guia.CANCELADA)
        self.assertFalse(vieja.es_activa)
        self.assertEqual(pedido.estado, Pedido.EMPACADO)
        self.assertIsNone(caja.ts_cierre)
        self.assertFalse(caja.foto_cierre)
        self.assertFalse(pedido.empaque_completo)
        # La caja pesada y con foto de contenido no se deshace: solo se vuelve a cerrar.
        self.assertEqual(caja.estado, Paquete.EMPACADO)
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="guia", entidad_id=str(vieja.pk), accion="cancelada_carrier",
        ).exists())
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), accion="regresado_a_empaque",
        ).exists())
        # "Reintentar guía" compra una nueva: la cancelada ya no cuenta.
        nueva = services.generar_guia(pedido)
        self.assertNotEqual(nueva.pk, vieja.pk)
        self.assertEqual(nueva.estado, Guia.GUIA_CREADA)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)

    def test_con_una_caja_ya_en_la_calle_no_se_toca(self):
        pedido, caja = self._pedido_con_guia()
        caja.estado = Paquete.DESPACHADO
        caja.save(update_fields=["estado"])
        with self.assertRaises(ValueError) as ctx:
            services.regresar_a_empaque(pedido, self.operador)
        self.assertIn("ya salió", str(ctx.exception))
        self.assertEqual(caja.guia_activa.estado, Guia.GUIA_CREADA)

    def test_solo_desde_guia_generada(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        with self.assertRaises(ValueError):
            services.regresar_a_empaque(pedido, self.operador)

    def test_cancelar_guia_solo_desde_creada(self):
        pedido, caja = self._pedido_con_guia()
        guia = caja.guia_activa
        guia.transicionar(Guia.RECOLECTADO)
        with self.assertRaises(ValueError):
            cancelar_guia(guia, self.operador)

    def test_cancelar_guia_audita_si_el_carrier_no_pudo(self):
        """El carrier falla al cancelar: la guía igual queda CANCELADA en Torre
        (ya no cuenta) y el evento dice que hay que cancelarla a mano."""
        pedido, caja = self._pedido_con_guia()
        guia = caja.guia_activa
        with mock.patch.object(MockAdapter, "cancelar", side_effect=RuntimeError("carrier caído")):
            ok = cancelar_guia(guia, self.operador)
        guia.refresh_from_db()
        self.assertFalse(ok)
        self.assertEqual(guia.estado, Guia.CANCELADA)
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="guia", entidad_id=str(guia.pk), accion="cancelacion_carrier_fallida",
        ).exists())

    def test_direccion_cambio_desde_lee_la_ingesta_repetida(self):
        pedido, caja = self._pedido_con_guia()
        desde = caja.guia_activa.creado
        self.assertFalse(services.direccion_cambio_desde(pedido, desde))
        registrar_evento("pedido", pedido.pk, "ingesta_repetida", actor="webhook", cliente=self.cliente,
                         delta={"origen": "webhook", "campos": ["notas"]})
        self.assertFalse(services.direccion_cambio_desde(pedido, desde))
        registrar_evento("pedido", pedido.pk, "ingesta_repetida", actor="webhook", cliente=self.cliente,
                         delta={"origen": "webhook", "campos": ["direccion", "cp", "es_local"]})
        self.assertTrue(services.direccion_cambio_desde(pedido, desde))

    def test_cierre_legacy_deja_de_contar(self):
        """Pedido empacado entero (sin cajas propias) con evidencia de cierre
        ligada al pedido: al regresar, esa foto se archiva como cierre anulado."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        services.generar_guia(pedido)
        self.evidencia_cierre(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        self.assertTrue(pedido.cajas_cerradas_completas)
        services.regresar_a_empaque(pedido, self.operador)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)
        self.assertFalse(pedido.cajas_cerradas_completas)
        self.assertEqual(EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), tipo="cierre_anulado",
        ).count(), 1)
