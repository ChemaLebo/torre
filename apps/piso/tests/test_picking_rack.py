"""Racks con producto fijo en el picking (Chema 2026-10-13): la ruta manda al
rack del producto aunque un lote más viejo viva en tarima, el físico del
pick sale del rack, y cuando el rack no alcanza la pantalla pide restock."""
from django.urls import reverse

from apps.catalogo.models import Lote, Ubicacion
from apps.catalogo.services import asignar_producto
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo
from apps.inventario.services import confirmar_pick, recibir, reservar, ubicar
from apps.pedidos.services import iniciar_picking

from .base import PisoTestCase


class PickingConRackFijoTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        self.login_piso()
        self.rack = Ubicacion.objects.create(codigo="A-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.viejo = Lote.objects.create(sku=self.sku, codigo="VIEJO", fecha_caducidad="2026-12-01")
        self.nuevo = Lote.objects.create(sku=self.sku, codigo="NUEVO", fecha_caducidad="2027-06-01")
        self.stock(self.tarima, self.viejo, 20)
        self.stock(self.rack, self.nuevo, 2)
        asignar_producto(self.rack, self.sku, actor="mesa1")

    def stock(self, ubicacion, lote, cantidad):
        orden = OrdenEntrada.objects.create(cliente=self.sku.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=cantidad, lote_codigo=lote.codigo)
        recibir(linea, cantidad, 0, self.operador)
        ubicar(self.sku, cantidad, ubicacion, lote, self.operador)

    def saldo(self, ubicacion, estado, lote=None):
        qs = Saldo.objects.filter(sku=self.sku, ubicacion=ubicacion, estado=estado)
        if lote is not None:
            qs = qs.filter(lote=lote)
        return sum(qs.values_list("cantidad", flat=True))

    def test_la_ruta_manda_al_rack_y_pide_restock_si_no_alcanza(self):
        pedido = self.crear_pedido(cantidad=5)
        iniciar_picking(pedido, self.operador)
        r = self.client.get(reverse("piso:picking_pedido", args=[pedido.pk]))
        linea = r.context["lineas"][0]
        self.assertEqual((linea.rack.codigo, linea.primera_ubicacion.ubicacion.codigo, linea.en_rack, linea.restock_falta), ("A-3", "A-3", 2, 3))
        self.assertEqual((linea.restock_origen.ubicacion.codigo, linea.restock_origen.lote.codigo), ("TAR-01", "VIEJO"))
        self.assertContains(r, "Rack fijo")
        self.assertContains(r, "Restock: en A-3 hay 2 y faltan 3. Trae del lote VIEJO en TAR-01 (20 ahí).")

    def test_el_pick_fisico_sale_del_rack_aunque_la_reserva_cayera_en_la_tarima(self):
        self.assertTrue(reservar(self.sku, 2, "PED-X"))
        # La capa FEFO cayó en la tarima (lote VIEJO); el físico debe salir del rack.
        self.assertEqual(self.saldo(self.tarima, Saldo.RESERVADO), 2)
        confirmar_pick(self.sku, 2, "PED-X")
        self.assertEqual(self.saldo(self.rack, Saldo.UBICADO_VENDIBLE), 0)
        self.assertEqual(self.saldo(self.tarima, Saldo.UBICADO_VENDIBLE), 20)
        self.assertEqual(self.saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 2)
        self.assertEqual(self.saldo(self.tarima, Saldo.RESERVADO), 0)
        # Si el rack se agota, sigue la capa y luego FEFO en toda la bodega.
        reservar(self.sku, 3, "PED-Y")
        confirmar_pick(self.sku, 3, "PED-Y")
        self.assertEqual(self.saldo(self.tarima, Saldo.UBICADO_VENDIBLE), 17)
        self.assertEqual(self.saldo(self.tarima, Saldo.EN_EMPAQUE, self.viejo), 3)
