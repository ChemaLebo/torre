"""Posiciones desde Bodega (Chema 2026-10-13): renombrar una posición
conservando todo, mover existencias de una posición a otra (vendible más la
capa de apartados que ya no cabe en el origen) con kardex `traslado`, y el
desglose de lo que hay en una posición."""
from django.test import TestCase

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import (
    existencias_de_posicion, mover_existencias, recibir, renombrar_posicion, reservar, ubicar,
)


class PosicionesBodegaBase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="CCPL24L", codigo_barras="7500465010011", descripcion="Piedra Lisa 24")
        self.lote = Lote.objects.create(sku=self.sku, codigo="CPL333AB")
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.a = Ubicacion.objects.create(codigo="PIC-1-I-F-2", tipo=Ubicacion.PICKING)
        self.b = Ubicacion.objects.create(codigo="PIC-1-D-B-1", tipo=Ubicacion.PICKING)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.inactiva = Ubicacion.objects.create(codigo="PIC-9-9", tipo=Ubicacion.PICKING, activo=False)
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=10, lote_codigo=self.lote.codigo)
        recibir(linea, 10, 0, "mesa1")
        ubicar(self.sku, 10, self.a, self.lote, "mesa1")

    def saldo(self, ubicacion, estado):
        return sum(Saldo.objects.filter(sku=self.sku, ubicacion=ubicacion, estado=estado).values_list("cantidad", flat=True))


class MoverExistenciasTests(PosicionesBodegaBase):
    def test_mueve_vendible_y_la_capa_de_apartados_que_ya_no_cabe(self):
        self.assertTrue(reservar(self.sku, 4, "PED-00001"))
        r = mover_existencias(self.sku, self.lote, self.a, self.b, 8, actor="mesa1", motivo="Plan de acomodo")
        self.assertEqual(r, {"cantidad": 8, "apartadas": 2})
        self.assertEqual((self.saldo(self.a, Saldo.UBICADO_VENDIBLE), self.saldo(self.a, Saldo.RESERVADO)), (2, 2))
        self.assertEqual((self.saldo(self.b, Saldo.UBICADO_VENDIBLE), self.saldo(self.b, Saldo.RESERVADO)), (8, 2))
        mov = Movimiento.objects.get(tipo=Movimiento.TRASLADO)
        self.assertEqual((mov.delta, mov.referencia, mov.lote, mov.actor), (8, "PIC-1-I-F-2>PIC-1-D-B-1", self.lote, "mesa1"))
        evento = EventoAuditoria.objects.get(entidad="ubicacion", entidad_id="PIC-1-I-F-2", accion="existencias_movidas")
        self.assertEqual(evento.delta, {"sku": "CCPL24L", "lote": "CPL333AB", "cantidad": 8, "a": "PIC-1-D-B-1", "apartadas": 2})
        self.assertEqual(evento.motivo, "Plan de acomodo")
        # Mover lo que queda: todo se va, el origen queda limpio.
        mover_existencias(self.sku, self.lote, self.a, self.tarima, 2, actor="mesa1")
        self.assertFalse(Saldo.objects.filter(ubicacion=self.a).exists())
        self.assertEqual((self.saldo(self.tarima, Saldo.UBICADO_VENDIBLE), self.saldo(self.tarima, Saldo.RESERVADO)), (2, 2))

    def test_sin_apartados_de_sobra_la_capa_se_queda(self):
        reservar(self.sku, 3, "PED-00002")
        r = mover_existencias(self.sku, self.lote, self.a, self.b, 5)
        self.assertEqual(r["apartadas"], 0)
        self.assertEqual((self.saldo(self.a, Saldo.UBICADO_VENDIBLE), self.saldo(self.a, Saldo.RESERVADO)), (5, 3))
        self.assertEqual(self.saldo(self.b, Saldo.RESERVADO), 0)

    def test_errores_no_tocan_nada(self):
        with self.assertRaisesMessage(ValueError, "hay 10 pieza(s)"):
            mover_existencias(self.sku, self.lote, self.a, self.b, 11)
        with self.assertRaisesMessage(ValueError, "distinta"):
            mover_existencias(self.sku, self.lote, self.a, self.a, 1)
        with self.assertRaisesMessage(ValueError, "no es una posición de almacén activa"):
            mover_existencias(self.sku, self.lote, self.a, self.inactiva, 1)
        with self.assertRaisesMessage(ValueError, "mayor a cero"):
            mover_existencias(self.sku, self.lote, self.a, self.b, 0)
        self.assertEqual(self.saldo(self.a, Saldo.UBICADO_VENDIBLE), 10)
        self.assertFalse(Movimiento.objects.filter(tipo=Movimiento.TRASLADO).exists())


class RenombrarPosicionTests(PosicionesBodegaBase):
    def test_renombra_conservando_inventario_y_apartados(self):
        reservar(self.sku, 4, "PED-00003")
        anterior = renombrar_posicion(self.a, " a-3 ", actor="mesa1")
        self.assertEqual((anterior, self.a.codigo, self.a.tipo), ("PIC-1-I-F-2", "A-3", Ubicacion.PICKING))
        self.assertEqual((self.saldo(self.a, Saldo.UBICADO_VENDIBLE), self.saldo(self.a, Saldo.RESERVADO)), (10, 4))
        self.assertFalse(Ubicacion.objects.filter(codigo="PIC-1-I-F-2").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="ubicacion", entidad_id="PIC-1-I-F-2", accion="ubicacion_movida").exists())

    def test_rechaza_nombres_invalidos_repetidos_o_inactivos(self):
        for malo, texto in (("", "solo letras"), ("a 3", "solo letras"), ("X" * 21, "solo letras"),
                            ("PIC-1-D-B-1", "Ya existe"), ("pic-9-9", "Ya existe"), ("PIC-1-I-F-2", "ya se llama así")):
            with self.assertRaisesMessage(ValueError, texto):
                renombrar_posicion(self.a, malo)
        self.a.refresh_from_db()
        self.assertEqual(self.a.codigo, "PIC-1-I-F-2")


class ExistenciasDePosicionTests(PosicionesBodegaBase):
    def test_desglose_por_producto_y_lote(self):
        reservar(self.sku, 4, "PED-00004")
        otro = SKU.objects.create(cliente=self.cliente, codigo="CC24", descripcion="Colimita 24", requiere_lote=False)
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=otro, cantidad_anunciada=3, lote_codigo="")
        recibir(linea, 3, 0, "mesa1")
        ubicar(otro, 3, self.a, None, "mesa1")
        filas = existencias_de_posicion(self.a)
        self.assertEqual(
            [(f["sku"].codigo, f["lote"].codigo if f["lote"] else None, f["vendible"], f["apartado"]) for f in filas],
            [("CC24", None, 3, 0), ("CCPL24L", "CPL333AB", 10, 4)],
        )
        self.assertEqual(existencias_de_posicion(self.b), [])
