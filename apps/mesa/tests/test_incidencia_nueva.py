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
        datos = {"pedido": self.pedido.folio, "tipo": Incidencia.TIPO_DAN, "texto": "Llegó la caja rota."}
        datos.update(extra)
        return self.client.post(self.url, datos, follow=True)

    def test_la_lista_y_el_pedido_llevan_al_formulario_con_el_pedido_puesto(self):
        self.assertContains(self.client.get(reverse("mesa:incidencias")), self.url)
        self.assertContains(self.client.get(reverse("mesa:pedidos")), f"{self.url}?pedido={self.pedido.pk}")
        html = self.client.get(f"{self.url}?pedido={self.pedido.pk}").content.decode()
        self.assertIn(f'value="{self.pedido.folio}"', html)
        self.assertIn(f'<option value="{self.cliente.pk}" selected', html)
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
        respuesta = self._abrir(pedido="33713", sku="six-col", prioridad="P3", interna="on", tipo=Incidencia.TIPO_FAL)
        inc = Incidencia.objects.get(pedido=self.pedido)
        self.assertEqual((inc.sku, inc.prioridad, inc.interna, inc.tipo), (sku, "P3", True, "FAL"))
        self.assertEqual(respuesta.status_code, 200)

    def test_sin_pedido_exige_cliente_y_el_pedido_desconocido_avisa(self):
        respuesta = self.client.post(self.url, {"pedido": "", "tipo": "DES", "texto": "Descuadre"})
        self.assertContains(respuesta, "Elige el cliente o captura un pedido.")
        respuesta = self.client.post(self.url, {"pedido": "PED-99999", "tipo": "DES", "texto": "x"})
        self.assertContains(respuesta, "No encuentro el pedido PED-99999")
        self.assertFalse(Incidencia.objects.exists())
        respuesta = self.client.post(self.url, {"cliente": self.cliente.pk, "tipo": "DES", "texto": "Descuadre"}, follow=True)
        inc = Incidencia.objects.get()
        self.assertIsNone(inc.pedido)
        self.assertEqual(inc.cliente, self.cliente)

    def test_segundo_reporte_del_mismo_tipo_se_agrupa_y_lo_dice(self):
        self._abrir()
        respuesta = self._abrir(texto="Y la otra caja también.")
        inc = Incidencia.objects.get(pedido=self.pedido)
        self.assertEqual(inc.mensajes.count(), 2)
        self.assertContains(respuesta, f"tu reporte quedó en {inc.folio}")
