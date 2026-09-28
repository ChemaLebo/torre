"""Compensaciones desde Mesa (Chema 2026-09-28): elegir line items, proponer
(el cliente aprueba) o aprobar y ejecutar ya; reintentar un reembolso que
Shopify rechazó; pagar a mano un cupón."""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.core.models import PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Compensacion, Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.integraciones.shopify import ShopifyError
from apps.pedidos.models import LineaPedido, Pedido


class CompensacionesMesaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado=Pedido.ENTREGADO)
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", precio_declarado=Decimal("300"))
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=2)
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="Rota")
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.url = reverse("mesa:incidencia_detalle", args=[self.inc.pk])

    def test_el_expediente_ofrece_los_line_items_y_los_tipos_que_aplican(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'name="linea_{self.linea.pk}"', html)
        self.assertIn('value="reposicion"', html)
        self.assertIn('value="reembolso"', html)
        retraso = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_AUTO, pedido=self.pedido, texto="x")
        html = self.client.get(reverse("mesa:incidencia_detalle", args=[retraso.pk])).content.decode()
        self.assertIn('value="reposicion"', html)  # cualquier tipo con pedido (Chema 2026-09-28)
        self.assertIn('value="reembolso"', html)

    def test_proponer_y_luego_aprobar_ejecuta_la_reposicion(self):
        respuesta = self.client.post(self.url, {
            "accion": "compensacion_crear", "tipo": "reposicion", f"linea_{self.linea.pk}": "1", f"cantidad_{self.linea.pk}": "1",
        }, follow=True)
        comp = Compensacion.objects.get(incidencia=self.inc)
        self.assertEqual((comp.estado, comp.lineas[0]["cantidad"], comp.creada_por), (Compensacion.COTIZADA, 1, "mesa"))
        self.assertContains(respuesta, "falta aprobarla")
        with patch("apps.pedidos.services.reponer_lineas", return_value=[MagicMock(cantidad=1)]) as reponer:
            respuesta = self.client.post(self.url, {"accion": "compensacion_aprobar", "compensacion_id": comp.pk}, follow=True)
        reponer.assert_called_once()
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.assertContains(respuesta, "Reposición aprobada")

    def test_aprobar_y_ejecutar_ya(self):
        with patch("apps.pedidos.services.reponer_lineas", return_value=[MagicMock(cantidad=2)]) as reponer:
            self.client.post(self.url, {
                "accion": "compensacion_crear", "tipo": "reposicion", f"linea_{self.linea.pk}": "1", "aprobar": "1",
            }, follow=True)
        reponer.assert_called_once()
        self.assertEqual(reponer.call_args.args[1], [(self.linea, 2)])  # sin cantidad capturada: todas
        self.assertEqual(Compensacion.objects.get().estado, Compensacion.APROBADA)

    def test_reembolso_rechazado_se_reintenta_desde_el_expediente(self):
        with patch("apps.integraciones.services.reembolsar_en_shopify", side_effect=ShopifyError("boom")):
            respuesta = self.client.post(self.url, {
                "accion": "compensacion_crear", "tipo": "reembolso", f"linea_{self.linea.pk}": "1",
                "reembolsar_envio": "1", "avisar_comprador": "1", "aprobar": "1",
            }, follow=True)
        comp = Compensacion.objects.get()
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.assertTrue(comp.reembolsar_envio)
        self.assertContains(respuesta, "Reintentar reembolso")
        with patch("apps.integraciones.services.reembolsar_en_shopify", return_value=("gid://shopify/Refund/1", Decimal("650"))):
            respuesta = self.client.post(self.url, {"accion": "compensacion_ejecutar", "compensacion_id": comp.pk}, follow=True)
        comp.refresh_from_db()
        self.assertEqual((comp.estado, comp.monto), (Compensacion.PAGADA, Decimal("650")))
        self.assertContains(respuesta, "hecho en Shopify")

    def test_cupon_se_paga_a_mano_con_referencia(self):
        self.client.post(self.url, {"accion": "compensacion_crear", "tipo": "cupon", "monto": "100", "aprobar": "1"}, follow=True)
        comp = Compensacion.objects.get()
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.client.post(self.url, {"accion": "compensacion_avanzar", "compensacion_id": comp.pk, "nuevo_estado": "PAGADA",
                                    "referencia_pago": "CUPON-10"}, follow=True)
        comp.refresh_from_db()
        self.assertEqual((comp.estado, comp.referencia_pago), (Compensacion.PAGADA, "CUPON-10"))

    def test_error_de_validacion_se_muestra(self):
        respuesta = self.client.post(self.url, {"accion": "compensacion_crear", "tipo": "reposicion"}, follow=True)
        self.assertContains(respuesta, "Elige qué productos se reponen.")
        self.assertFalse(Compensacion.objects.exists())
