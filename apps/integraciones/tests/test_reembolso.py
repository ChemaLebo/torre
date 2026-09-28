"""Refund en Shopify (Chema 2026-09-28): line items por SKU y/o envío con el
monto y las transacciones que sugiere Shopify, o un monto libre contra el
pago original; sin restock. Todo GraphQL parchado."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.core.models import EventoAuditoria
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.integraciones.models import SyncLog
from apps.integraciones.services import reembolsar_en_shopify
from apps.integraciones.shopify import ShopifyClient, ShopifyError

ORDEN = "gid://shopify/Order/777"
LINEAS = {"order": {"id": ORDEN, "lineItems": {"nodes": [
    {"id": "gid://shopify/LineItem/1", "sku": "SIX", "quantity": 2, "refundableQuantity": 2},
    {"id": "gid://shopify/LineItem/2", "sku": "C12", "quantity": 1, "refundableQuantity": 0},
]}, "transactions": [
    {"id": "gid://shopify/OrderTransaction/5", "kind": "SALE", "status": "SUCCESS", "gateway": "shopify_payments",
     "parentTransaction": None, "amountSet": {"shopMoney": {"amount": "1200.00", "currencyCode": "MXN"}}},
]}}
SUGERIDO = {"order": {"suggestedRefund": {
    "amountSet": {"shopMoney": {"amount": "589.00", "currencyCode": "MXN"}},
    "suggestedTransactions": [{"amountSet": {"shopMoney": {"amount": "589.00", "currencyCode": "MXN"}},
                               "gateway": "shopify_payments", "kind": "SUGGESTED_REFUND",
                               "parentTransaction": {"id": "gid://shopify/OrderTransaction/5"}}],
}}}
CREADO = {"refundCreate": {"refund": {"id": "gid://shopify/Refund/9", "totalRefundedSet": {"shopMoney": {"amount": "589.00", "currencyCode": "MXN"}}},
                           "userErrors": []}}


class ReembolsoShopifyTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba", location_id="1")
        self.pedido = crear_pedido(self.cliente, self.tienda, shopify_order_id="777")

    def test_por_lineas_y_envio_con_lo_que_sugiere_shopify(self):
        with patch.object(ShopifyClient, "graphql", side_effect=[LINEAS, SUGERIDO, CREADO]) as gql:
            referencia, monto = reembolsar_en_shopify(
                self.pedido, lineas=[("SIX", 2)], reembolsar_envio=True, nota="INC-2026-0001 · Torre", avisar=False,
            )
        self.assertEqual((referencia, monto), ("gid://shopify/Refund/9", Decimal("589.00")))
        self.assertEqual(gql.call_count, 3)
        sugerido = gql.call_args_list[1].args[1]
        self.assertEqual(sugerido, {"id": ORDEN, "lineas": [{"lineItemId": "gid://shopify/LineItem/1", "quantity": 2}], "envio": True})
        entrada = gql.call_args_list[2].args[1]["input"]
        self.assertEqual(entrada["orderId"], ORDEN)
        self.assertEqual(entrada["refundLineItems"], [{"lineItemId": "gid://shopify/LineItem/1", "quantity": 2, "restockType": "NO_RESTOCK"}])
        self.assertEqual(entrada["shipping"], {"fullRefund": True})
        self.assertEqual(entrada["transactions"], [{"orderId": ORDEN, "gateway": "shopify_payments", "kind": "REFUND",
                                                    "amount": "589.00", "parentId": "gid://shopify/OrderTransaction/5"}])
        self.assertEqual((entrada["notify"], entrada["note"]), (False, "INC-2026-0001 · Torre"))
        self.assertTrue(SyncLog.objects.filter(tienda=self.tienda, resultado=SyncLog.RESULTADO_OK, detalle__contains="refund").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk), accion="reembolso_shopify").exists())

    def test_sin_piezas_reembolsables_truena_y_queda_en_synclog(self):
        with patch.object(ShopifyClient, "graphql", side_effect=[LINEAS]):
            with self.assertRaises(ShopifyError):
                reembolsar_en_shopify(self.pedido, lineas=[("C12", 1)])
        self.assertTrue(SyncLog.objects.filter(tienda=self.tienda, resultado=SyncLog.RESULTADO_ERROR, detalle__contains="C12").exists())

    def test_monto_libre_va_contra_el_pago_original(self):
        creado = {"refundCreate": {"refund": {"id": "gid://shopify/Refund/3", "totalRefundedSet": {"shopMoney": {"amount": "150.00"}}}, "userErrors": []}}
        with patch.object(ShopifyClient, "graphql", side_effect=[LINEAS, creado]) as gql:
            referencia, monto = reembolsar_en_shopify(self.pedido, monto=Decimal("150"))
        self.assertEqual((referencia, monto), ("gid://shopify/Refund/3", Decimal("150.00")))
        entrada = gql.call_args_list[1].args[1]["input"]
        self.assertEqual(entrada["refundLineItems"], [])
        self.assertNotIn("shipping", entrada)
        self.assertEqual(entrada["transactions"][0]["amount"], "150.00")
        self.assertEqual(entrada["transactions"][0]["parentId"], "gid://shopify/OrderTransaction/5")

    def test_user_errors_y_sin_token(self):
        rechazo = {"refundCreate": {"refund": None, "userErrors": [{"field": ["transactions"], "message": "Cannot refund more than available"}]}}
        with patch.object(ShopifyClient, "graphql", side_effect=[LINEAS, SUGERIDO, rechazo]):
            with self.assertRaises(ShopifyError) as ctx:
                reembolsar_en_shopify(self.pedido, lineas=[("SIX", 1)])
        self.assertIn("Cannot refund more", str(ctx.exception))
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        with self.assertRaises(ShopifyError):
            reembolsar_en_shopify(self.pedido, lineas=[("SIX", 1)])
