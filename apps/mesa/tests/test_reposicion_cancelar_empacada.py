"""Cancelar una reposición ya pickeada o empacada (Chema 2026-10-20, PED-00281):
botón por caja en el detalle del pedido, modal pieza por pieza (regresa a
bodega con posición y lote, cuarentena o no regresa), "Generar reingreso"
para decidir después, selector cuando hay dos reposiciones sobre la misma
caja original, y lo que queda después: caja cancelada en el expediente,
compensación cancelada, guía original de vuelta, contadores desde las cajas."""
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia, Paquete
from apps.incidencias.models import Compensacion, Incidencia
from apps.incidencias.services import abrir_incidencia, crear_compensacion
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import recibir, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido, PickLinea


def suma(sku, estado):
    return sum(Saldo.objects.filter(sku=sku, estado=estado).values_list("cantidad", flat=True))


@override_settings(ENVIA_API_KEY="", MEDIA_ROOT=tempfile.mkdtemp(prefix="torre-repo-emp-"))
class CancelarReposicionEmpacadaTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.a = SKU.objects.create(cliente=self.cliente, codigo="A-SIX", descripcion="Six A", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
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
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("6"), carrier="noventa9Minutos", estado=Paquete.DESPACHADO)
        from apps.envios.models import PaqueteLinea
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.la, cantidad=2)
        self.guia = Guia.objects.create(pedido=self.pedido, paquete=self.caja, carrier="noventa9Minutos", numero="1865484451",
                                        proveedor="envia", estado=Guia.EN_TRANSITO)
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="No llegó")
        self.url = reverse("mesa:pedido_detalle", args=[self.pedido.pk])

    def aprobar_reposicion(self, incidencia=None):
        with self.captureOnCommitCallbacks(execute=True):
            comp = crear_compensacion(incidencia or self.inc, "reposicion", self.mesa, "mesa", lineas=[(self.la, 1, self.caja.pk)], aprobar=True, motivo="no_entregada")
        self.pedido.refresh_from_db()
        return comp

    def pickear_y_empacar(self):
        """La reposición entra a picking y se empaca: la caja nueva queda EMPACADO en bodega, sin dueño."""
        self.pedido.refresh_from_db()
        services.iniciar_picking(self.pedido, self.mesa)
        self.la.refresh_from_db()
        services.confirmar_linea_pick(self.la, self.la.por_pickear, self.mesa, ubicacion=self.pic, lote=None)
        services.empacar(self.pedido, self.mesa, self.a.peso_gr, [SimpleUploadedFile("c.jpg", b"x", content_type="image/jpeg")])
        nueva = Paquete.objects.get(pedido=self.pedido, estado=Paquete.PLANEADO)
        nueva.transicionar(Paquete.EMPACADO, actor=self.mesa)
        Pedido.objects.filter(pk=self.pedido.pk).update(asignado_a=None)
        self.pedido.refresh_from_db()
        return nueva

    def test_boton_por_caja_modal_y_regreso_a_bodega(self):
        comp = self.aprobar_reposicion()
        nueva = self.pickear_y_empacar()
        self.assertEqual((suma(self.a, Saldo.EN_EMPAQUE), suma(self.a, Saldo.UBICADO_VENDIBLE)), (1, 9))
        html = self.client.get(self.url).content.decode()
        self.assertIn(f"modal-cancelar-reposicion-{nueva.pk}", html)
        self.assertIn('value="cancelar_reposicion_caja"', html)
        self.assertIn("En empaque, no descontada: tomada de 1 de A-01-1", html)
        self.assertIn(f"caja {nueva.numero} empacada, en bodega", html)
        self.assertNotIn('<select name="compensacion_id">', html)  # una sola reposición: sin selector
        respuesta = self.client.post(self.url, {
            "accion": "cancelar_reposicion_caja", "folio": self.pedido.folio, "caja": nueva.pk, "compensacion_id": comp.pk,
            f"pieza-{self.a.pk}-0-sku": self.a.pk, f"pieza-{self.a.pk}-0-cantidad": "1", f"pieza-{self.a.pk}-0-destino": "bodega",
            f"pieza-{self.a.pk}-0-ubicacion": "TAR-01", f"pieza-{self.a.pk}-0-lote": "", "motivo": "el carrier sí entregó la original",
        }, follow=True)
        self.assertContains(respuesta, "Reposición cancelada")
        nueva.refresh_from_db(); comp.refresh_from_db(); self.pedido.refresh_from_db(); self.la.refresh_from_db(); self.guia.refresh_from_db()
        self.assertEqual((nueva.estado, nueva.reingreso_estado), (Paquete.CANCELADO, Paquete.REINGRESADO))
        self.assertEqual((comp.estado, self.pedido.estado, self.guia.sustituida_motivo), (Compensacion.CANCELADA, Pedido.EN_TRANSITO, ""))
        self.assertEqual((self.la.cantidad_repuesta, self.la.cantidad_repuesta_reservada, self.la.cantidad_pickeada, self.la.cantidad_despachada), (0, 0, 2, 2))
        self.assertEqual((suma(self.a, Saldo.EN_EMPAQUE), suma(self.a, Saldo.UBICADO_VENDIBLE)), (0, 10))
        self.assertEqual(Saldo.objects.get(sku=self.a, ubicacion=self.tarima, estado=Saldo.UBICADO_VENDIBLE).cantidad, 1)
        self.assertTrue(Movimiento.objects.filter(sku=self.a, tipo=Movimiento.PUTAWAY, referencia=self.pedido.folio).exists())
        self.assertEqual(list(PickLinea.objects.filter(linea=self.la).values_list("estado", flat=True)), [PickLinea.REGRESADO])
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(self.pedido.pk), accion="reposicion_cancelada")
        self.assertEqual((evento.delta["desde"], evento.delta["caja"]), ("empacada", nueva.numero))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk), accion="reingreso_resuelto").exists())
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('value="cancelar_reposicion_caja"', html)
        self.assertIn("Cancelada", html)
        detalle = self.client.get(reverse("mesa:incidencia_detalle", args=[self.inc.pk])).content.decode()
        self.assertIn("Canceló la reposición de la caja", detalle)

    def test_generar_reingreso_deja_la_caja_por_decidir(self):
        comp = self.aprobar_reposicion()
        nueva = self.pickear_y_empacar()
        respuesta = self.client.post(self.url, {
            "accion": "cancelar_reposicion_caja", "folio": self.pedido.folio, "caja": nueva.pk, "compensacion_id": comp.pk,
            "generar_reingreso": "1", "motivo": "la decido mañana",
        }, follow=True)
        self.assertContains(respuesta, "queda en Reingresos por decidir")
        nueva.refresh_from_db(); comp.refresh_from_db()
        self.assertEqual((nueva.estado, nueva.reingreso_estado, comp.estado), (Paquete.CANCELADO, "", Compensacion.CANCELADA))
        self.assertEqual(suma(self.a, Saldo.EN_EMPAQUE), 1)  # nada se movió todavía
        self.assertIn(self.pedido.pk, [p.pk for p in services.reingresos_por_decidir()])
        self.assertEqual([c.pk for c in services.cajas_por_reingresar(self.pedido)], [nueva.pk])
        # Después se resuelve pieza por pieza: no regresa, y se descuenta del kardex.
        services.resolver_contenido_caja(self.pedido, nueva, [
            {"sku": self.a, "cantidad": 1, "destino": "merma", "descontar": True, "motivo": "se rompió"},
        ], self.mesa, motivo="se rompió")
        nueva.refresh_from_db()
        self.assertEqual((nueva.reingreso_estado, suma(self.a, Saldo.EN_EMPAQUE)), (Paquete.NO_RECUPERADO, 0))
        self.assertTrue(Movimiento.objects.filter(sku=self.a, tipo=Movimiento.MERMA, delta=-1).exists())
        self.assertEqual(services.cajas_por_reingresar(self.pedido), [])

    def test_con_dos_reposiciones_sobre_la_misma_caja_el_modal_pregunta_cual(self):
        comp1 = self.aprobar_reposicion()
        inc2 = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="Tampoco llegó")
        comp2 = self.aprobar_reposicion(incidencia=inc2)
        nueva = self.pickear_y_empacar()
        html = self.client.get(self.url).content.decode()
        self.assertIn('<select name="compensacion_id">', html)
        self.assertIn(f'<option value="{comp2.pk}">{inc2.folio}', html)  # la más reciente primero
        self.assertIn("no hay forma de saber cuál creó esta caja", html)
        # El planeador juntó las dos reposiciones en una caja de 2 piezas: cancelar la caja cancela las dos
        # (la más reciente primero) y la guía original vuelve a mandar.
        self.assertEqual(sum(pl.cantidad for pl in nueva.lineas.all()), 2)
        respuesta = self.client.post(self.url, {
            "accion": "cancelar_reposicion_caja", "folio": self.pedido.folio, "caja": nueva.pk,
            f"pieza-{self.a.pk}-0-sku": self.a.pk, f"pieza-{self.a.pk}-0-cantidad": "2", f"pieza-{self.a.pk}-0-destino": "merma",
            f"pieza-{self.a.pk}-0-descontar": "1", "motivo": "sobró",
        }, follow=True)
        self.assertContains(respuesta, "Reposición cancelada")
        comp1.refresh_from_db(); comp2.refresh_from_db(); self.guia.refresh_from_db(); self.la.refresh_from_db()
        self.assertEqual((comp1.estado, comp2.estado, self.guia.sustituida_motivo), (Compensacion.CANCELADA, Compensacion.CANCELADA, ""))
        self.assertEqual((self.la.cantidad_repuesta, suma(self.a, Saldo.EN_EMPAQUE)), (0, 0))
        self.assertTrue(Movimiento.objects.filter(sku=self.a, tipo=Movimiento.MERMA, delta=-2).exists())

    def test_con_el_piso_encima_no_se_cancela(self):
        comp = self.aprobar_reposicion()
        nueva = self.pickear_y_empacar()
        Pedido.objects.filter(pk=self.pedido.pk).update(asignado_a=self.mesa)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('value="cancelar_reposicion_caja"', html)
        respuesta = self.client.post(self.url, {
            "accion": "cancelar_reposicion_caja", "folio": self.pedido.folio, "caja": nueva.pk, "compensacion_id": comp.pk,
            "generar_reingreso": "1", "motivo": "x",
        }, follow=True)
        self.assertContains(respuesta, "en manos del piso")
        nueva.refresh_from_db()
        self.assertEqual(nueva.estado, Paquete.EMPACADO)
