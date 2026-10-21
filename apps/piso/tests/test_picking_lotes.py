"""Lote por pedido (Chema 2026-10-22): cada escaneo del picking anota de qué
posición y de qué lote salió la pieza (pedidos.PickLinea). Los productos con
lote exigen el QR de la etiqueta; los que no llevan lote anotan solo la
posición; un picking reiniciado borra sus registros; un pick sin escaneo
(servicio directo, hija de kit) queda "sin registro"."""
from decimal import Decimal

from django.urls import reverse

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.catalogo.services import asignar_producto
from apps.core.models import EventoAuditoria
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.inventario.services import recibir, reservar, ubicar
from apps.pedidos.models import LineaPedido, Pedido, PickLinea
from apps.pedidos.services import confirmar_linea_pick, iniciar_picking, reiniciar_picking

from .base import PisoTestCase


class PickingAnotaPosicionYLoteTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        self.login_piso()
        self.rack = Ubicacion.objects.create(codigo="A-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.con_lote = SKU.objects.create(
            cliente=self.cliente, codigo="CAYACO-C24", codigo_barras="7509999999991", descripcion="Cayaco caja 24",
            peso_gr=9000, requiere_lote=True, precio_declarado=Decimal("500"),
        )
        self.viejo = Lote.objects.create(sku=self.con_lote, codigo="VIEJO", fecha_caducidad="2026-12-01")
        self.nuevo = Lote.objects.create(sku=self.con_lote, codigo="NUEVO", fecha_caducidad="2027-06-01")
        self._stock(self.con_lote, self.tarima, self.viejo, 20)
        self._stock(self.con_lote, self.rack, self.nuevo, 5)
        asignar_producto(self.rack, self.con_lote, actor="mesa1")
        self.crear_stock(cantidad=10)  # self.sku (sin lote) en A-01-1

    def _stock(self, sku, ubicacion, lote, cantidad):
        orden = OrdenEntrada.objects.create(cliente=sku.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=cantidad, lote_codigo=lote.codigo)
        recibir(linea, cantidad, 0, self.operador)
        ubicar(sku, cantidad, ubicacion, lote, self.operador)

    def _pedido_con_lote(self, cantidad=2):
        pedido = self.crear_pedido(cantidad=1, reservar_stock=False)
        pedido.lineas.all().delete()
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.con_lote, cantidad=cantidad)
        self.assertTrue(reservar(self.con_lote, cantidad, pedido.folio))
        linea.reservada = True
        linea.save(update_fields=["reservada"])
        iniciar_picking(pedido, self.operador)
        return pedido, linea

    def test_el_qr_anota_la_posicion_del_rack_y_el_lote(self):
        pedido, linea = self._pedido_con_lote()
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        r = self.client.post(url, {"codigo": "P|7509999999991|NUEVO", "cantidad": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual((r.status_code, r.json()["pickeada"]), (200, 1))
        pick = PickLinea.objects.get(linea=linea)
        self.assertEqual((pick.ubicacion.codigo, pick.lote.codigo, pick.cantidad, pick.estado), ("A-3", "NUEVO", 1, PickLinea.PICKEADO))
        evento = EventoAuditoria.objects.filter(entidad="linea_pedido", entidad_id=str(linea.pk), accion="pick").latest("pk")
        self.assertEqual((evento.delta["posicion"], evento.delta["lote"]), ("A-3", "NUEVO"))
        # Segundo escaneo: otro renglón; la suma de los registros es lo pickeado.
        self.client.post(url, {"codigo": "P|7509999999991|NUEVO", "cantidad": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(sum(p.cantidad for p in linea.picks.all()), 2)

    def test_un_producto_con_lote_exige_el_qr(self):
        pedido, linea = self._pedido_con_lote()
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        r = self.client.post(url, {"codigo": "7509999999991", "cantidad": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("lleva lote: escanea el QR de la etiqueta", r.json()["error"])
        linea.refresh_from_db()
        self.assertEqual((linea.cantidad_pickeada, PickLinea.objects.count()), (0, 0))
        # El fallback sin JS muestra el mismo aviso.
        r = self.client.post(url, {"codigo": "7509999999991", "cantidad": "1"}, follow=True)
        self.assertContains(r, "lleva lote: escanea el QR")

    def test_un_producto_sin_lote_anota_solo_la_posicion(self):
        pedido = self.crear_pedido(cantidad=2)
        iniciar_picking(pedido, self.operador)
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        r = self.client.post(url, {"codigo": "7501234567890", "cantidad": "2"}, HTTP_ACCEPT="application/json")
        self.assertEqual((r.status_code, r.json()["pickeada"]), (200, 2))
        pick = PickLinea.objects.get(linea__pedido=pedido)
        self.assertEqual((pick.ubicacion.codigo, pick.lote, pick.cantidad), ("A-01-1", None, 2))

    def test_reiniciar_el_picking_borra_los_registros(self):
        pedido, linea = self._pedido_con_lote()
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        self.client.post(url, {"codigo": "P|7509999999991|NUEVO", "cantidad": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(PickLinea.objects.filter(linea=linea).count(), 1)
        self.assertTrue(reiniciar_picking(pedido, self.operador, motivo="cambió de manos"))
        linea.refresh_from_db()
        self.assertEqual((linea.cantidad_pickeada, PickLinea.objects.filter(linea=linea).count()), (0, 0))

    def test_un_pick_sin_escaneo_queda_sin_registro(self):
        pedido, linea = self._pedido_con_lote()
        confirmar_linea_pick(linea, 2, self.operador)
        pick = PickLinea.objects.get(linea=linea)
        self.assertEqual((pick.ubicacion, pick.lote, pick.cantidad), (None, None, 2))
        self.assertIn("sin posición", str(pick))
