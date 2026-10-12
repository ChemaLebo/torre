"""Caja sin salida en Mesa (Chema 2026-10-12): contador en Salida, la
incidencia interna con "Salió sin registro" y la píldora en el pedido."""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import PerfilUsuario
from apps.envios.models import Guia, Manifiesto, Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


class CajaSinSalidaMesaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=4000)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado=Pedido.GUIA_GENERADA, es_local=False)
        linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=1, reservada=True, cantidad_pickeada=1)
        nacio = timezone.now() - timezone.timedelta(days=3)
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO, ts_cierre=nacio)
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=linea, cantidad=1)
        guia = Guia.objects.create(pedido=self.pedido, paquete=self.caja, carrier="estafeta", numero="EST-1", proveedor="mock", estado=Guia.GUIA_CREADA)
        Guia.objects.filter(pk=guia.pk).update(creado=nacio)
        Manifiesto.objects.create(carrier="estafeta", corral="SAL-OTRO", chofer="Chofer")
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_contador_en_salida_incidencia_con_boton_y_pildora_en_el_pedido(self):
        salida = self.client.get(reverse("piso:salida"))
        self.assertContains(salida, "1 caja sin salida")
        self.assertContains(salida, self.pedido.folio)
        self.assertEqual(services.revisar_cajas_sin_salida()["abiertas"], 1)
        inc = Incidencia.objects.get(pedido=self.pedido, tipo="CSS")
        url = reverse("mesa:incidencia_detalle", args=[inc.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Salió sin registro", html)
        self.assertIn(f'name="caja_id" value="{self.caja.pk}"', html)
        self.assertNotIn("CSS ·", self.client.get(reverse("mesa:incidencia_nueva")).content.decode())  # la abre el revisor
        with patch("apps.mensajeria.services.enviar_en_camino"), patch("apps.integraciones.services.marcar_fulfillment"), \
             patch("apps.inventario.services.despachar"), self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "salida_sin_registro", "caja_id": self.caja.pk}, follow=True)
        self.assertContains(respuesta, "La caja 1 quedó como salida sin registro")
        self.caja.refresh_from_db()
        self.pedido.refresh_from_db()
        inc.refresh_from_db()
        self.assertEqual((self.caja.estado, self.pedido.estado, inc.estado), (Paquete.DESPACHADO, Pedido.RECOLECTADO, Incidencia.RESUELTA))
        detalle = self.client.get(reverse("mesa:pedido_detalle", args=[self.pedido.pk])).content.decode()
        self.assertIn("sin escaneo de salida", detalle)
        self.assertNotContains(self.client.get(reverse("piso:salida")), "sin salida</b>")
