"""Empacar resta lo anotado al escanear (lote por pedido, Chema 2026-10-22):
el físico sale de la posición y lote del registro del picking aunque entre un
lote más viejo al rack antes de pesar; si lo anotado no está en el registro
se resta como siempre y queda el evento; sin registros (pedidos anteriores)
empaca como siempre y los registros nacen con lo que salió."""
from decimal import Decimal

from django.urls import reverse

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.catalogo.services import asignar_producto
from apps.core.models import EventoAuditoria
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import recibir, reservar, ubicar
from apps.pedidos.models import LineaPedido, PickLinea
from apps.pedidos.services import confirmar_linea_pick, empacar, iniciar_picking

from .base import PisoTestCase


class EmpacarRestaLoAnotadoTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        self.login_piso()
        self.rack = Ubicacion.objects.create(codigo="A-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.vacia = Ubicacion.objects.create(codigo="TAR-02", tipo=Ubicacion.RESERVA)
        self.con_lote = SKU.objects.create(
            cliente=self.cliente, codigo="CAYACO-C24", codigo_barras="7509999999991", descripcion="Cayaco caja 24",
            peso_gr=9000, requiere_lote=True, precio_declarado=Decimal("500"),
        )
        self.viejo = Lote.objects.create(sku=self.con_lote, codigo="VIEJO", fecha_caducidad="2026-12-01")
        self.nuevo = Lote.objects.create(sku=self.con_lote, codigo="NUEVO", fecha_caducidad="2027-06-01")
        self.antiguo = Lote.objects.create(sku=self.con_lote, codigo="ANTIGUO", fecha_caducidad="2026-06-01")
        self._stock(self.tarima, self.viejo, 20)
        self._stock(self.rack, self.nuevo, 5)
        asignar_producto(self.rack, self.con_lote, actor="mesa1")

    def _stock(self, ubicacion, lote, cantidad):
        orden = OrdenEntrada.objects.create(cliente=self.con_lote.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.con_lote, cantidad_anunciada=cantidad, lote_codigo=lote.codigo)
        recibir(linea, cantidad, 0, self.operador)
        ubicar(self.con_lote, cantidad, ubicacion, lote, self.operador)

    def _pedido(self, cantidad=2):
        pedido = self.crear_pedido(cantidad=1, reservar_stock=False)
        pedido.lineas.all().delete()
        pedido.peso_esperado_gr = self.con_lote.peso_gr * cantidad
        pedido.save(update_fields=["peso_esperado_gr"])
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.con_lote, cantidad=cantidad)
        self.assertTrue(reservar(self.con_lote, cantidad, pedido.folio))
        linea.reservada = True
        linea.save(update_fields=["reservada"])
        iniciar_picking(pedido, self.operador)
        return pedido, linea

    def _escanear(self, pedido, lote, veces):
        url = reverse("piso:picking_pedido", args=[pedido.pk])
        for _ in range(veces):
            r = self.client.post(url, {"codigo": f"P|7509999999991|{lote}", "cantidad": "1"}, HTTP_ACCEPT="application/json")
            self.assertEqual(r.status_code, 200, r.content)

    def _saldo(self, ubicacion, estado, lote=None):
        return sum(Saldo.objects.filter(sku=self.con_lote, ubicacion=ubicacion, estado=estado, lote=lote).values_list("cantidad", flat=True))

    def test_empacar_resta_lo_anotado_aunque_llegue_un_lote_mas_viejo_al_rack(self):
        pedido, linea = self._pedido(2)
        self._escanear(pedido, "NUEVO", 2)
        # Entre el escaneo y la báscula llega un lote más viejo al rack: antes se restaba ese.
        self._stock(self.rack, self.antiguo, 3)
        empacar(pedido, self.operador, pedido.peso_esperado_gr, [self.foto("contenido.jpg")])
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 2)
        self.assertEqual(self._saldo(self.rack, Saldo.UBICADO_VENDIBLE, self.antiguo), 3)
        self.assertEqual(self._saldo(self.rack, Saldo.UBICADO_VENDIBLE, self.nuevo), 3)
        picks = list(linea.picks.all())
        self.assertEqual([(p.ubicacion.codigo, p.lote.codigo, p.cantidad, p.estado) for p in picks], [("A-3", "NUEVO", 2, PickLinea.EN_EMPAQUE)])
        self.assertIsNotNone(picks[0].ts_estado)
        # Kardex: una fila PICK por lote, con el lote.
        self.assertEqual(list(Movimiento.objects.filter(sku=self.con_lote, tipo=Movimiento.PICK).values_list("lote__codigo", "delta")), [("NUEVO", 2)])
        self.assertFalse(EventoAuditoria.objects.filter(accion="pick_fuera_de_registro").exists())

    def test_si_lo_anotado_no_esta_en_el_registro_resta_como_siempre_y_avisa(self):
        pedido, linea = self._pedido(1)
        # Se anotó una posición que en el registro no tiene la pieza (restock sin registrar).
        confirmar_linea_pick(linea, 1, self.operador, ubicacion=self.vacia, lote=self.nuevo)
        empacar(pedido, self.operador, pedido.peso_esperado_gr, [self.foto("contenido.jpg")])
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 1)
        self.assertEqual([(p.ubicacion.codigo, p.lote.codigo, p.estado) for p in linea.picks.all()], [("A-3", "NUEVO", PickLinea.EN_EMPAQUE)])
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="pick_fuera_de_registro")
        self.assertEqual(evento.delta["anotado"], [["TAR-02", "NUEVO", 1]])
        self.assertEqual(evento.delta["resto"], [["A-3", "NUEVO", 1]])
        self.assertIn("no estaba en el registro", evento.motivo)

    def test_sin_registros_empaca_como_siempre_y_los_registros_nacen_con_lo_que_salio(self):
        pedido, linea = self._pedido(2)
        confirmar_linea_pick(linea, 2, self.operador)  # pedido anterior al cambio: sin posición ni lote
        empacar(pedido, self.operador, pedido.peso_esperado_gr, [self.foto("contenido.jpg")])
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 2)
        self.assertEqual([(p.ubicacion.codigo, p.lote.codigo, p.cantidad) for p in linea.picks.all()], [("A-3", "NUEVO", 2)])
        self.assertFalse(EventoAuditoria.objects.filter(accion="pick_fuera_de_registro").exists())
