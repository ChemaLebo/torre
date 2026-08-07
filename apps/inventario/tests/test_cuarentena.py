"""Dictamen de cuarentena: nada sale de cuarentena sin dos firmas.

Revendible regresa a put-away (y de ahí al flujo normal hasta vendible);
merma sale físico con Movimiento tipo merma y delta negativo.
"""
from datetime import date

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.inventario import services
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo

from .base import InventarioTestCase


class CuarentenaBase(InventarioTestCase):
    def setUp(self):
        super().setUp()
        self.piso1 = self.crear_firmante("piso1", PerfilUsuario.ROL_PISO, "1111")
        self.jefe = self.crear_firmante("jefe", PerfilUsuario.ROL_PISO, "2222")

    # ── Helpers ──

    def cuarentena_por_recepcion(self, danadas):
        """Cuarentena por la puerta oficial: ASN recibida con piezas dañadas."""
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=danadas)
        services.recibir(linea, 0, danadas, self.piso1)
        return orden

    def poner_cuarentena(self, cantidad, lote=None):
        """Siembra cuarentena directa (arrange, para escenarios con lote)."""
        return Saldo.objects.create(
            sku=self.sku, ubicacion=self.ubic_recepcion, lote=lote,
            estado=Saldo.CUARENTENA, cantidad=cantidad,
        )

    def dictaminar(self, cantidad, destino, lote=None, motivo_texto=""):
        return services.dictaminar_cuarentena(
            self.sku, cantidad, destino, "piso1", "1111", "jefe", "2222",
            self.piso1, lote=lote, motivo_texto=motivo_texto,
        )


class DictamenRevendibleTests(CuarentenaBase):
    def test_revendible_mueve_a_putaway_y_llega_a_vendible(self):
        self.cuarentena_por_recepcion(5)
        self.dictaminar(5, "revendible", motivo_texto="Caja mojada, producto intacto")

        self.assertEqual(self.suma(Saldo.CUARENTENA), 0)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 5)
        putaway = Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY)
        self.assertEqual(putaway.ubicacion_id, self.ubic_recepcion.pk)

        mov = Movimiento.objects.get(
            sku=self.sku, tipo=Movimiento.AJUSTE, referencia="DICTAMEN",
        )
        self.assertEqual(mov.delta, 5)
        self.assertEqual(mov.estado_origen, Saldo.CUARENTENA)
        self.assertEqual(mov.estado_destino, Saldo.EN_PUTAWAY)

        # Flujo completo: el put-away normal lo deja vendible otra vez.
        services.ubicar(self.sku, 5, self.ubic_picking, None, self.piso1)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 0)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 5)

    def test_revendible_parcial_deja_el_resto_en_cuarentena(self):
        self.cuarentena_por_recepcion(6)
        self.dictaminar(2, "revendible")
        self.assertEqual(self.suma(Saldo.CUARENTENA), 4)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 2)


class DictamenMermaTests(CuarentenaBase):
    def test_merma_emite_movimiento_merma_con_delta_negativo(self):
        self.cuarentena_por_recepcion(5)
        self.dictaminar(3, "merma", motivo_texto="Botellas estrelladas")

        self.assertEqual(self.suma(Saldo.CUARENTENA), 2)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 0)
        mov = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.MERMA)
        self.assertEqual(mov.delta, -3)
        self.assertEqual(mov.estado_origen, Saldo.CUARENTENA)
        self.assertEqual(mov.estado_destino, "")
        self.assertEqual(mov.referencia, "DICTAMEN")

    def test_fefo_consume_primero_el_lote_que_caduca_antes(self):
        lote_viejo = self.crear_lote("L-VIEJO", date(2026, 9, 1))
        lote_nuevo = self.crear_lote("L-NUEVO", date(2027, 3, 1))
        self.poner_cuarentena(4, lote=lote_nuevo)
        self.poner_cuarentena(4, lote=lote_viejo)

        self.dictaminar(5, "merma")

        restante = Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA)
        self.assertEqual(restante.cantidad, 3)
        self.assertEqual(restante.lote_id, lote_nuevo.pk)

    def test_dictamen_por_lote_solo_toca_ese_lote(self):
        lote_viejo = self.crear_lote("L-VIEJO", date(2026, 9, 1))
        lote_nuevo = self.crear_lote("L-NUEVO", date(2027, 3, 1))
        self.poner_cuarentena(4, lote=lote_viejo)
        self.poner_cuarentena(4, lote=lote_nuevo)

        self.dictaminar(4, "merma", lote=lote_nuevo)

        restante = Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA)
        self.assertEqual(restante.lote_id, lote_viejo.pk)
        self.assertEqual(restante.cantidad, 4)

        with self.assertRaisesMessage(ValueError, "del lote L-VIEJO"):
            self.dictaminar(5, "merma", lote=lote_viejo)


class DictamenValidacionesTests(CuarentenaBase):
    def test_pin_malo_no_toca_stock(self):
        self.cuarentena_por_recepcion(4)
        with self.assertRaisesMessage(ValueError, "PIN incorrecto"):
            services.dictaminar_cuarentena(
                self.sku, 2, "merma", "piso1", "9999", "jefe", "2222", self.piso1,
            )
        self.assertEqual(self.suma(Saldo.CUARENTENA), 4)
        self.assertFalse(Movimiento.objects.filter(tipo=Movimiento.MERMA).exists())

    def test_mismo_usuario_no_firma_dos_veces(self):
        self.cuarentena_por_recepcion(4)
        with self.assertRaisesMessage(ValueError, "dos personas distintas"):
            services.dictaminar_cuarentena(
                self.sku, 2, "merma", "piso1", "1111", "piso1", "1111", self.piso1,
            )
        self.assertEqual(self.suma(Saldo.CUARENTENA), 4)

    def test_rol_portal_no_firma(self):
        karina = self.crear_firmante("karina", PerfilUsuario.ROL_PORTAL, "4444")
        karina.perfil.cliente = self.cliente
        karina.perfil.save()
        self.cuarentena_por_recepcion(4)
        with self.assertRaisesMessage(ValueError, "no puede firmar"):
            services.dictaminar_cuarentena(
                self.sku, 2, "merma", "karina", "4444", "jefe", "2222", self.piso1,
            )
        self.assertEqual(self.suma(Saldo.CUARENTENA), 4)

    def test_cantidad_mayor_al_saldo(self):
        self.cuarentena_por_recepcion(3)
        with self.assertRaisesMessage(ValueError, "Solo hay 3 piezas"):
            self.dictaminar(5, "merma")
        self.assertEqual(self.suma(Saldo.CUARENTENA), 3)

    def test_cantidad_cero_o_negativa(self):
        self.cuarentena_por_recepcion(3)
        with self.assertRaises(ValueError):
            self.dictaminar(0, "merma")
        with self.assertRaises(ValueError):
            self.dictaminar(-2, "merma")
        self.assertEqual(self.suma(Saldo.CUARENTENA), 3)

    def test_destino_invalido(self):
        self.cuarentena_por_recepcion(3)
        with self.assertRaisesMessage(ValueError, "Destino inválido"):
            self.dictaminar(1, "regalar")
        self.assertEqual(self.suma(Saldo.CUARENTENA), 3)

    def test_lote_de_otro_sku_rechazado(self):
        from apps.catalogo.models import SKU
        otro = SKU.objects.create(
            cliente=self.cliente, codigo="OTRO-SIX", descripcion="Otro six",
            requiere_lote=False,
        )
        lote_ajeno = self.crear_lote("L-AJENO", sku=otro)
        self.cuarentena_por_recepcion(3)
        with self.assertRaisesMessage(ValueError, "no corresponde"):
            self.dictaminar(1, "merma", lote=lote_ajeno)
        self.assertEqual(self.suma(Saldo.CUARENTENA), 3)


class DictamenPorFilaTests(CuarentenaBase):
    """Con `ubicacion` el dictamen se acota a ESA fila: dictaminar la fila del
    retorno jamás descuenta las dañadas de la recepción (ni al revés)."""

    def setUp(self):
        super().setUp()
        # Fila A (pk menor): dañadas de ASN con lote, en REC-01.
        self.lote_rec = self.crear_lote("L-REC", date(2026, 9, 1))
        self.fila_a = Saldo.objects.create(
            sku=self.sku, ubicacion=self.ubic_recepcion, lote=self.lote_rec,
            estado=Saldo.CUARENTENA, cantidad=2,
        )
        # Fila B (pk mayor): retorno sin lote, en RET-01.
        self.fila_b = Saldo.objects.create(
            sku=self.sku, ubicacion=self.ubic_retorno, lote=None,
            estado=Saldo.CUARENTENA, cantidad=3,
        )

    def dictaminar_fila_b(self, cantidad, destino="merma"):
        return services.dictaminar_cuarentena(
            self.sku, cantidad, destino, "piso1", "1111", "jefe", "2222",
            self.piso1, lote=None, ubicacion=self.ubic_retorno,
        )

    def test_dictaminar_la_fila_b_solo_toca_la_fila_b(self):
        self.dictaminar_fila_b(3)
        self.fila_a.refresh_from_db()
        self.assertEqual(self.fila_a.cantidad, 2)  # intacta
        self.assertFalse(
            Saldo.objects.filter(
                sku=self.sku, estado=Saldo.CUARENTENA, ubicacion=self.ubic_retorno,
            ).exists()
        )

    def test_cantidad_mayor_a_la_fila_es_error_aunque_el_total_del_sku_alcance(self):
        # Total del SKU en cuarentena: 5; la fila B solo tiene 3.
        with self.assertRaisesMessage(ValueError, "Esa fila solo tiene 3 piezas en cuarentena."):
            self.dictaminar_fila_b(4)
        self.fila_a.refresh_from_db()
        self.fila_b.refresh_from_db()
        self.assertEqual(self.fila_a.cantidad, 2)
        self.assertEqual(self.fila_b.cantidad, 3)

    def test_sin_ubicacion_sigue_el_fefo_global(self):
        # Comportamiento actual sin fila explícita: FEFO por caducidad (A primero).
        self.dictaminar(2, "merma")
        self.fila_b.refresh_from_db()
        self.assertEqual(self.fila_b.cantidad, 3)
        self.assertFalse(Saldo.objects.filter(pk=self.fila_a.pk).exists())


class DictamenAuditoriaTests(CuarentenaBase):
    def test_evento_registrado_sin_pins_en_el_delta(self):
        self.cuarentena_por_recepcion(2)
        self.dictaminar(2, "merma", motivo_texto="Dañado total")

        evento = EventoAuditoria.objects.get(entidad="cuarentena", accion="dictamen_merma")
        self.assertEqual(evento.entidad_id, self.sku.codigo)
        self.assertEqual(evento.delta["cantidad"], 2)
        self.assertEqual(evento.delta["firmas"], ["piso1", "jefe"])
        self.assertEqual(evento.delta["motivo_texto"], "Dañado total")
        self.assertNotIn("1111", str(evento.delta))
        self.assertNotIn("2222", str(evento.delta))

    def test_evento_revendible(self):
        self.cuarentena_por_recepcion(2)
        self.dictaminar(2, "revendible")
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="cuarentena", accion="dictamen_revendible",
                entidad_id=self.sku.codigo,
            ).exists()
        )
