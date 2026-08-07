"""Picking móvil (C2): confirmar vía JSON sin reload, ruta por ubicación y
el visor de cámara con su fallback manual siempre visible."""
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.pedidos.models import LineaPedido

from .base import PisoTestCase


class PickingJsonTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=3)
        from apps.pedidos.services import iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        self.url = reverse("piso:picking_pedido", args=[self.pedido.pk])

    def _post_json(self, datos):
        return self.client.post(self.url, datos, HTTP_ACCEPT="application/json")

    def test_confirmar_via_json_regresa_avance_sin_reload(self):
        respuesta = self._post_json({"codigo": self.sku.codigo_barras, "cantidad": "1"})
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["sku"], self.sku.codigo)
        self.assertEqual(datos["pickeada"], 1)
        self.assertEqual(datos["restante"], 2)
        self.assertFalse(datos["completo"])
        self.assertEqual(datos["avance"], {"pickeadas": 1, "total": 3})
        self.assertIsNone(datos["siguiente"])

    def test_confirmar_todas_completa_y_regresa_url_de_empaque(self):
        self._post_json({"codigo": self.sku.codigo_barras, "cantidad": "1"})
        # Botón "×N confirmar todas": manda la cantidad restante en un POST.
        respuesta = self._post_json({"codigo": self.sku.codigo_barras, "cantidad": "2"})
        datos = respuesta.json()
        self.assertTrue(datos["ok"])
        self.assertTrue(datos["completo"])
        self.assertEqual(
            datos["siguiente"], reverse("piso:empaque_pedido", args=[self.pedido.pk]),
        )
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 3)

    def test_codigo_equivocado_regresa_400_con_el_mensaje_del_service(self):
        respuesta = self._post_json({"codigo": "0000000000000", "cantidad": "1"})
        self.assertEqual(respuesta.status_code, 400)
        datos = respuesta.json()
        self.assertFalse(datos["ok"])
        self.assertIn("Código equivocado", datos["error"])
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 0)

    def test_post_clasico_sigue_funcionando_sin_json(self):
        respuesta = self.client.post(self.url, {
            "codigo": self.sku.codigo_barras, "cantidad": "1",
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 1)


class PickingPantallaTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=3)
        from apps.pedidos.services import iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        self.url = reverse("piso:picking_pedido", args=[self.pedido.pk])

    def test_template_incluye_escaner_js_y_fallback_manual(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "js/escaner.js")
        self.assertContains(respuesta, "visor-escaner")
        self.assertContains(respuesta, 'name="codigo"')  # fallback SIEMPRE visible
        self.assertContains(respuesta, "Pausar pedido")

    def test_muestra_la_ubicacion_fefo_en_grande(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "pick-ubicacion")
        self.assertContains(respuesta, "A-01-1")

    def test_lineas_ordenadas_por_ruta_de_ubicacion(self):
        # SKU extra con stock en un anaquel ANTERIOR (A-00-1): aunque su línea
        # se agregó al final, la ruta lo pone primero.
        sku2 = SKU.objects.create(
            cliente=self.cliente, codigo="PARAMO-SIX", codigo_barras="7509876543210",
            descripcion="Páramo six pack", peso_gr=2000, requiere_lote=False,
        )
        ubic2 = Ubicacion.objects.create(codigo="A-00-1", tipo=Ubicacion.PICKING)
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea_asn = LineaASN.objects.create(orden=orden, sku=sku2, cantidad_anunciada=5)
        from apps.inventario.services import recibir, ubicar
        recibir(linea_asn, 5, 0, self.operador)
        ubicar(sku2, 5, ubic2, None, self.operador)
        LineaPedido.objects.create(pedido=self.pedido, sku=sku2, cantidad=1)

        respuesta = self.client.get(self.url)
        codigos = [l.sku.codigo for l in respuesta.context["lineas"]]
        self.assertEqual(codigos, ["PARAMO-SIX", "COLIMITA-SIX"])
