"""Reingresos por caja (Chema 2026-10-20/21, PED-00151): pantalla "Reingresos"
en el menú con las cajas por decidir y el modal pieza por pieza; "Registrar
reingreso" desde el pedido para una caja despachada que regresó (la guía pasa
a retorno y cada pieza va a donde se decidió, o queda por decidir con
"Generar reingreso"); "Crear recepción" sigue mandando al piso; Recepciones ya
no lista reingresos."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Incidencia
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import recibir, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def suma(sku, estado):
    return sum(Saldo.objects.filter(sku=sku, estado=estado).values_list("cantidad", flat=True))


@override_settings(ENVIA_API_KEY="")
class ReingresosPorCajaTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="12PKCC0", descripcion="12 pack Colima Cero", peso_gr=7300,
                                      requiere_lote=False, precio_declarado=Decimal(449))
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="B-6", tipo=Ubicacion.PICKING)
        self.mesa = get_user_model().objects.create_user(username="mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=5)
        recibir(linea, 5, 0, self.mesa)
        ubicar(self.sku, 5, self.pic, None, self.mesa)
        self.client.force_login(self.mesa)
        # PED-00151: dos cajas despachadas; la 2 (paquetexpress) regresó y envia.com nunca avisó.
        self.pedido = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana",
                                            cp="01780", estado=Pedido.EN_TRANSITO)
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=self.sku, cantidad=2, reservada=True, cantidad_pickeada=2, cantidad_despachada=2)
        self.c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("7"), carrier="estafeta", estado=Paquete.DESPACHADO)
        self.c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal("7"), carrier="paquetexpress", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=self.linea, cantidad=1)
        PaqueteLinea.objects.create(paquete=self.c2, linea_pedido=self.linea, cantidad=1)
        self.g1 = Guia.objects.create(pedido=self.pedido, paquete=self.c1, carrier="estafeta", numero="605870980061070999AM26", proveedor="envia", estado=Guia.ENTREGADO)
        self.g2 = Guia.objects.create(pedido=self.pedido, paquete=self.c2, carrier="paquetexpress", numero="211254693751", proveedor="envia", estado=Guia.RECOLECTADO)
        self.url_pedido = reverse("mesa:pedido_detalle", args=[self.pedido.pk])
        self.url_reingresos = reverse("mesa:reingresos")

    def test_registrar_reingreso_desde_el_pedido_y_resolver_pieza_por_pieza(self):
        html = self.client.get(self.url_pedido).content.decode()
        self.assertIn(f"modal-reingreso-{self.c2.pk}", html)
        self.assertNotIn(f"modal-reingreso-{self.c1.pk}", html)  # la 1 ya se entregó
        self.assertIn("Ya salió del kardex: lo que regrese entra como retorno", html)
        respuesta = self.client.post(self.url_pedido, {
            "accion": "reingreso_caja", "folio": self.pedido.folio, "caja": self.c2.pk,
            f"pieza-{self.sku.pk}-0-sku": self.sku.pk, f"pieza-{self.sku.pk}-0-cantidad": "1", f"pieza-{self.sku.pk}-0-destino": "bodega",
            f"pieza-{self.sku.pk}-0-ubicacion": "B-6", f"pieza-{self.sku.pk}-0-lote": "", "motivo": "regresó por paquetexpress sin aviso",
        }, follow=True)
        self.assertContains(respuesta, "cada pieza quedó donde decidiste")
        self.c2.refresh_from_db(); self.g2.refresh_from_db(); self.pedido.refresh_from_db()
        self.assertEqual((self.g2.estado, self.c2.reingreso_estado), (Guia.RETORNO, Paquete.REINGRESADO))
        self.assertIsNotNone(self.c2.regreso_reportado)
        self.assertEqual(self.pedido.estado, Pedido.ENTREGADO)  # las demás cajas ya se entregaron
        self.assertEqual(suma(self.sku, Saldo.UBICADO_VENDIBLE), 6)
        self.assertTrue(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RETORNO, delta=1, estado_destino="ubicado_vendible").exists())
        self.assertTrue(Incidencia.objects.filter(pedido=self.pedido, tipo="RF").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(self.g2.pk), accion="retorno_manual").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk), accion="reingreso_resuelto").exists())
        self.assertEqual(services.cajas_por_reingresar(self.pedido), [])

    def test_generar_reingreso_deja_la_caja_en_la_pantalla_de_reingresos(self):
        respuesta = self.client.post(self.url_pedido, {
            "accion": "reingreso_caja", "folio": self.pedido.folio, "caja": self.c2.pk, "generar_reingreso": "1", "motivo": "aún no la tengo",
        }, follow=True)
        self.assertContains(respuesta, "queda en Reingresos por decidir")
        self.c2.refresh_from_db(); self.g2.refresh_from_db()
        self.assertEqual((self.g2.estado, self.c2.reingreso_estado), (Guia.RETORNO, ""))
        self.assertIsNotNone(self.c2.regreso_reportado)
        self.assertEqual([c.pk for c in services.cajas_por_reingresar(self.pedido)], [self.c2.pk])
        # La pantalla de Reingresos la lista con su modal; Recepciones ya no.
        html = self.client.get(self.url_reingresos).content.decode()
        self.assertIn(self.pedido.folio, html)
        self.assertIn("regresada por el carrier", html)
        self.assertIn(f"modal-reingreso-{self.c2.pk}", html)
        self.assertIn('value="reingreso_caja"', html)
        self.assertIn("Crear recepción", html)
        self.assertNotIn("Reingresos por decidir", self.client.get(reverse("mesa:recepciones")).content.decode())
        # Badge del menú en Reingresos.
        nav = self.client.get(reverse("mesa:dashboard")).content.decode()
        self.assertIn('href="/mesa/reingresos/"', nav) if "/mesa/reingresos/" in nav else self.assertIn("Reingresos", nav)
        # Y se resuelve desde ahí: no regresa.
        respuesta = self.client.post(self.url_reingresos, {
            "accion": "reingreso_caja", "folio": self.pedido.folio, "caja": self.c2.pk,
            f"pieza-{self.sku.pk}-0-sku": self.sku.pk, f"pieza-{self.sku.pk}-0-cantidad": "1", f"pieza-{self.sku.pk}-0-destino": "merma", "motivo": "se perdió",
        }, follow=True)
        self.assertRedirects(respuesta, self.url_reingresos, fetch_redirect_response=False)
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.reingreso_estado, Paquete.NO_RECUPERADO)
        self.assertEqual(suma(self.sku, Saldo.UBICADO_VENDIBLE), 5)

    def test_crear_recepcion_sigue_mandando_al_piso(self):
        self.client.post(self.url_pedido, {"accion": "reingreso_caja", "folio": self.pedido.folio, "caja": self.c2.pk, "generar_reingreso": "1"}, follow=True)
        respuesta = self.client.post(self.url_reingresos, {"accion": "reingreso", "pedido_id": self.pedido.pk, "motivo": "la cuenta el piso"}, follow=True)
        self.assertContains(respuesta, "Recepción")
        self.assertContains(respuesta, "el piso la recibe y la acomoda")
        orden = self.pedido.reingresos.get()
        self.assertEqual((orden.tipo, orden.lineas.get().cantidad_anunciada), ("reingreso", 1))
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.reingreso_estado, Paquete.REINGRESADO)

    def test_no_se_registra_regreso_de_una_caja_entregada(self):
        respuesta = self.client.post(self.url_pedido, {"accion": "reingreso_caja", "folio": self.pedido.folio, "caja": self.c1.pk, "generar_reingreso": "1"}, follow=True)
        self.c1.refresh_from_db(); self.g1.refresh_from_db()
        # La guía entregada es terminal: no se toca; la caja sí queda reportada (Mesa manda) para decidir.
        self.assertEqual(self.g1.estado, Guia.ENTREGADO)
        self.assertIsNotNone(self.c1.regreso_reportado)
        self.assertContains(respuesta, "queda en Reingresos por decidir")
