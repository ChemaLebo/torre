"""Producto no registrado (Chema 2026-10-07, PED-00377 de Infinitea): un line
item cuyo producto no existe en Torre detiene el pedido con una incidencia
interna SKU (links al producto, correo solo a la lista fija, nada de SKU
provisional) y «Volver a leer la orden» lo reanuda cuando el producto ya
existe y tiene existencias."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.configuracion.models import CorreoIncidencias
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.integraciones.models import Tienda, WebhookEvento
from apps.integraciones.services import procesar_webhook, releer_orden
from apps.integraciones.tests.test_webhooks import payload_orders_create
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.inventario.services import recibir, ubicar
from apps.pedidos.models import Pedido


class ProductoNoRegistradoTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Infinitea", slug="infinitea", integracion_envios="envia")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="misteaque.myshopify.com", token="shpat_prueba", location_id="")
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.anaquel = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.matcha = SKU.objects.create(cliente=self.cliente, codigo="609143618037", descripcion="Celestial Matcha", peso_gr=200, requiere_lote=False)
        self._stock(self.matcha, 5)
        CorreoIncidencias.objects.create(correo="ops@torre.mx")
        CorreoIncidencias.objects.create(correo="dueno@infinitea.mx", cliente=self.cliente)

    def _stock(self, sku, n):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=n)
        recibir(linea, n, 0, "piso1")
        ubicar(sku, n, self.anaquel, None, "piso1")

    def _payload(self, kit_sku=None, order_id=6064519708752):
        payload = payload_orders_create(order_id=order_id, numero=4091)
        payload["line_items"] = [
            {"id": 1, "sku": kit_sku, "variant_id": 42283180163152, "product_id": 7000001, "quantity": 1, "current_quantity": 1, "title": "Kit Matcha", "price": "890.00", "grams": 900},
            {"id": 2, "sku": "609143618037", "variant_id": 41146722975824, "product_id": 7000002, "quantity": 1, "current_quantity": 1, "title": "Celestial Matcha", "price": "450.00", "grams": 200},
        ]
        return payload

    def _ingerir(self, payload, webhook_id="wh-1", topic="orders/create"):
        evento = WebhookEvento.objects.create(tienda=self.tienda, webhook_id=webhook_id, topic=topic, payload=payload)
        with self.captureOnCommitCallbacks(execute=True):
            procesar_webhook(evento)
        return Pedido.objects.get(tienda=self.tienda, shopify_order_id=str(payload["id"]))

    def test_item_sin_sku_detiene_el_pedido_con_incidencia_interna_y_links(self):
        pedido = self._ingerir(self._payload(kit_sku=None))
        self.assertEqual((pedido.estado, pedido.detenido, pedido.incidencia_activa), (Pedido.PENDIENTE, True, False))
        self.assertEqual(list(pedido.lineas.values_list("sku__codigo", "reservada")), [("609143618037", True)])  # la conocida sí; la desconocida no se inventa
        inc = Incidencia.objects.get(pedido=pedido)
        self.assertEqual((inc.tipo, inc.interna, inc.prioridad, inc.origen), (Incidencia.TIPO_SKU, True, Incidencia.P1, Incidencia.ORIGEN_AUTO))
        texto = inc.mensajes.first().texto
        self.assertIn("Kit Matcha × 1", texto)
        self.assertIn("sin SKU en Shopify", texto)
        self.assertIn("https://misteaque.myshopify.com/admin/products/7000001/variants/42283180163152", texto)
        self.assertFalse(Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_FAL).exists())  # ya no es FAL
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="ingesta")
        self.assertEqual(evento.delta["no_registrados"], ["Kit Matcha"])
        # Correo: interna → solo la lista fija de Torre, nunca la del cliente.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@torre.mx"])
        self.assertIn("SKU", mail.outbox[0].subject)

    def test_releer_reanuda_cuando_el_producto_existe_y_reserva(self):
        pedido = self._ingerir(self._payload(kit_sku=None))
        # 1) Sigue sin existir: nada cambia.
        with patch("apps.integraciones.services.ShopifyClient") as cls, self.captureOnCommitCallbacks(execute=True):
            cls.return_value.obtener_orden.return_value = self._payload(kit_sku=None)
            r = releer_orden(pedido, actor="mesa")
        self.assertEqual([d["titulo"] for d in r["no_registrados"]], ["Kit Matcha"])
        self.assertIn("Sigue(n) sin existir", r["mensaje"])
        pedido.refresh_from_db()
        self.assertTrue(pedido.detenido)
        # 2) Infinitea le puso SKU y el poller lo trajo, Mesa lo activó, pero sin existencias.
        kit = SKU.objects.create(cliente=self.cliente, codigo="KIT-MATCHA", descripcion="Kit Matcha", peso_gr=900, requiere_lote=False)
        with patch("apps.integraciones.services.ShopifyClient") as cls, self.captureOnCommitCallbacks(execute=True):
            cls.return_value.obtener_orden.return_value = self._payload(kit_sku="KIT-MATCHA")
            r = releer_orden(pedido, actor="mesa")
        pedido.refresh_from_db()
        self.assertEqual(r["sin_stock"], ["KIT-MATCHA"])
        self.assertIn("sin existencias", r["mensaje"])
        self.assertEqual(sorted(pedido.lineas.values_list("sku__codigo", "reservada")), [("609143618037", True), ("KIT-MATCHA", False)])
        self.assertTrue(pedido.detenido)
        self.assertTrue(Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_SKU).abierta)
        # 3) Entra stock (ASN recibido y acomodado): la reserva se completa sola y el pedido vuelve a la cola.
        with self.captureOnCommitCallbacks(execute=True):
            self._stock(kit, 3)
        pedido.refresh_from_db()
        self.assertFalse(pedido.detenido)
        self.assertEqual(pedido.lineas.get(sku=kit).reservada, True)
        inc = Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_SKU)
        self.assertEqual(inc.estado, Incidencia.RESUELTA)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="pedido_reanudado").exists())

    def test_releer_con_existencias_reanuda_de_una(self):
        pedido = self._ingerir(self._payload(kit_sku=None))
        kit = SKU.objects.create(cliente=self.cliente, codigo="KIT-MATCHA", descripcion="Kit Matcha", peso_gr=900, requiere_lote=False)
        self._stock(kit, 3)
        with patch("apps.integraciones.services.ShopifyClient") as cls, self.captureOnCommitCallbacks(execute=True):
            cls.return_value.obtener_orden.return_value = self._payload(kit_sku="KIT-MATCHA")
            r = releer_orden(pedido, actor="mesa")
        pedido.refresh_from_db()
        self.assertTrue(r["reanudado"])
        self.assertEqual((pedido.detenido, pedido.lineas.count()), (False, 2))
        self.assertEqual(Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_SKU).estado, Incidencia.RESUELTA)
        self.assertEqual(len(mail.outbox), 2)  # abierta + reanudada, ambas solo a Torre
        self.assertEqual({tuple(m.to) for m in mail.outbox}, {("ops@torre.mx",)})

    def test_edicion_que_agrega_producto_desconocido_tambien_detiene(self):
        pedido = self._ingerir(self._payload(kit_sku="609143618037"))  # las dos líneas conocidas
        self.assertFalse(pedido.detenido)
        editada = self._payload(kit_sku=None)
        editada["updated_at"] = "2026-07-21T11:00:00-06:00"
        pedido = self._ingerir(editada, webhook_id="wh-2", topic="orders/updated")
        self.assertTrue(pedido.detenido)
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_SKU, interna=True).exists())


class MesaProductoNoRegistradoTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Infinitea", slug="infinitea", integracion_envios="envia")
        self.tienda = Tienda.objects.create(cliente=self.cliente, dominio="misteaque.myshopify.com", token="shpat_prueba", location_id="")
        self.pedido = Pedido.objects.create(cliente=self.cliente, tienda=self.tienda, shopify_order_id="6064519708752", origen="webhook", comprador_nombre="Ana", cp="44100", es_local=False, estado=Pedido.PENDIENTE)
        mesa = get_user_model().objects.create_user("mesa-sku", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_levantar_a_mano_detiene_y_el_boton_vuelve_a_leer(self):
        respuesta = self.client.get(reverse("mesa:incidencia_nueva"))
        self.assertContains(respuesta, "SKU · Producto no registrado")
        respuesta = self.client.post(reverse("mesa:incidencia_nueva"), {
            "cliente": self.cliente.pk, "tipo": "SKU", "pedido": self.pedido.pk, "texto": "Kit Matcha sin dar de alta", "prioridad": "P1",
        }, follow=True)
        self.assertContains(respuesta, "queda detenido hasta que el producto exista")
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.detenido)
        inc = Incidencia.objects.get(pedido=self.pedido, tipo=Incidencia.TIPO_SKU)
        self.assertTrue(inc.interna)
        self.assertContains(respuesta, 'value="releer_orden"')
        with patch("apps.mesa.views.releer_orden", create=True) as _no, patch("apps.integraciones.services.ShopifyClient") as cls:
            cls.return_value.obtener_orden.side_effect = Exception("API caída")
            respuesta = self.client.post(reverse("mesa:incidencia_detalle", args=[inc.pk]), {"accion": "releer_orden"}, follow=True)
        self.assertContains(respuesta, "No se pudo leer la orden en Shopify")
