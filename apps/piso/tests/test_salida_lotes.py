"""Salida y regresos con lote por pedido (Chema 2026-10-22): el manifiesto
resta de empaque exactamente lo que viajó en las cajas que salen (no el lote
más viejo de todos los pedidos), el kardex lleva una fila SALIDA por lote,
los registros quedan con su caja, "Quitar de salida" regresa a la posición y
lote originales (no al corral sin lote), la cancelación en bodega manda el
lote al put-away, un pedido sin registros sale FEFO como siempre, y Mesa
muestra los lotes por línea."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.catalogo.services import asignar_producto
from apps.core.models import PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Paquete, PaqueteLinea
from apps.envios.services import cancelar_guia
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import recibir, reservar, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, PickLinea

from .base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class SalidaConLotePorPedidoTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        MockAdapter.reiniciar()
        self.login_piso()
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.rack = Ubicacion.objects.create(codigo="A-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.con_lote = SKU.objects.create(
            cliente=self.cliente, codigo="CAYACO-C24", codigo_barras="7509999999991", descripcion="Cayaco caja 24",
            peso_gr=9000, requiere_lote=True, precio_declarado=Decimal("500"),
        )
        self.viejo = Lote.objects.create(sku=self.con_lote, codigo="VIEJO", fecha_caducidad="2026-12-01")
        self.nuevo = Lote.objects.create(sku=self.con_lote, codigo="NUEVO", fecha_caducidad="2027-06-01")
        self._stock(self.tarima, self.viejo, 20)
        self._stock(self.rack, self.nuevo, 1)
        asignar_producto(self.rack, self.con_lote, actor="mesa1")

    def _stock(self, ubicacion, lote, cantidad):
        orden = OrdenEntrada.objects.create(cliente=self.con_lote.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.con_lote, cantidad_anunciada=cantidad, lote_codigo=lote.codigo)
        recibir(linea, cantidad, 0, self.operador)
        ubicar(self.con_lote, cantidad, ubicacion, lote, self.operador)

    def _saldo(self, ubicacion, estado, lote=None):
        return sum(Saldo.objects.filter(sku=self.con_lote, ubicacion=ubicacion, estado=estado, lote=lote).values_list("cantidad", flat=True))

    def _pedido_empacado(self, lote, escanear=True):
        """Pedido de 1 pieza del producto con lote, pickeado (por QR o directo) y empacado en una caja."""
        pedido = self.crear_pedido(cantidad=1, reservar_stock=False)
        pedido.lineas.all().delete()
        pedido.peso_esperado_gr = self.con_lote.peso_gr
        pedido.save(update_fields=["peso_esperado_gr"])
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.con_lote, cantidad=1)
        self.assertTrue(reservar(self.con_lote, 1, pedido.folio))
        linea.reservada = True
        linea.save(update_fields=["reservada"])
        services.iniciar_picking(pedido, self.operador)
        if escanear:
            r = self.client.post(reverse("piso:picking_pedido", args=[pedido.pk]), {"codigo": f"P|7509999999991|{lote}", "cantidad": "1"}, HTTP_ACCEPT="application/json")
            self.assertEqual(r.status_code, 200, r.content)
        else:
            services.confirmar_linea_pick(linea, 1, self.operador)
        services.empacar(pedido, self.operador, pedido.peso_esperado_gr, [self.foto("contenido.jpg")])
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("9"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1)
        services.generar_guia(pedido)
        Paquete.objects.filter(pedido=pedido).update(ts_cierre=timezone.now(), ts_etiquetas=timezone.now(), ts_listo_salida=timezone.now())
        pedido.refresh_from_db()
        return pedido, linea, Paquete.objects.get(pk=caja.pk)

    def _salir(self, pedido):
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()

    def test_el_manifiesto_resta_el_lote_que_viajo_en_la_caja_no_el_mas_viejo(self):
        # P1 se llevó NUEVO del rack; P2, con el rack vacío, se llevó VIEJO de la tarima (FEFO global).
        p1, l1, c1 = self._pedido_empacado("NUEVO")
        p2, l2, c2 = self._pedido_empacado("VIEJO")
        self.assertEqual((self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), self._saldo(self.tarima, Saldo.EN_EMPAQUE, self.viejo)), (1, 1))
        self._salir(p1)
        # Antes salía VIEJO (el más viejo en empaque); ahora sale lo que P1 pickeó: NUEVO.
        self.assertEqual((self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), self._saldo(self.tarima, Saldo.EN_EMPAQUE, self.viejo)), (0, 1))
        salida = Movimiento.objects.get(sku=self.con_lote, tipo=Movimiento.SALIDA, referencia=p1.folio)
        self.assertEqual((salida.lote.codigo, salida.delta), ("NUEVO", -1))
        pick = l1.picks.get()
        self.assertEqual((pick.estado, pick.caja_id, pick.ubicacion.codigo, pick.lote.codigo), (PickLinea.DESPACHADO, c1.pk, "A-3", "NUEVO"))
        l2.refresh_from_db()
        self.assertEqual(l2.picks.get().estado, PickLinea.EN_EMPAQUE)

    def test_quitar_de_salida_regresa_a_su_posicion_con_su_lote(self):
        p1, l1, c1 = self._pedido_empacado("NUEVO")
        self._salir(p1)
        cancelar_guia(c1.guia_activa, self.mesa, motivo="el carrier no la recogió")
        c1 = Paquete.objects.get(pk=c1.pk)
        with patch("apps.integraciones.services.cancelar_fulfillment_caja"), self.captureOnCommitCallbacks(execute=True):
            services.quitar_de_salida(p1, c1, self.mesa, motivo="nunca se fue")
        # Antes caía al corral sin lote; ahora vuelve a A-3 con NUEVO.
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 1)
        self.assertFalse(Saldo.objects.filter(sku=self.con_lote, ubicacion__tipo=Ubicacion.SALIDA, cantidad__gt=0).exists())
        retorno = Movimiento.objects.get(sku=self.con_lote, tipo=Movimiento.RETORNO)
        self.assertEqual((retorno.lote.codigo, retorno.delta), ("NUEVO", 1))
        self.assertEqual(l1.picks.get().estado, PickLinea.EN_EMPAQUE)
        # Y la siguiente salida vuelve a llevarse NUEVO.
        p1.refresh_from_db()
        services.generar_guia(p1)
        self._salir(Pedido_fresco(p1))
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 0)

    def test_cancelar_en_bodega_manda_el_lote_al_put_away(self):
        p2, l2, c2 = self._pedido_empacado("NUEVO")
        services.cancelar(p2, self.operador, motivo="ya no lo quiere")
        putaway = Saldo.objects.get(sku=self.con_lote, estado=Saldo.EN_PUTAWAY)
        self.assertEqual((putaway.ubicacion.codigo, putaway.lote.codigo, putaway.cantidad), ("REC-01", "NUEVO", 1))
        self.assertEqual(l2.picks.get().estado, PickLinea.REGRESADO)
        # El piso lo acomoda con su lote: ubicar prefiere la fila del mismo lote.
        ubicar(self.con_lote, 1, self.rack, self.nuevo, self.operador)
        self.assertEqual(self._saldo(self.rack, Saldo.UBICADO_VENDIBLE, self.nuevo), 1)

    def test_un_pedido_sin_registros_sale_fefo_como_siempre(self):
        p1, l1, c1 = self._pedido_empacado("NUEVO", escanear=False)
        # Sin escaneo no hay posición ni lote anotados: empacar los anota con lo que salió (rack, NUEVO).
        self.assertEqual([(p.ubicacion.codigo, p.lote.codigo) for p in l1.picks.all()], [("A-3", "NUEVO")])
        PickLinea.objects.filter(linea=l1).delete()  # pedido anterior al cambio
        self._salir(p1)
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.nuevo), 0)
        pick = l1.picks.get()
        self.assertEqual((pick.estado, pick.caja_id, pick.ubicacion.codigo, pick.lote.codigo, pick.cantidad), (PickLinea.DESPACHADO, c1.pk, "A-3", "NUEVO", 1))

    def test_mesa_muestra_los_lotes_por_linea(self):
        p1, l1, c1 = self._pedido_empacado("NUEVO")
        self.client.force_login(self.mesa)
        r = self.client.get(reverse("mesa:pedido_detalle", args=[p1.pk]))
        self.assertContains(r, "1 × A-3 · <span class=\"mono\">NUEVO</span> · en empaque")
        self._salir(p1)
        r = self.client.get(reverse("mesa:pedido_detalle", args=[p1.pk]))
        self.assertContains(r, "salió en la caja 1")


def Pedido_fresco(pedido):
    from apps.pedidos.models import Pedido
    return Pedido.objects.get(pk=pedido.pk)
