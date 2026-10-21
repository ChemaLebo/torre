"""Decisiones pieza por pieza (Chema 2026-10-22): producto en empaque que no va
a salir regresa a la posición y lote elegidos, a cuarentena o se da de baja
(con o sin descontar del kardex); producto que ya salió reingresa a bodega o
a cuarentena como retorno, o no regresa. Torre ejecuta exactamente lo que
Mesa decidió; nunca adivina."""
from decimal import Decimal

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import EventoAuditoria
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import (
    confirmar_pick, disponible, recibir, regresar_piezas_de_empaque, reingresar_piezas, reservar, ubicar,
)
from apps.piso.tests.base import PisoTestCase


class DecisionesPorPiezaTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        self.rack = Ubicacion.objects.create(codigo="A-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tarima = Ubicacion.objects.create(codigo="TAR-16", tipo=Ubicacion.RESERVA)
        self.corral = Ubicacion.objects.get(codigo="SAL-OTRO")
        self.con_lote = SKU.objects.create(
            cliente=self.cliente, codigo="C24OROL", codigo_barras="7509999999992", descripcion="Caja 24 Oro",
            peso_gr=9000, requiere_lote=True, precio_declarado=Decimal("500"),
        )
        self.lote = Lote.objects.create(sku=self.con_lote, codigo="CCYL75ABCD", fecha_caducidad="2027-05-25")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.con_lote, cantidad_anunciada=5, lote_codigo=self.lote.codigo)
        recibir(linea, 5, 0, self.operador)
        ubicar(self.con_lote, 5, self.rack, self.lote, self.operador)
        # Dos piezas en empaque (pickeadas del rack) y una en el corral sin lote (quitada de salida).
        self.assertTrue(reservar(self.con_lote, 2, "PED-A"))
        confirmar_pick(self.con_lote, 2, "PED-A")
        Saldo.objects.create(sku=self.con_lote, ubicacion=self.corral, lote=None, estado=Saldo.EN_EMPAQUE, cantidad=1)

    def _saldo(self, ubicacion, estado, lote=None):
        return sum(Saldo.objects.filter(sku=self.con_lote, ubicacion=ubicacion, estado=estado, lote=lote).values_list("cantidad", flat=True))

    def test_bodega_cuarentena_y_merma_desde_empaque(self):
        resultado = regresar_piezas_de_empaque(self.con_lote, [
            {"cantidad": 1, "destino": "bodega", "ubicacion": self.tarima, "lote": self.lote},
            {"cantidad": 1, "destino": "cuarentena", "lote": self.lote},
            {"cantidad": 1, "destino": "merma", "lote": self.lote, "descontar": True, "motivo": "se rompió"},
        ], "PED-A", self.operador, preferir=[(self.rack.pk, self.lote.pk, 2)])
        self.assertEqual(len(resultado), 3)
        self.assertEqual(self._saldo(self.tarima, Saldo.UBICADO_VENDIBLE, self.lote), 1)
        self.assertEqual(self._saldo(self.ubic_recepcion, Saldo.CUARENTENA, self.lote), 1)
        self.assertEqual(sum(Saldo.objects.filter(sku=self.con_lote, estado=Saldo.EN_EMPAQUE).values_list("cantidad", flat=True)), 0)
        tipos = list(Movimiento.objects.filter(sku=self.con_lote, referencia="PED-A").exclude(tipo__in=["reserva", "pick"]).values_list("tipo", "delta", "lote__codigo"))
        self.assertEqual(sorted(tipos), sorted([("putaway", 1, "CCYL75ABCD"), ("traslado", 1, "CCYL75ABCD"), ("merma", -1, "CCYL75ABCD")]))
        self.assertEqual(disponible(self.con_lote), 4)  # 3 en el rack + 1 en la tarima
        evento = EventoAuditoria.objects.get(entidad="sku", entidad_id=self.con_lote.codigo, accion="piezas_regresadas_de_empaque")
        self.assertEqual(evento.delta["decisiones"][0], {"cantidad": 1, "destino": "bodega", "ubicacion": "TAR-16", "lote": "CCYL75ABCD", "descontar": True})

    def test_merma_sin_descontar_no_mueve_nada(self):
        antes = Movimiento.objects.count()
        regresar_piezas_de_empaque(self.con_lote, [
            {"cantidad": 1, "destino": "merma", "descontar": False, "motivo": "la caja se perdió; el manifiesto ya la descontó"},
        ], "PED-A", self.operador)
        self.assertEqual(Movimiento.objects.count(), antes)
        self.assertEqual(sum(Saldo.objects.filter(sku=self.con_lote, estado=Saldo.EN_EMPAQUE).values_list("cantidad", flat=True)), 3)

    def test_validaciones(self):
        with self.assertRaisesMessage(ValueError, "elige la posición"):
            regresar_piezas_de_empaque(self.con_lote, [{"cantidad": 1, "destino": "bodega", "lote": self.lote}], "PED-A", self.operador)
        with self.assertRaisesMessage(ValueError, "lleva lote"):
            regresar_piezas_de_empaque(self.con_lote, [{"cantidad": 1, "destino": "bodega", "ubicacion": self.rack}], "PED-A", self.operador)
        with self.assertRaisesMessage(ValueError, "solo vive en picking o reserva"):
            regresar_piezas_de_empaque(self.con_lote, [{"cantidad": 1, "destino": "bodega", "ubicacion": self.corral, "lote": self.lote}], "PED-A", self.operador)
        with self.assertRaisesMessage(ValueError, "las decisiones descuentan 4"):
            regresar_piezas_de_empaque(self.con_lote, [{"cantidad": 4, "destino": "cuarentena", "lote": self.lote}], "PED-A", self.operador)
        with self.assertRaisesMessage(ValueError, "Elige a dónde va"):
            regresar_piezas_de_empaque(self.con_lote, [{"cantidad": 1, "destino": "otro"}], "PED-A", self.operador)
        otro_lote = Lote.objects.create(sku=self.sku, codigo="AJENO")
        with self.assertRaisesMessage(ValueError, "no corresponde al producto"):
            regresar_piezas_de_empaque(self.con_lote, [{"cantidad": 1, "destino": "cuarentena", "lote": otro_lote}], "PED-A", self.operador)

    def test_la_pieza_del_corral_sin_lote_regresa_con_el_lote_elegido(self):
        # La pieza quitada de salida vive en el corral sin lote; Mesa elige el lote al regresarla.
        regresar_piezas_de_empaque(self.con_lote, [
            {"cantidad": 1, "destino": "bodega", "ubicacion": self.rack, "lote": self.lote},
        ], "PED-B", self.operador, preferir=[(self.corral.pk, None, 1)])
        self.assertEqual(self._saldo(self.corral, Saldo.EN_EMPAQUE, None), 0)
        self.assertEqual(self._saldo(self.rack, Saldo.EN_EMPAQUE, self.lote), 2)
        self.assertEqual(self._saldo(self.rack, Saldo.UBICADO_VENDIBLE, self.lote), 4)

    def test_reingresar_piezas_que_ya_salieron(self):
        resultado = reingresar_piezas(self.con_lote, [
            {"cantidad": 1, "destino": "bodega", "ubicacion": self.rack, "lote": self.lote},
            {"cantidad": 1, "destino": "cuarentena", "lote": self.lote},
            {"cantidad": 1, "destino": "merma", "motivo": "se perdió en la calle"},
        ], "PED-C", self.operador)
        self.assertEqual([len(entrada) for _, entrada in resultado], [1, 1, 0])
        self.assertEqual(self._saldo(self.rack, Saldo.UBICADO_VENDIBLE, self.lote), 4)
        self.assertEqual(self._saldo(self.ubic_recepcion, Saldo.CUARENTENA, self.lote), 1)
        retornos = list(Movimiento.objects.filter(sku=self.con_lote, tipo=Movimiento.RETORNO, referencia="PED-C").values_list("delta", "estado_destino"))
        self.assertEqual(sorted(retornos), [(1, "cuarentena"), (1, "ubicado_vendible")])
        self.assertTrue(EventoAuditoria.objects.filter(entidad="sku", entidad_id=self.con_lote.codigo, accion="piezas_reingresadas").exists())
