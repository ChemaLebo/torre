""""Parar en piso" desde Mesa (Chema 2026-10-20/21): el pedido sale de la cola
del piso como un DET con origen Mesa; por caja seleccionada se cancela la guía
y se sacan las confirmaciones del corral, o se desarma la caja y su contenido
vuelve a bodega a su posición y lote; un pedido parado no aparece en Salida."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Incidencia
from apps.inventario.models import Saldo
from apps.pedidos import services
from apps.pedidos.models import Pedido, PickLinea
from apps.piso.tests.base import PisoTestCase


def suma(sku, estado):
    return sum(Saldo.objects.filter(sku=sku, estado=estado).values_list("cantidad", flat=True))


@override_settings(ENVIA_API_KEY="")
class PararEnPisoTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=20)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def _url(self, pedido):
        return reverse("mesa:pedido_detalle", args=[pedido.pk])

    def _con_guia_en_corral(self):
        """Pedido con una caja empacada, guía comprada y los tres sellos (en el corral)."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        ahora = timezone.now()
        Paquete.objects.filter(pk=caja.pk).update(ts_etiquetas=ahora, ts_cierre=ahora, ts_listo_salida=ahora)
        Pedido.objects.filter(pk=pedido.pk).update(asignado_a=None)
        pedido.refresh_from_db()
        return pedido, Paquete.objects.get(pk=caja.pk), linea

    def test_en_cola_solo_se_detiene(self):
        pedido = self.crear_pedido(cantidad=1)
        html = self.client.get(self._url(pedido)).content.decode()
        self.assertIn('id="modal-parar"', html)
        self.assertIn('value="parar_en_piso"', html)
        respuesta = self.client.post(self._url(pedido), {"accion": "parar_en_piso", "folio": pedido.folio, "motivo": "Otro", "detalle": "el cliente pidió esperar"}, follow=True)
        self.assertContains(respuesta, "parado en piso (INC-")
        pedido.refresh_from_db()
        self.assertEqual((pedido.detenido, pedido.estado, pedido.asignado_a), (True, Pedido.PENDIENTE, None))
        det = Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_DET)
        self.assertTrue(det.mensajes.filter(texto__contains="el cliente pidió esperar").exists())
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="pedido_detenido")
        self.assertEqual(evento.delta["origen"], "mesa")
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="parado_en_piso").exists())
        # Ya detenido: el botón desaparece y un segundo intento avisa.
        self.assertNotIn('value="parar_en_piso"', self.client.get(self._url(pedido)).content.decode())
        respuesta = self.client.post(self._url(pedido), {"accion": "parar_en_piso", "folio": pedido.folio, "motivo": "Otro", "detalle": "x"}, follow=True)
        self.assertContains(respuesta, "ya está detenido")

    def test_en_picking_el_operador_lo_pierde_y_el_avance_se_conserva(self):
        pedido = self.crear_pedido(cantidad=2)
        services.iniciar_picking(pedido, self.operador)
        services.confirmar_linea_pick(pedido.lineas.get(), 1, self.operador, ubicacion=self.ubic_picking)
        self.client.post(self._url(pedido), {"accion": "parar_en_piso", "folio": pedido.folio, "motivo": "No encuentro el producto"}, follow=True)
        pedido.refresh_from_db()
        self.assertEqual((pedido.detenido, pedido.asignado_a, pedido.estado, pedido.lineas.get().cantidad_pickeada), (True, None, Pedido.EN_PICKING, 1))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="pedido_soltado").exists())

    def test_en_el_corral_cancela_la_guia_y_sale_de_salida(self):
        pedido, caja, linea = self._con_guia_en_corral()
        guia = caja.guia_activa
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        html = self.client.get(self._url(pedido)).content.decode()
        self.assertIn("en el corral, lista para el chofer", html)
        self.assertIn(f'name="caja-{caja.pk}-guia"', html)
        self.assertIn(f'name="caja-{caja.pk}-contenido"', html)
        self.assertIn("2 × COLIMITA-SIX → <span class=\"mono\">A-01-1</span>", html)
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(self._url(pedido), {
                "accion": "parar_en_piso", "folio": pedido.folio, "motivo": "Otro", "detalle": "dirección en duda", f"caja-{caja.pk}-guia": "1",
            }, follow=True)
        self.assertContains(respuesta, "parado en piso")
        pedido.refresh_from_db(); caja.refresh_from_db(); guia.refresh_from_db()
        self.assertEqual((guia.estado, caja.estado, pedido.estado, pedido.detenido), (Guia.CANCELADA, Paquete.EMPACADO, Pedido.EMPACADO, True))
        self.assertEqual((caja.ts_etiquetas, caja.ts_cierre, caja.ts_listo_salida), (None, None, None))
        self.assertEqual(suma(self.sku, Saldo.EN_EMPAQUE), 2)  # nada se movió: la caja sigue empacada
        # Salida no la lista.
        self.client.force_login(self.operador)
        self.assertNotContains(self.client.get(reverse("piso:salida")), pedido.folio)

    def test_desarmar_la_caja_regresa_su_contenido_a_bodega(self):
        pedido, caja, linea = self._con_guia_en_corral()
        self.assertEqual([(p.ubicacion.codigo, p.estado) for p in linea.picks.all()], [("A-01-1", PickLinea.EN_EMPAQUE)])
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), suma(self.sku, Saldo.UBICADO_VENDIBLE), suma(self.sku, Saldo.RESERVADO)), (2, 18, 0))
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(self._url(pedido), {
                "accion": "parar_en_piso", "folio": pedido.folio, "motivo": "Producto dañado", f"caja-{caja.pk}-contenido": "1",
            }, follow=True)
        self.assertContains(respuesta, "parado en piso")
        pedido.refresh_from_db(); caja.refresh_from_db(); linea.refresh_from_db()
        self.assertEqual((caja.estado, pedido.estado, pedido.detenido), (Paquete.PLANEADO, Pedido.EN_PICKING, True))
        self.assertIsNone(caja.guia_activa)  # la guía se canceló al desarmar
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), suma(self.sku, Saldo.UBICADO_VENDIBLE), suma(self.sku, Saldo.RESERVADO)), (0, 20, 2))
        self.assertEqual((linea.cantidad_pickeada, linea.cantidad_despachada), (0, 0))
        self.assertEqual(list(linea.picks.values_list("estado", flat=True)), [PickLinea.REGRESADO])
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="parado_en_piso")
        self.assertEqual(evento.delta["cajas_desarmadas"][0]["regreso"][0]["a"], [["A-01-1", None, 2]])
        # Al reanudar, el piso la vuelve a pickear: la línea pide 2 otra vez.
        services.reanudar_pedido(pedido, self.mesa)
        pedido.refresh_from_db()
        self.assertEqual((pedido.detenido, pedido.lineas.get().por_pickear), (False, 2))
