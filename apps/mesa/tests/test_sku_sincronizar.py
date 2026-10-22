"""Botón «Sincronizar con Shopify» por SKU en Mesa (Chema 2026-10-21): trae la
ficha de la variante y pisa lo de Torre con lo que Shopify trae con valor; lo
vacío allá no borra nada; un "por completar" que recibe peso se activa."""
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios.tests.base import crear_tienda
from apps.integraciones.shopify import ShopifyError

from .test_clientes_gestion import BaseGestionClientes


def ficha(vid, sku, peso_gr=0, medidas=(0, 0, 0), barcode="", precio="0", producto="", titulo="Default Title", con_medidas=True):
    return {"variant_id": vid, "sku": sku, "titulo_variante": titulo, "producto": producto, "tipo": "", "estado": "ACTIVE",
            "codigo_barras": barcode, "precio": precio, "peso_gr": peso_gr,
            "largo_cm": medidas[0], "ancho_cm": medidas[1], "alto_cm": medidas[2], "con_medidas": con_medidas}


class SincronizarSkuShopifyTests(BaseGestionClientes):
    def setUp(self):
        super().setUp()
        self.tienda = crear_tienda(self.colima, token="shpat_prueba")
        self.url = reverse("mesa:cliente_skus", args=[self.colima.pk])
        self.sku = SKU.objects.create(
            cliente=self.colima, codigo="SM24LOR", descripcion="SM24LOR", shopify_variant_id="5",
            precio_declarado=Decimal("1000.00"), activo=False,
        )

    def _sincronizar(self, sku, por_id=None, por_codigo=None, error=None):
        with patch("apps.integraciones.services.ShopifyClient") as cliente_cls:
            api = cliente_cls.return_value
            if error is not None:
                api.fichas_variantes.side_effect = error
                api.ficha_por_codigo.side_effect = error
            else:
                api.fichas_variantes.return_value = {por_id["variant_id"]: por_id} if por_id else {}
                api.ficha_por_codigo.return_value = por_codigo
            respuesta = self.client.post(self.url, {"accion": "sku_sincronizar", "sku_id": sku.pk}, follow=True)
            self.api = api
        sku.refresh_from_db()
        return respuesta

    def test_boton_con_modal_de_confirmacion_en_la_fila_y_en_la_edicion(self):
        html = self.client.get(self.url + f"?editar={self.sku.pk}").content.decode()
        self.assertIn('<dialog class="modal" id="modal-sincronizar"', html)
        self.assertIn('<input type="hidden" name="accion" value="sku_sincronizar">', html)
        self.assertIn('<input type="hidden" name="sku_id" id="sincronizar-sku-id" value="">', html)
        self.assertEqual(html.count(f'data-sku="{self.sku.pk}" data-codigo="SM24LOR" onclick="abrirSincronizar(this)"'), 2)  # fila + modal de edición
        self.assertIn("<b>pisa</b> lo capturado en Torre", html)
        self.assertNotIn("confirm(", html)  # modal propio, no el del navegador (Chema 2026-10-21)
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        sin_tienda = self.client.get(self.url).content.decode()
        self.assertNotIn("modal-sincronizar", sin_tienda)  # sin tienda conectada no hay botón ni modal
        self.assertNotIn("abrirSincronizar(this)", sin_tienda)

    def test_por_completar_recibe_lo_de_shopify_y_se_activa(self):
        respuesta = self._sincronizar(self.sku, por_id=ficha(
            "5", "SM24LOR", peso_gr=15000, medidas=(40, 26, 25), barcode="7503049667126", precio="1000.00",
            producto="SODA MATE ORIGINAL LATA | 24 Pack | 355ml",
        ))
        self.assertEqual(
            (self.sku.activo, self.sku.peso_gr, self.sku.largo_cm, self.sku.ancho_cm, self.sku.alto_cm, self.sku.codigo_barras, self.sku.descripcion),
            (True, 15000, 40, 26, 25, "7503049667126", "SODA MATE ORIGINAL LATA | 24 Pack | 355ml"),
        )
        self.assertContains(respuesta, f"SM24LOR sincronizado desde {self.tienda.dominio}: nombre, código de barras, peso, largo, ancho, alto, activado.")
        evento = EventoAuditoria.objects.get(entidad="sku", entidad_id="SM24LOR", accion="sku_sincronizado_desde_shopify")
        self.assertEqual((evento.delta["peso_gr"], evento.delta["activo"], evento.delta["tienda"]), (["0", "15000"], ["False", "True"], self.tienda.dominio))
        self.assertEqual((evento.actor_tipo, evento.actor_id), ("usuario", "mesa1"))
        self.api.ficha_por_codigo.assert_not_called()

    def test_lo_vacio_en_shopify_no_borra_lo_de_torre(self):
        SKU.objects.filter(pk=self.sku.pk).update(activo=True, peso_gr=14700, largo_cm=40, ancho_cm=30, alto_cm=26, codigo_barras="750100", variante="Caja")
        self.sku.refresh_from_db()
        respuesta = self._sincronizar(self.sku, por_id=ficha("5", "SM24LOR", producto="SM24LOR", precio="1000.00"))
        self.assertEqual((self.sku.peso_gr, self.sku.largo_cm, self.sku.codigo_barras, self.sku.variante, self.sku.activo), (14700, 40, "750100", "Caja", True))
        self.assertContains(respuesta, f"SM24LOR ya estaba igual que en {self.tienda.dominio}.")
        self.assertFalse(EventoAuditoria.objects.filter(accion="sku_sincronizado_desde_shopify").exists())

    def test_inactivo_por_decision_de_mesa_recibe_peso_pero_no_se_activa(self):
        SKU.objects.filter(pk=self.sku.pk).update(peso_gr=14700)  # inactivo con peso: no es "por completar"
        self.sku.refresh_from_db()
        respuesta = self._sincronizar(self.sku, por_id=ficha("5", "SM24LOR", peso_gr=15000))
        self.assertEqual((self.sku.peso_gr, self.sku.activo), (15000, False))
        self.assertContains(respuesta, "sincronizado desde")
        self.assertNotContains(respuesta, "activado")

    def test_sin_id_se_busca_por_codigo_y_se_liga(self):
        manual = SKU.objects.create(cliente=self.colima, codigo="MANUAL-1", descripcion="A mano", peso_gr=500, activo=True)
        respuesta = self._sincronizar(manual, por_codigo=ficha("7", "MANUAL-1", peso_gr=600, barcode="750999", producto="A mano"))
        self.assertEqual((manual.shopify_variant_id, manual.peso_gr, manual.codigo_barras, manual.activo), ("7", 600, "750999", True))
        self.assertContains(respuesta, "ligado a su variante, código de barras, peso.")
        self.api.fichas_variantes.assert_not_called()

    def test_sin_medidas_en_la_api_avisa(self):
        respuesta = self._sincronizar(self.sku, por_id=ficha("5", "SM24LOR", peso_gr=15000, con_medidas=False))
        self.assertEqual((self.sku.peso_gr, self.sku.largo_cm, self.sku.activo), (15000, 0, True))
        self.assertContains(respuesta, "La API no dio las medidas empacadas (versión 2027-01): solo peso.")

    def test_errores_claros(self):
        respuesta = self._sincronizar(self.sku, por_id=None, por_codigo=None)
        self.assertContains(respuesta, "SM24LOR no está en Shopify: ni por id de variante ni por código exacto.")
        respuesta = self._sincronizar(self.sku, error=ShopifyError("HTTP 502"))
        self.assertContains(respuesta, "Shopify no respondió: HTTP 502")
        self.tienda.token = ""
        self.tienda.save(update_fields=["token"])
        respuesta = self._sincronizar(self.sku, por_id=ficha("5", "SM24LOR", peso_gr=1))
        self.assertContains(respuesta, "Cervecería Colima no tiene una tienda de Shopify conectada.")
        self.assertEqual((self.sku.peso_gr, self.sku.activo), (0, False))
        self.assertEqual(self.client.post(self.url, {"accion": "sku_sincronizar", "sku_id": 999999}).status_code, 404)
