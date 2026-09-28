"""Levantar una incidencia desde Mesa (Chema 2026-09-28): por folio o número
de orden de Shopify, con SKU opcional, prioridad del tipo, interna, y la
agrupación cuando el pedido ya tiene un caso abierto del mismo tipo."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.core.models import PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia, MensajeIncidencia
from apps.pedidos.models import Pedido


class IncidenciaNuevaMesaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, shopify_order_name="#33713")
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.url = reverse("mesa:incidencia_nueva")

    def _abrir(self, **extra):
        datos = {"pedido": self.pedido.pk, "tipo": Incidencia.TIPO_DAN, "texto": "Llegó la caja rota."}
        datos.update(extra)
        return self.client.post(self.url, datos, follow=True)

    def test_la_lista_y_el_pedido_llevan_al_formulario_con_el_pedido_puesto(self):
        self.assertContains(self.client.get(reverse("mesa:incidencias")), self.url)
        self.assertContains(self.client.get(reverse("mesa:pedidos")), f"{self.url}?pedido={self.pedido.pk}")
        html = self.client.get(f"{self.url}?pedido={self.pedido.pk}").content.decode()
        self.assertIn(f'<option value="{self.pedido.pk}" selected>{self.pedido.folio} · #33713 · Ana Compradora · {self.cliente.nombre} · Empacado</option>', html)
        self.assertNotIn('name="cliente"', html)  # el cliente es el del pedido, siempre
        self.assertNotIn("PAQ ·", html)  # esa la abre el planificador
        self.assertEqual(self.client.get(f"{self.url}?pedido=abc").status_code, 200)

    def test_abre_con_origen_manual_prioridad_del_tipo_y_avisa_al_cliente(self):
        from unittest.mock import patch

        with patch("apps.mensajeria.services.notificar_cliente_incidencia") as avisar:
            respuesta = self._abrir()
        inc = Incidencia.objects.get(pedido=self.pedido)
        self.assertRedirects(respuesta, reverse("mesa:incidencia_detalle", args=[inc.pk]))
        self.assertEqual((inc.origen, inc.tipo, inc.prioridad, inc.interna), ("manual", "DAN", "P1", False))
        self.assertEqual(inc.cliente, self.cliente)  # el del pedido, sin elegirlo
        self.assertEqual(inc.mensajes.get().rol_autor, MensajeIncidencia.ROL_MESA)
        avisar.assert_called_once()
        self.assertContains(respuesta, f"Incidencia {inc.folio} abierta")

    def test_por_numero_de_orden_con_sku_prioridad_e_interna(self):
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX-COL", descripcion="Six")
        respuesta = self._abrir(sku="six-col", prioridad="P3", interna="on", tipo=Incidencia.TIPO_FAL)
        inc = Incidencia.objects.get(pedido=self.pedido)
        self.assertEqual((inc.sku, inc.prioridad, inc.interna, inc.tipo), (sku, "P3", True, "FAL"))
        self.assertEqual(respuesta.status_code, 200)

    def test_el_pedido_es_obligatorio_y_el_desplegable_trae_los_recientes_mas_el_prellenado(self):
        from datetime import timedelta

        from django.utils import timezone

        respuesta = self.client.post(self.url, {"pedido": "", "tipo": "DES", "texto": "Descuadre"})
        self.assertContains(respuesta, "Elige el pedido: el cliente de la incidencia es el del pedido.")
        self.assertFalse(Incidencia.objects.exists())
        viejo = crear_pedido(self.cliente, self.tienda)
        Pedido.objects.filter(pk=viejo.pk).update(creado=timezone.now() - timedelta(days=90))
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'<option value="{self.pedido.pk}">', html)
        self.assertNotIn(f'<option value="{viejo.pk}">', html)  # fuera de la ventana: no estorba
        html = self.client.get(f"{self.url}?pedido={viejo.pk}").content.decode()
        self.assertIn(f'<option value="{viejo.pk}" selected>', html)  # prellenado aunque sea viejo
        respuesta = self.client.post(self.url, {"pedido": viejo.pk, "tipo": "RET", "texto": "x"})
        self.assertEqual(respuesta.status_code, 200)  # fuera de la lista sin prellenar: no se acepta

    def test_segundo_reporte_del_mismo_tipo_se_agrupa_y_lo_dice(self):
        self._abrir()
        respuesta = self._abrir(texto="Y la otra caja también.")
        inc = Incidencia.objects.get(pedido=self.pedido)
        self.assertEqual(inc.mensajes.count(), 2)
        self.assertContains(respuesta, f"tu reporte quedó en {inc.folio}")
