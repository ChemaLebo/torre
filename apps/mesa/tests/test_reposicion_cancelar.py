"""Reposiciones en Mesa (Chema 2026-10-14, PED-00261): la reposición aprobada
se ve en la tarjeta Cajas del pedido desde que nace (aunque sin existencias
no haya caja planeada), con a qué caja repone, cuándo se aprobó y en qué va;
y se cancela desde ahí (o desde la incidencia) mientras el piso no la tome:
no es un reembolso, el pedido regresa a donde estaba con su guía original."""
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Compensacion, Incidencia
from apps.incidencias.services import abrir_incidencia, crear_compensacion
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.inventario.services import recibir, ubicar
from apps.pedidos.models import LineaPedido, Pedido


@override_settings(ENVIA_API_KEY="", MEDIA_ROOT=tempfile.mkdtemp(prefix="torre-repo-mesa-"))
class ReposicionEnMesaTests(TestCase):
    """Bodega mínima: A con 10 piezas ubicadas, B sin existencias (como pedidos.tests.test_reposicion)."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.a = SKU.objects.create(cliente=self.cliente, codigo="A-SIX", descripcion="Six A", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        self.b = SKU.objects.create(cliente=self.cliente, codigo="B-SIX", descripcion="Six B", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.mesa = get_user_model().objects.create_user(username="mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.a, cantidad_anunciada=10)
        recibir(linea, 10, 0, self.mesa)
        ubicar(self.a, 10, self.pic, None, self.mesa)
        self.client.force_login(self.mesa)
        self.pedido = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana",
                                            cp="01780", estado=Pedido.EN_TRANSITO)
        self.la = LineaPedido.objects.create(pedido=self.pedido, sku=self.a, cantidad=2, reservada=True, cantidad_pickeada=2, cantidad_despachada=2)
        self.lb = LineaPedido.objects.create(pedido=self.pedido, sku=self.b, cantidad=1, reservada=True, cantidad_pickeada=1, cantidad_despachada=1)
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("6"), carrier="noventa9Minutos", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.la, cantidad=2)
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.lb, cantidad=1)
        self.guia = Guia.objects.create(pedido=self.pedido, paquete=self.caja, carrier="noventa9Minutos", numero="1865484451",
                                        proveedor="envia", estado=Guia.EN_TRANSITO)
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="No llegó")

    def aprobar_reposicion(self, lineas):
        with self.captureOnCommitCallbacks(execute=True):
            comp = crear_compensacion(self.inc, "reposicion", self.mesa, "mesa", lineas=lineas, aprobar=True, motivo="no_entregada")
        self.pedido.refresh_from_db()
        self.guia.refresh_from_db()
        return comp

    def test_sin_existencias_la_reposicion_se_ve_en_cajas_y_se_cancela_desde_ahi(self):
        comp = self.aprobar_reposicion([(self.lb, 1, self.caja.pk)])
        self.assertEqual((self.pedido.estado, self.guia.sustituida_motivo), (Pedido.PENDIENTE, "no_entregada"))
        self.assertFalse(Paquete.objects.filter(pedido=self.pedido).exclude(pk=self.caja.pk).exists())  # sin stock: no nace caja
        url = reverse("mesa:pedido_detalle", args=[self.pedido.pk])
        html = self.client.get(url).content.decode()
        for esperado in ("<b>Reposición</b> · 1× B-SIX", "repone a la caja 1", self.inc.folio, "aprobada ",
                         "en espera de existencias (B-SIX)", 'value="cancelar_reposicion"', "contenido repuesto · no entregado · en espera de existencias"):
            self.assertIn(esperado, html)
        self.assertNotIn("sustituida ·", html)
        respuesta = self.client.post(url, {"accion": "cancelar_reposicion", "folio": self.pedido.folio, "compensacion_id": comp.pk}, follow=True)
        self.assertContains(respuesta, "Reposición cancelada (1× B-SIX (caja 1))")
        self.pedido.refresh_from_db()
        self.guia.refresh_from_db()
        self.lb.refresh_from_db()
        comp.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.guia.sustituida_motivo, self.lb.cantidad_repuesta, comp.estado),
                         (Pedido.EN_TRANSITO, "", 0, Compensacion.CANCELADA))
        html = respuesta.content.decode()
        self.assertNotIn("<b>Reposición</b>", html)
        self.assertNotIn("contenido repuesto", html)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(self.guia.pk), accion="guia_sustitucion_revertida").exists())
        detalle = self.client.get(reverse("mesa:incidencia_detalle", args=[self.inc.pk])).content.decode()
        self.assertIn("Cancelada", detalle)
        self.assertIn("Canceló la reposición (1× B-SIX (caja 1))", detalle)
        self.assertNotIn('value="compensacion_cancelar"', detalle)

    def test_con_existencias_la_caja_planeada_se_ve_y_la_incidencia_tambien_cancela(self):
        comp = self.aprobar_reposicion([(self.la, 1, self.caja.pk)])
        nueva = Paquete.objects.get(pedido=self.pedido, estado=Paquete.PLANEADO)
        html = self.client.get(reverse("mesa:pedido_detalle", args=[self.pedido.pk])).content.decode()
        self.assertIn(f"planeada en la caja {nueva.numero} (en bodega, sin picking)", html)
        self.assertIn("reposición de la caja 1", html)  # el renglón de la caja nueva
        self.assertIn(f"contenido repuesto · no entregado · por caja {nueva.numero}", html)
        detalle = self.client.get(reverse("mesa:incidencia_detalle", args=[self.inc.pk])).content.decode()
        self.assertIn('value="compensacion_cancelar"', detalle)
        respuesta = self.client.post(reverse("mesa:incidencia_detalle", args=[self.inc.pk]),
                                     {"accion": "compensacion_cancelar", "compensacion_id": comp.pk}, follow=True)
        self.assertContains(respuesta, "Reposición cancelada (1× A-SIX (caja 1))")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_TRANSITO)
        self.assertFalse(Paquete.objects.filter(pk=nueva.pk).exists())  # el plan de la reposición se tiró
        # Con el piso encima no hay botón ni acción.
        comp2 = self.aprobar_reposicion([(self.la, 1, self.caja.pk)])
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.EN_PICKING, asignado_a=self.mesa)
        html = self.client.get(reverse("mesa:pedido_detalle", args=[self.pedido.pk])).content.decode()
        self.assertIn("en manos del piso", html)
        self.assertNotIn('value="cancelar_reposicion"', html)
        respuesta = self.client.post(reverse("mesa:pedido_detalle", args=[self.pedido.pk]),
                                     {"accion": "cancelar_reposicion", "folio": self.pedido.folio, "compensacion_id": comp2.pk}, follow=True)
        self.assertContains(respuesta, "ya entró a picking")
