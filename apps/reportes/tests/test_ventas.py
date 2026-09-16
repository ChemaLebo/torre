"""Ventas por línea: el precio real de Shopify entra al ingerir, el backfill lo
recupera de los webhooks guardados y el reporte suma solo lo que tiene precio."""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch

from django.apps import apps as registro_apps
from django.utils import timezone

from apps.integraciones.models import WebhookEvento
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido
from apps.reportes import ventas
from apps.reportes.base import limites

from .base import ReportesTestCase


def payload(order_id, items):
    return {
        "id": order_id, "name": f"#{order_id}", "financial_status": "paid", "total_price": "0",
        "email": "ana@example.com",
        "customer": {"first_name": "Ana", "last_name": "Prueba"},
        "shipping_address": {"name": "Ana Prueba", "phone": "+523121234567", "zip": "44100",
                             "address1": "Calle 1", "city": "Guadalajara", "province": "Jalisco", "country": "México"},
        "line_items": [{"id": i, "sku": sku, "quantity": cantidad, "price": precio, "title": sku}
                       for i, (sku, cantidad, precio) in enumerate(items, start=1)],
    }


class VentasTests(ReportesTestCase):
    def _ingerir(self, datos):
        with patch("apps.inventario.services.reservar", return_value=True), \
             patch("apps.mensajeria.services.enviar_confirmacion"), \
             patch("apps.incidencias.services.abrir_incidencia"), \
             self.captureOnCommitCallbacks(execute=True):
            return services.ingerir_pedido_shopify(self.tienda, datos)

    def test_la_ingesta_guarda_el_precio_real_de_la_linea(self):
        pedido = self._ingerir(payload(9001, [("COLIMITA-SIX", 2, "189.50"), ("PARAMO-C12", 1, "")]))
        precios = {l.sku.codigo: l.precio_unitario for l in pedido.lineas.all()}
        self.assertEqual(precios, {"COLIMITA-SIX": Decimal("189.50"), "PARAMO-C12": None})

    def test_backfill_desde_el_webhook_guardado(self):
        pedido = Pedido.objects.create(cliente=self.colima, tienda=self.tienda, shopify_order_id="9002", comprador_nombre="Ana", cp="44100")
        con = LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=3)
        sin = LineaPedido.objects.create(pedido=pedido, sku=self.caja, cantidad=1)
        manual = Pedido.objects.create(cliente=self.colima, origen="manual", comprador_nombre="Luis", cp="44100")
        linea_manual = LineaPedido.objects.create(pedido=manual, sku=self.six, cantidad=1)
        WebhookEvento.objects.create(
            tienda=self.tienda, webhook_id="w-9002", topic="orders/create",
            payload=payload(9002, [("COLIMITA-SIX", 3, "199.00"), ("PARAMO-C12", 1, "x")]),
        )
        rellenar = import_module("apps.pedidos.migrations.0010_linea_precio_unitario").rellenar_precios
        rellenar(registro_apps, None)
        for l in (con, sin, linea_manual):
            l.refresh_from_db()
        self.assertEqual(con.precio_unitario, Decimal("199.00"))
        self.assertIsNone(sin.precio_unitario)  # precio ilegible → null
        self.assertIsNone(linea_manual.precio_unitario)

    def test_reporte_suma_solo_lineas_con_precio_y_no_cancelados(self):
        hoy = timezone.localdate()
        inicio, fin = limites(hoy - timedelta(days=1), hoy)
        pedido = Pedido.objects.create(cliente=self.colima, tienda=self.tienda, shopify_order_id="9003", comprador_nombre="Ana", cp="44100", canal=Pedido.CANAL_TIKTOK)
        LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=2, precio_unitario=Decimal("189.50"))
        LineaPedido.objects.create(pedido=pedido, sku=self.caja, cantidad=1)
        kit_hija = LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=1)
        LineaPedido.objects.filter(pk=kit_hija.pk).update(parte_de_kit=pedido.lineas.first())
        cancelado = Pedido.objects.create(cliente=self.colima, comprador_nombre="Eva", cp="44100", estado=Pedido.CANCELADO)
        LineaPedido.objects.create(pedido=cancelado, sku=self.six, cantidad=5, precio_unitario=Decimal(100))
        Pedido.objects.create(cliente=self.otro, comprador_nombre="Ajeno", cp="06600")

        r = ventas.generar(self.colima, inicio, fin, {"sin_cancelados": False}, es_mesa=False)
        self.assertEqual(len(r["filas"]), 4)
        primera = r["filas"][0]
        self.assertEqual((primera[1], primera[2], primera[4], primera[6], primera[7], primera[8], primera[9]),
                         (pedido.folio, "TikTok Shop", "COLIMITA-SIX", 2, Decimal("189.50"), Decimal("379.00"), "Torre"))
        self.assertIn("(componente de kit)", r["filas"][2][5])
        self.assertEqual(dict(r["resumen"]), {"pedidos": 2, "piezas": 3, "MXN vendidos": Decimal("379.00"), "líneas sin precio": 1})
        oculto = ventas.generar(self.colima, inicio, fin, {"sin_cancelados": True}, es_mesa=False)
        self.assertEqual(len(oculto["filas"]), 3)
