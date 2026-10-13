"""Reembolsos de Shopify en la ingesta (Chema 2026-10-12, #33909 / PED-00255):
cada pieza reembolsada cancela una caja que salió y no se entregó; lo
entregado no cambia; el pedido se da por entregado con lo que sí llegó; sin
CAN de conflicto; una caja reembolsada en tránsito abre la interna DEV."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia
from apps.pedidos import services
from apps.pedidos.linea_tiempo import construir
from apps.pedidos.models import LineaPedido, Pedido


def _payload(lineas, reembolsos):
    """Orden de Shopify como la manda orders/updated tras un refund:
    `lineas` = [(sku, pedidas, current_quantity)], `reembolsos` = [(sku, piezas)]."""
    return {
        "id": 8415221153954, "name": "#33909", "financial_status": "partially_refunded",
        "line_items": [{"id": i, "sku": sku, "quantity": q, "current_quantity": cq, "price": "100.00", "title": sku}
                       for i, (sku, q, cq) in enumerate(lineas, start=1)],
        "refunds": [{"id": 1, "refund_line_items": [
            {"line_item_id": 0, "quantity": piezas, "restock_type": "no_restock", "line_item": {"sku": sku}} for sku, piezas in reembolsos
        ]}],
    }


class ReembolsosTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.cp24 = SKU.objects.create(cliente=self.cliente, codigo="CP24", descripcion="Páramo", peso_gr=14000)
        self.nzp = SKU.objects.create(cliente=self.cliente, codigo="12C12NZP", descripcion="NZ Pils", peso_gr=7000)

    def _pedido_33909(self):
        """4 cajas despachadas: 1 (CP24) entregada, 2 (CP24) regresada, 3 y 4 (2× NZP) entregadas."""
        pedido = crear_pedido(self.cliente, self.tienda, estado=Pedido.EN_TRANSITO, es_local=False, shopify_order_id="8415221153954")
        l1 = LineaPedido.objects.create(pedido=pedido, sku=self.cp24, cantidad=2, reservada=True, cantidad_pickeada=2, cantidad_despachada=2)
        l2 = LineaPedido.objects.create(pedido=pedido, sku=self.nzp, cantidad=4, reservada=True, cantidad_pickeada=4, cantidad_despachada=4)
        cajas = {}
        for numero, linea, piezas, estado in ((1, l1, 1, Guia.ENTREGADO), (2, l1, 1, Guia.RETORNO), (3, l2, 2, Guia.ENTREGADO), (4, l2, 2, Guia.ENTREGADO)):
            caja = Paquete.objects.create(pedido=pedido, numero=numero, peso_kg=Decimal("9"), carrier="imile", estado=Paquete.DESPACHADO)
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=piezas)
            Guia.objects.create(pedido=pedido, paquete=caja, carrier="imile", numero=f"IM-{numero}", proveedor="envia", estado=estado)
            cajas[numero] = caja
        return pedido, cajas

    def _actualizar(self, pedido, payload):
        with patch("apps.mensajeria.services.notificar_cliente_incidencia"), self.captureOnCommitCallbacks(execute=True):
            services._actualizar_pedido_existente(pedido, payload, "webhook", False)
        pedido.refresh_from_db()
        return pedido

    def test_el_refund_cancela_la_caja_regresada_y_entrega_el_pedido_sin_can(self):
        pedido, cajas = self._pedido_33909()
        payload = _payload([("CP24", 2, 0), ("12C12NZP", 4, 4)], [("CP24", 2)])
        self._actualizar(pedido, payload)
        for n in (1, 2, 3, 4):
            cajas[n].refresh_from_db()
        self.assertIsNotNone(cajas[2].cancelada_por_reembolso)
        self.assertTrue(all(cajas[n].cancelada_por_reembolso is None for n in (1, 3, 4)))  # la entregada con daño no cambia
        self.assertEqual(cajas[2].estado, Paquete.DESPACHADO)  # salió: su guía se cobra y su kardex pasó
        self.assertEqual(pedido.estado, Pedido.ENTREGADO)
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="reembolso_aplicado")
        self.assertEqual((evento.delta["reembolsos"], evento.delta["cajas_canceladas"]), ({"CP24": 2}, [2]))
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="edicion_orden").exists())
        self.assertFalse(Incidencia.objects.filter(pedido=pedido).exists())  # ni CAN ni DEV (la caja ya venía de regreso)
        self.assertEqual(pedido.lineas.get(sku=self.cp24).cantidad, 2)  # los line items no se tocan
        # Replay del mismo payload: nada cambia.
        self._actualizar(pedido, payload)
        self.assertEqual(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="reembolso_aplicado").count(), 1)
        # Línea de tiempo: el paso dinámico cuelga del último paso con hora anterior al reembolso; la caja lleva su píldora.
        fila = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=pedido.pk))}[2]
        self.assertEqual([(x["nombre"], x["despues_de"]) for x in fila["extras"]], [("Cancelada por reembolso", "guia")])
        self.assertTrue(fila["caja_cancelada"])
        self.assertEqual([f["extras"] for f in construir(Pedido.objects.filter(pk=pedido.pk)) if f["caja"] != 2], [[], [], []])

    def test_reembolsada_en_transito_abre_la_interna_para_pedir_el_regreso(self):
        pedido, cajas = self._pedido_33909()
        Guia.objects.filter(paquete=cajas[2]).update(estado=Guia.EN_TRANSITO)
        self._actualizar(pedido, _payload([("CP24", 2, 0), ("12C12NZP", 4, 4)], [("CP24", 2)]))
        cajas[2].refresh_from_db()
        self.assertIsNotNone(cajas[2].cancelada_por_reembolso)
        dev = Incidencia.objects.get(pedido=pedido, tipo="DEV")
        self.assertTrue(dev.interna)
        self.assertIn("IM-2", dev.mensajes.first().texto)
        self.assertEqual(pedido.estado, Pedido.ENTREGADO)  # la caja cancelada no cuenta; las demás están entregadas

    def test_reembolso_de_una_pieza_entregada_con_danio_no_cancela_nada_ni_abre_can(self):
        pedido, cajas = self._pedido_33909()
        Guia.objects.filter(paquete=cajas[2]).update(estado=Guia.ENTREGADO)
        self._actualizar(pedido, _payload([("CP24", 2, 1), ("12C12NZP", 4, 4)], [("CP24", 1)]))
        self.assertTrue(all(Paquete.objects.get(pk=c.pk).cancelada_por_reembolso is None for c in cajas.values()))
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion__in=["reembolso_aplicado", "edicion_orden"]).exists())
        self.assertFalse(Incidencia.objects.filter(pedido=pedido).exists())

    def test_una_caja_mixta_solo_se_cancela_si_el_reembolso_cubre_todo_su_contenido(self):
        pedido, cajas = self._pedido_33909()
        PaqueteLinea.objects.create(paquete=cajas[2], linea_pedido=pedido.lineas.get(sku=self.nzp), cantidad=1)  # la caja 2 también lleva 1 NZP
        self._actualizar(pedido, _payload([("CP24", 2, 0), ("12C12NZP", 4, 4)], [("CP24", 2)]))
        cajas[2].refresh_from_db()
        self.assertIsNone(cajas[2].cancelada_por_reembolso)  # el comprador sigue esperando su NZP
        self.assertEqual(pedido.estado, Pedido.EN_TRANSITO)
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="edicion_orden").exists())

    def test_reembolso_con_piezas_en_bodega_sigue_el_camino_de_edicion(self):
        """Lo reembolsado que no ha salido no es "explicado": se reduce o conflictúa como siempre."""
        pedido = crear_pedido(self.cliente, self.tienda, estado=Pedido.PENDIENTE, es_local=False)
        LineaPedido.objects.create(pedido=pedido, sku=self.cp24, cantidad=2)
        with patch("apps.inventario.services.liberar_reserva"):
            self._actualizar(pedido, _payload([("CP24", 2, 1)], [("CP24", 1)]))
        self.assertEqual(pedido.lineas.get().cantidad, 1)
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="reembolso_aplicado").exists())
