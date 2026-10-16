"""Salida del pedido entero (Chema 2026-10-15, PED-00151): al registrar la
salida de un pedido sin cajas por caja, también salen los registros de cajas
que no estaban empacadas (planeadas después del empaque); si no, se quedan
vivos y una reposición los vuelve a ofrecer en empaque."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.catalogo.models import SKU
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


class SalidaEnteraTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="12PKCCL", descripcion="12 pack", peso_gr=7000)

    def test_salida_del_pedido_entero_saca_tambien_la_caja_planeada(self):
        pedido = crear_pedido(self.cliente, self.tienda, estado=Pedido.GUIA_GENERADA, es_local=False)
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=1, reservada=True, cantidad_pickeada=1)
        # La caja nació cuando Mesa forzó el carrier, ya con el pedido empacado entero: quedó planeada.
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal(7), carrier="estafeta", estado=Paquete.PLANEADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero="G-1", proveedor="envia", estado=Guia.GUIA_CREADA)
        self.assertEqual(services.cajas_por_salir(pedido), [])  # sin cajas empacadas: sale entero
        with patch("apps.inventario.services.despachar") as despachar, patch("apps.integraciones.services.marcar_fulfillment"), \
             patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, None)
        caja.refresh_from_db()
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((caja.estado, pedido.estado, linea.cantidad_despachada), (Paquete.DESPACHADO, Pedido.RECOLECTADO, 1))
        despachar.assert_called_once_with(self.sku, 1, pedido.folio)
        # Nada queda vivo: una reposición ya no la vuelve a ofrecer.
        self.assertFalse(pedido.paquetes.exclude(estado=Paquete.DESPACHADO).exists())
