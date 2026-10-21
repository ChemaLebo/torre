"""Detener desde el piso con "Regresar a racks" (Chema 2026-10-21): al
confirmar, un modal lista a dónde vuelve cada pieza escaneada o empacada de
ESTA ola; en picking solo se anota (el kardex no se ha movido), con el pick
físico hecho la caja se desarma como en Parar en piso. Las cajas en salida,
despachadas o que ya salieron nunca se tocan desde el piso: eso es de Mesa."""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Incidencia
from apps.inventario.models import Saldo
from apps.pedidos import services
from apps.pedidos.models import Pedido, PickLinea

from .base import PisoTestCase

TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


def suma(sku, estado):
    return sum(Saldo.objects.filter(sku=sku, estado=estado).values_list("cantidad", flat=True))


@override_settings(ENVIA_API_KEY="", TORRE=TORRE_CARRIERS_CLASICOS)
class DetenerRegresaARacksTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=20)

    def _saldos(self):
        return (suma(self.sku, Saldo.UBICADO_VENDIBLE), suma(self.sku, Saldo.RESERVADO), suma(self.sku, Saldo.EN_EMPAQUE))

    def _en_picking_escaneado(self, cantidad=2):
        pedido = self.crear_pedido(cantidad=cantidad)
        services.iniciar_picking(pedido, self.operador)
        linea = pedido.lineas.get()
        services.confirmar_linea_pick(linea, cantidad, self.operador, ubicacion=self.ubic_picking)
        pedido.refresh_from_db()
        return pedido, linea

    def _empacado_con_guia(self, sellos=False):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        if sellos:
            ahora = timezone.now()
            Paquete.objects.filter(pk=caja.pk).update(ts_etiquetas=ahora, ts_cierre=ahora, ts_listo_salida=ahora)
        pedido.refresh_from_db()
        return pedido, Paquete.objects.get(pk=caja.pk), linea

    def test_en_picking_el_modal_lista_las_piezas_y_regresar_solo_anota(self):
        pedido, linea = self._en_picking_escaneado()
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        respuesta = self.client.get(url)
        self.assertContains(respuesta, 'id="modal-detener"')
        self.assertContains(respuesta, "Regresar a racks y detener")
        self.assertContains(respuesta, '2 × <span class="mono">COLIMITA-SIX</span> → <span class="mono">A-01-1</span>')
        self.assertNotContains(respuesta, "{#")  # un comentario de plantilla a varias líneas se imprime (PED-00197)
        self.assertEqual(self._saldos(), (20, 2, 0))
        respuesta = self.client.post(url, {"accion": "detener", "motivo": "No encuentro el producto", "regresar": "1"}, follow=True)
        self.assertRedirects(respuesta, reverse("piso:home"), fetch_redirect_response=False)
        self.assertContains(respuesta, "volvió a sus racks")
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.detenido, pedido.asignado_a, pedido.estado, linea.cantidad_pickeada), (True, None, Pedido.EN_PICKING, 0))
        self.assertEqual(list(linea.picks.values_list("estado", flat=True)), [PickLinea.REGRESADO])
        self.assertEqual(self._saldos(), (20, 2, 0))  # el kardex ni se movió: siguen contadas y apartadas
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="regresado_a_racks")
        self.assertEqual(evento.delta["piezas"], [{"sku": "COLIMITA-SIX", "piezas": 2, "a": [["A-01-1", None, 2]]}])
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_DET, interna=True).exists())
        # Al reanudar vuelve a picking desde cero.
        services.reanudar_pedido(pedido, self.operador)
        pedido.refresh_from_db()
        self.assertEqual((pedido.lineas_completas, pedido.lineas.get().por_pickear), (False, 2))

    def test_detener_sin_regresar_conserva_el_avance_como_siempre(self):
        pedido, linea = self._en_picking_escaneado()
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        self.client.post(url, {"accion": "detener", "motivo": "Otro", "detalle": "espera", "regresar": "0"}, follow=True)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.detenido, linea.cantidad_pickeada), (True, 2))
        self.assertEqual(list(linea.picks.values_list("estado", flat=True)), [PickLinea.PICKEADO])
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="regresado_a_racks").exists())

    def test_en_una_reposicion_solo_regresa_lo_de_esta_ola(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        linea = pedido.lineas.get()
        caja1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja1, linea_pedido=linea, cantidad=1)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"):
            services.marcar_recolectado(pedido, self.operador)
        Guia.objects.filter(paquete=caja1).update(estado=Guia.ENTREGADO, ts_recolectado_carrier=timezone.now())
        pedido.refresh_from_db()
        pedido.transicionar(Pedido.ENTREGADO)
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 1, caja1)], self.operador)
        pedido.refresh_from_db()
        services.iniciar_picking(pedido, self.operador)
        linea.refresh_from_db()
        services.confirmar_linea_pick(linea, 1, self.operador, ubicacion=self.ubic_picking)
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        respuesta = self.client.get(url)
        self.assertContains(respuesta, '1 × <span class="mono">COLIMITA-SIX</span> → <span class="mono">A-01-1</span>')
        self.client.post(url, {"accion": "detener", "motivo": "Producto dañado", "regresar": "1"}, follow=True)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        caja1.refresh_from_db()
        self.assertEqual((pedido.detenido, linea.cantidad_pickeada, linea.cantidad_despachada), (True, 1, 1))
        self.assertEqual((caja1.estado, pedido.paquetes.get(numero=2).estado), (Paquete.DESPACHADO, Paquete.PLANEADO))
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="regresado_a_racks")
        self.assertEqual(evento.delta["piezas"], [{"sku": "COLIMITA-SIX", "piezas": 1, "a": [["A-01-1", None, 1]]}])

    def test_empacado_con_guia_se_desarma_y_vuelve_a_picking(self):
        pedido, caja, linea = self._empacado_con_guia()
        self.assertEqual((pedido.estado, self._saldos()), (Pedido.GUIA_GENERADA, (18, 0, 2)))
        url = reverse("piso:empaque_pedido", args=[pedido.pk])
        respuesta = self.client.get(url)
        self.assertContains(respuesta, "Regresar a racks y detener")
        self.assertContains(respuesta, '2 × <span class="mono">COLIMITA-SIX</span> → <span class="mono">A-01-1</span> · caja 1')
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "detener", "motivo": "No cabe / falta insumo de empaque", "regresar": "1"}, follow=True)
        self.assertContains(respuesta, "volvió a sus racks")
        pedido.refresh_from_db()
        caja.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.detenido, pedido.estado, caja.estado), (True, Pedido.EN_PICKING, Paquete.PLANEADO))
        self.assertIsNone(caja.guia_activa)
        self.assertEqual(self._saldos(), (20, 2, 0))
        self.assertEqual((linea.cantidad_pickeada, linea.cantidad_despachada), (0, 0))
        self.assertEqual(list(linea.picks.values_list("estado", flat=True)), [PickLinea.REGRESADO])
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="regresado_a_racks")
        self.assertEqual(evento.delta["cajas_desarmadas"][0]["regreso"][0]["a"], [["A-01-1", None, 2]])
        self.assertEqual(len(evento.delta["guias_canceladas"]), 1)

    def test_una_caja_en_salida_no_se_toca_desde_el_piso(self):
        pedido, caja, linea = self._empacado_con_guia(sellos=True)
        guia = caja.guia_activa
        url = reverse("piso:empaque_pedido", args=[pedido.pk])
        # El wizard ya la da por "en salida": el Detener vive en Revisar caja.
        respuesta = self.client.get(url + "?caja=1")
        self.assertContains(respuesta, 'id="modal-detener"')
        self.assertNotContains(respuesta, "Regresar a racks y detener")
        self.assertContains(respuesta, "solo se decide desde Mesa")
        respuesta = self.client.post(url, {"accion": "detener", "motivo": "Otro", "detalle": "x", "regresar": "1"}, follow=True)
        self.assertContains(respuesta, "solo se decide desde Mesa")
        pedido.refresh_from_db()
        caja.refresh_from_db()
        self.assertEqual((pedido.detenido, caja.estado, caja.guia_activa.pk, caja.ts_listo_salida is not None), (False, Paquete.EMPACADO, guia.pk, True))
        self.assertEqual(self._saldos(), (18, 0, 2))
