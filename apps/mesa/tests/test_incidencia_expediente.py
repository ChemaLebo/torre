"""Expediente de la incidencia en Mesa: link a la orden de Shopify del pedido
(Chema 2026-09-23), solo cuando el pedido viene de una tienda."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.pedidos.models import Pedido


class ExpedienteIncidenciaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def _detalle(self, pedido):
        inc = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL, pedido=pedido, texto="Retraso")
        return self.client.get(reverse("mesa:incidencia_detalle", args=[inc.pk])).content.decode()

    def test_pedido_de_tienda_lleva_a_la_orden_de_shopify(self):
        pedido = crear_pedido(self.cliente, self.tienda, shopify_order_id="8401962139810")
        html = self._detalle(pedido)
        self.assertIn(f"https://{self.tienda.dominio}/admin/orders/8401962139810", html)
        self.assertIn("Ver orden #8401962139810 en Shopify", html)

    def test_lista_todas_las_guias_una_por_caja(self):
        from decimal import Decimal

        from apps.envios.models import Guia, Paquete

        pedido = crear_pedido(self.cliente, self.tienda)
        for n in (1, 2):
            caja = Paquete.objects.create(pedido=pedido, numero=n, peso_kg=Decimal("2"), carrier="estafeta", estado=Paquete.EMPACADO)
            Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero=f"EST-{n}", proveedor="mock")
        cancelada = Guia.objects.create(pedido=pedido, carrier="imile", numero="IM-VIEJA", proveedor="mock", estado=Guia.CANCELADA)
        html = self._detalle(pedido)
        self.assertIn("Guía caja 1: estafeta", html)
        self.assertIn("EST-1", html)
        self.assertIn("Guía caja 2: estafeta", html)
        self.assertIn("EST-2", html)
        self.assertIn("IM-VIEJA", html)
        self.assertIn("(cancelada)", html)
        self.assertEqual(cancelada.es_activa, False)

    def test_pedido_manual_no_tiene_link(self):
        pedido = Pedido.objects.create(cliente=self.cliente, origen="manual", comprador_nombre="Ana", cp="44100")
        self.assertNotIn("en Shopify", self._detalle(pedido))
