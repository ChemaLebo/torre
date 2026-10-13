"""Recepción por escaneo (Chema 2026-10-13): contar ≠ recibir.

Cada caja se escanea a una tarima y queda CONTADA sin stock; confirmar la
tarima la vuelve RECIBIDA (en_putaway en la tarima, dañadas a cuarentena);
cerrar la orden vuelve vendible todo lo recibido. Aquí se prueban los
servicios: resolver el escaneo (código de barras, código, QR de Torre,
distribución de teclado), contar, anular, confirmar, cuadre, cierre, reinicio,
tarimas con nombre y el editor de Mesa.
"""
from datetime import date
from unittest import mock

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import EscaneoRecepcion, LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import (
    agregar_linea_recepcion, ajustar_conteo, anular_escaneo, cerrar_recepcion, confirmar_tarima, contar_caja,
    crear_tarima, cuadre_recepcion, editar_linea_recepcion, marcar_danadas, mover_cajas, nombre_tarima_propuesto,
    normalizar_escaneo, qr_posicion, qr_producto, realidad_recepcion, reingresar_desde_pedido, reiniciar_recepcion,
    reservar, resolver_escaneo, resumen_lineas, tarimas_de_orden, confirmar_pick,
)

from .base import InventarioTestCase


class EscaneoBase(InventarioTestCase):
    """Cliente con dos productos de caja idéntica (24PCCLIPA / CC24LCAR, como en
    ASN-0007), otro cliente con su producto, una orden con lote anunciado y una tarima."""

    def setUp(self):
        super().setUp()
        self.sku.codigo_barras = "7501000000011"
        self.sku.requiere_lote = True
        self.sku.save()
        self.sku_b = SKU.objects.create(
            cliente=self.cliente, codigo="CC24LCAR", codigo_barras="7501000000028", descripcion="Colimita 24 lata",
            requiere_lote=True,
        )
        self.otro_cliente = Cliente.objects.create(nombre="Infinitea", slug="infinitea", buffer_stock=0)
        self.sku_ajeno = SKU.objects.create(
            cliente=self.otro_cliente, codigo="TE-VERDE", codigo_barras="7509999999999", descripcion="Té verde",
        )
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=1)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=10, lote_codigo="L1", fecha_caducidad=date(2027, 4, 22),
        )
        self.linea_b = LineaASN.objects.create(orden=self.orden, sku=self.sku_b, cantidad_anunciada=5, lote_codigo="B1")
        self.tarima = crear_tarima("piso1")

    def contar(self, n, sku=None, lote="L1", tarima=None, danada=False, actor="piso1"):
        return [
            contar_caja(self.orden, sku or self.sku, lote, None, tarima or self.tarima, actor, danada=danada)
            for _ in range(n)
        ]

    def eventos(self, accion):
        return EventoAuditoria.objects.filter(entidad="asn", entidad_id=self.orden.folio, accion=accion)

    def fila(self, sku=None):
        return next(f for f in resumen_lineas(self.orden) if f["sku"] == (sku or self.sku))


class NormalizarEscaneoTests(InventarioTestCase):
    def test_teclado_en_espanol_se_corrige_solo_en_codigos_de_torre(self):
        self.assertEqual(normalizar_escaneo("U]TAR'09"), "U|TAR-09")
        self.assertEqual(normalizar_escaneo("P]7503026716090]CPL330ABCD"), "P|7503026716090|CPL330ABCD")
        self.assertEqual(normalizar_escaneo("PIC'2'D'B'2"), "PIC-2-D-B-2")
        self.assertEqual(normalizar_escaneo("  7501234567890 "), "7501234567890")
        self.assertEqual(normalizar_escaneo("lote 'x]"), "lote 'x]")  # no parece código nuestro: tal cual

    def test_contenido_de_los_qr(self):
        self.assertEqual(qr_producto(self.sku, "L1"), f"P|{self.sku.codigo}|L1")  # sin código de barras: el código
        self.sku.codigo_barras = "7501000000011"
        self.assertEqual(qr_producto(self.sku, None), "P|7501000000011|")
        self.assertEqual(qr_posicion(self.ubic_picking), "U|A-01-1")


class ResolverEscaneoTests(EscaneoBase):
    def test_codigo_de_barras_y_codigo_del_sku_en_la_orden(self):
        r = resolver_escaneo(self.orden, "7501000000011")
        self.assertEqual((r["sku"], r["en_orden"], r["lote_qr"]), (self.sku, True, None))
        self.assertEqual([l.pk for l in r["lineas"]], [self.linea.pk])
        self.assertEqual(resolver_escaneo(self.orden, "colimita-six")["sku"], self.sku)

    def test_qr_de_torre_trae_el_lote(self):
        r = resolver_escaneo(self.orden, "P|7501000000011|L1")
        self.assertEqual((r["sku"], r["lote_qr"]), (self.sku, "L1"))
        r = resolver_escaneo(self.orden, "P]7501000000011]L1")  # teclado en español
        self.assertEqual(r["lote_qr"], "L1")

    def test_producto_del_cliente_fuera_de_la_orden_no_es_error(self):
        sku_c = SKU.objects.create(cliente=self.cliente, codigo="EXTRA", codigo_barras="7501000000035", descripcion="Extra")
        r = resolver_escaneo(self.orden, "7501000000035")
        self.assertEqual((r["sku"], r["en_orden"], r["lineas"]), (sku_c, False, []))

    def test_otro_cliente_no_registrado_posicion_y_kit_son_errores_distintos(self):
        with self.assertRaisesMessage(ValueError, "es un producto de Infinitea"):
            resolver_escaneo(self.orden, "7509999999999")
        with self.assertRaisesMessage(ValueError, "no está registrado"):
            resolver_escaneo(self.orden, "0000000000000")
        with self.assertRaisesMessage(ValueError, "etiqueta de posición"):
            resolver_escaneo(self.orden, "U|TAR-01")
        with self.assertRaisesMessage(ValueError, "Escanea un código"):
            resolver_escaneo(self.orden, "   ")
        kit = SKU.objects.create(cliente=self.cliente, codigo="KIT-1", codigo_barras="7501000000042", descripcion="Kit", es_kit=True)
        with self.assertRaisesMessage(ValueError, "es un kit"):
            resolver_escaneo(self.orden, kit.codigo_barras)


class ContarCajaTests(EscaneoBase):
    def test_contar_no_crea_stock_y_pasa_la_orden_a_en_recepcion(self):
        escaneo = self.contar(1)[0]
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.EN_RECEPCION)
        self.assertEqual((escaneo.linea, escaneo.tarima, escaneo.estado, escaneo.actor), (self.linea, self.tarima, "contada", "piso1"))
        self.assertEqual(Saldo.objects.count(), 0)
        self.assertEqual(Movimiento.objects.count(), 0)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertEqual(self.fila()["contadas"], 1)
        lote = Lote.objects.get(sku=self.sku, codigo="L1")
        self.assertEqual(lote.fecha_caducidad, date(2027, 4, 22))  # la caducidad de la línea

    def test_lote_nuevo_crea_linea_con_anunciado_cero_y_producto_nuevo_tambien(self):
        contar_caja(self.orden, self.sku, "L2", date(2027, 1, 1), self.tarima, "piso1")
        nueva = self.orden.lineas.get(sku=self.sku, lote_codigo="L2")
        self.assertEqual((nueva.cantidad_anunciada, nueva.fecha_caducidad), (0, date(2027, 1, 1)))
        self.assertEqual(self.eventos("lote_no_anunciado").count(), 1)
        sku_c = SKU.objects.create(cliente=self.cliente, codigo="EXTRA", descripcion="Extra", requiere_lote=False)
        contar_caja(self.orden, sku_c, "", None, self.tarima, "piso1")
        self.assertEqual(self.orden.lineas.get(sku=sku_c).cantidad_anunciada, 0)
        self.assertEqual(self.eventos("sku_no_anunciado").count(), 1)

    def test_validaciones(self):
        with self.assertRaisesMessage(ValueError, "requiere lote"):
            contar_caja(self.orden, self.sku, "", None, self.tarima, "piso1")
        with self.assertRaisesMessage(ValueError, "en qué tarima"):
            contar_caja(self.orden, self.sku, "L1", None, self.ubic_picking, "piso1")
        with self.assertRaisesMessage(ValueError, "no es de"):
            contar_caja(self.orden, self.sku_ajeno, "X", None, self.tarima, "piso1")
        self.orden.estado = OrdenEntrada.CERRADA
        self.orden.save()
        with self.assertRaisesMessage(ValueError, "ya está cerrada"):
            contar_caja(self.orden, self.sku, "L1", None, self.tarima, "piso1")

    def test_anular_resta_la_ultima_caja_de_esa_tarima(self):
        self.contar(3)
        anulado = anular_escaneo(self.orden, self.linea, self.tarima, "piso1")
        self.assertEqual(anulado.estado, EscaneoRecepcion.ANULADA)
        self.assertEqual(anulado.anulada_por, "piso1")
        self.assertEqual(self.fila()["contadas"], 2)
        self.assertEqual(self.eventos("escaneo_anulado").count(), 1)
        otra = crear_tarima("piso1")
        with self.assertRaisesMessage(ValueError, "No hay cajas"):
            anular_escaneo(self.orden, self.linea, otra, "piso1")


class ConfirmarTarimaTests(EscaneoBase):
    def test_confirmar_crea_el_stock_en_la_tarima_y_las_danadas_en_cuarentena(self):
        self.contar(4)
        self.contar(1, danada=True)
        self.contar(2, sku=self.sku_b, lote="B1")
        r = confirmar_tarima(self.orden, self.tarima, "piso1")
        self.assertEqual((r["cajas"], r["danadas"]), (6, 1))
        lote = Lote.objects.get(sku=self.sku, codigo="L1")
        en_tarima = Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY)
        self.assertEqual((en_tarima.ubicacion, en_tarima.lote, en_tarima.cantidad), (self.tarima, lote, 4))
        cuarentena = Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA)
        self.assertEqual((cuarentena.ubicacion, cuarentena.lote, cuarentena.cantidad), (self.ubic_recepcion, lote, 1))
        self.assertEqual(Saldo.objects.get(sku=self.sku_b, estado=Saldo.EN_PUTAWAY).cantidad, 2)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 0)  # nada vendible hasta cerrar
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (4, 1))
        self.assertEqual(EscaneoRecepcion.objects.filter(estado=EscaneoRecepcion.RECIBIDA).count(), 7)
        movs = Movimiento.objects.filter(tipo=Movimiento.RECEPCION, referencia=self.orden.folio)
        self.assertEqual(sorted((m.sku.codigo, m.delta, m.estado_destino) for m in movs),
                         [("CC24LCAR", 2, "en_putaway"), ("COLIMITA-SIX", 1, "cuarentena"), ("COLIMITA-SIX", 4, "en_putaway")])
        self.assertEqual(self.eventos("tarima_confirmada").count(), 1)
        tarimas = tarimas_de_orden(self.orden)
        self.assertEqual(len(tarimas), 1)
        self.assertEqual((tarimas[0]["cajas"], tarimas[0]["recibidas"], tarimas[0]["por_confirmar"], tarimas[0]["confirmada"]), (7, 7, 0, True))
        with self.assertRaisesMessage(ValueError, "No hay cajas por confirmar"):
            confirmar_tarima(self.orden, self.tarima, "piso1")

    def test_anular_una_recibida_revierte_su_stock(self):
        self.contar(2)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        anular_escaneo(self.orden, self.linea, self.tarima, "piso1", motivo="Sobró una etiqueta")
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 1)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 1)
        self.assertEqual(Movimiento.objects.filter(tipo=Movimiento.RECEPCION, delta=-1).count(), 1)

    def test_faltante_tras_confirmar_vuelve_a_dejar_la_tarima_por_confirmar(self):
        self.contar(2)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        self.contar(1)
        t = tarimas_de_orden(self.orden)[0]
        self.assertEqual((t["por_confirmar"], t["confirmada"]), (1, False))
        confirmar_tarima(self.orden, self.tarima, "piso1")
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 3)

    def test_reingreso_de_cancelacion_en_bodega_toma_el_stock_de_la_zona_de_recepcion(self):
        # La cancelación ya dejó 3 piezas en put-away en REC-01 sin tarima (ver
        # pedidos._devolver_stock_y_cancelar): al confirmar la tarima se mueven,
        # no se duplican.
        self.poner_vendible(5)
        self.assertTrue(reservar(self.sku, 3, "PED-1"))
        confirmar_pick(self.sku, 3, "PED-1")
        reingresar_desde_pedido(self.sku, 3, "PED-1", "mesa", desde_empaque=True)
        reingreso = OrdenEntrada.objects.create(cliente=self.cliente, tipo=OrdenEntrada.TIPO_REINGRESO, estado=OrdenEntrada.EN_RECEPCION)
        LineaASN.objects.create(orden=reingreso, sku=self.sku, cantidad_anunciada=3)
        self.sku.requiere_lote = False
        self.sku.save()
        for _ in range(3):
            contar_caja(reingreso, self.sku, "", None, self.tarima, "piso1")
        confirmar_tarima(reingreso, self.tarima, "piso1")
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 3)
        self.assertEqual(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY, ubicacion=self.ubic_recepcion).count(), 0)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).ubicacion, self.tarima)
        self.assertFalse(Movimiento.objects.filter(tipo=Movimiento.RECEPCION, referencia=reingreso.folio).exists())
        self.assertEqual(reingreso.lineas.get().cantidad_recibida, 3)
        cerrar_recepcion(reingreso, "piso1")
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 5)


class CuadreYCierreTests(EscaneoBase):
    def test_cuadre_detecta_espejo_entre_cajas_identicas(self):
        # Anunciadas 10 de A y 5 de B; llegaron 7 de A y 8 de B: 3 cajas de A
        # contadas como B (ASN-0007).
        self.contar(7)
        self.contar(8, sku=self.sku_b, lote="B1")
        cuadre = cuadre_recepcion(self.orden)
        self.assertEqual(cuadre["espejo"], [{"falta": "COLIMITA-SIX", "sobra": "CC24LCAR", "cajas": 3}])
        self.assertTrue(cuadre["con_diferencias"])
        self.assertEqual(len(cuadre["por_confirmar"]), 1)
        self.assertEqual(cuadre["sin_etiqueta"], 15)
        por_sku = {l["sku"].codigo: l for l in cuadre["lineas"]}
        self.assertEqual((por_sku["COLIMITA-SIX"]["faltante"], por_sku["CC24LCAR"]["sobrante"]), (3, 3))

    def test_cerrar_vuelve_vendible_avisa_una_vez_por_producto_y_abre_des_con_diferencias(self):
        self.contar(7)
        self.contar(5, sku=self.sku_b, lote="B1")
        confirmar_tarima(self.orden, self.tarima, "piso1")
        with mock.patch("apps.inventario.services._notificar_cambio_disponible") as aviso:
            cerrar_recepcion(self.orden, "piso1", tarimas_recibidas=1)
        self.assertEqual(sorted(c.args[0].codigo for c in aviso.call_args_list), ["CC24LCAR", "COLIMITA-SIX"])
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.tarimas_recibidas), (OrdenEntrada.CERRADA, 1))
        self.assertIsNotNone(self.orden.ts_vendible)
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 0)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 7)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE, sku=self.sku_b), 5)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).lote.codigo, "L1")
        evento = self.eventos("cerrada_con_discrepancia").get()
        self.assertEqual(evento.delta["discrepancias"], [{
            "sku": "COLIMITA-SIX", "anunciada": 10, "recibida": 7, "danada": 0, "faltante": 3, "sobrante": 0,
        }])

    def test_cerrar_exige_tarimas_confirmadas_y_nada_en_la_zona_de_recepcion(self):
        self.contar(2)
        with self.assertRaisesMessage(ValueError, "sin confirmar en TAR-01"):
            cerrar_recepcion(self.orden, "piso1")
        confirmar_tarima(self.orden, self.tarima, "piso1")
        Saldo.objects.create(sku=self.sku_b, ubicacion=self.ubic_recepcion, estado=Saldo.EN_PUTAWAY, cantidad=4)
        with self.assertRaisesMessage(ValueError, "zona de recepción sin tarima"):
            cerrar_recepcion(self.orden, "piso1")
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 0)

    def test_reiniciar_anula_todo_y_revierte_el_stock(self):
        self.contar(3)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        self.contar(2)
        r = reiniciar_recepcion(self.orden, "mesa")
        self.assertEqual((r["anuladas"], r["recibidas_revertidas"], r["no_encontradas"]), (5, 3, 0))
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 0)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertEqual(self.fila()["contadas"], 0)
        self.assertEqual(self.eventos("recepcion_reiniciada").count(), 1)


class TarimasTests(EscaneoBase):
    def test_nombre_propuesto_editable_y_unico(self):
        self.assertEqual(self.tarima.codigo, "TAR-01")
        self.assertEqual(nombre_tarima_propuesto(), "TAR-02")
        propia = crear_tarima("piso1", codigo="tar-roja")
        self.assertEqual((propia.codigo, propia.tipo, propia.activo), ("TAR-ROJA", Ubicacion.RESERVA, True))
        self.assertEqual(nombre_tarima_propuesto(), "TAR-02")  # los nombres sin número no corren la cuenta
        with self.assertRaisesMessage(ValueError, "Ya existe TAR-01"):
            crear_tarima("piso1", codigo="TAR-01")
        with self.assertRaisesMessage(ValueError, "empieza con TAR-"):
            crear_tarima("piso1", codigo="RES-5")
        self.assertEqual(crear_tarima("piso1").codigo, "TAR-02")


class EditorTests(EscaneoBase):
    def test_realidad_por_linea_y_tarima(self):
        self.contar(3)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        otra = crear_tarima("piso1")
        self.contar(2, tarima=otra)
        self.contar(1, tarima=otra, danada=True)
        fila = next(f for f in realidad_recepcion(self.orden) if f["sku"] == self.sku)
        self.assertEqual((fila["anunciadas"], fila["contadas"], fila["danadas"], fila["recibidas"], fila["por_confirmar"]), (10, 5, 1, 3, 3))
        self.assertEqual([(t["tarima"].codigo, t["contadas"], t["recibidas"], t["por_confirmar"], t["danadas"]) for t in fila["tarimas"]],
                         [("TAR-01", 3, 3, 0, 0), ("TAR-02", 2, 0, 3, 1)])
        self.assertEqual([(p["ubicacion"].codigo, p["cantidad"]) for p in fila["posiciones"]], [("TAR-01", 3)])
        self.assertEqual(fila["en_recepcion"], 3)

    def test_ajustar_conteo_sube_y_baja_con_stock(self):
        self.contar(2)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        self.assertEqual(ajustar_conteo(self.orden, self.linea, self.tarima, 5, 1, "mesa", "Faltaron por escanear"), (3, 1))
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 5)
        self.assertEqual(self.suma(Saldo.CUARENTENA), 1)
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (5, 1))
        self.assertEqual(ajustar_conteo(self.orden, self.linea, self.tarima, 4, 0, "mesa", "Se contó de más"), (-1, -1))
        self.assertEqual((self.suma(Saldo.EN_PUTAWAY), self.suma(Saldo.CUARENTENA)), (4, 0))
        self.assertEqual(EscaneoRecepcion.objects.filter(estado=EscaneoRecepcion.ANULADA).count(), 2)
        self.assertEqual(self.eventos("recepcion_corregida").count(), 2)
        with self.assertRaisesMessage(ValueError, "por qué"):
            ajustar_conteo(self.orden, self.linea, self.tarima, 1, 0, "mesa", "")

    def test_mover_y_marcar_danadas(self):
        self.contar(4)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        reserva = Ubicacion.objects.create(codigo="RES-1-D-F-1", tipo=Ubicacion.RESERVA)
        mover_cajas(self.orden, self.linea, self.tarima, reserva, 3, "mesa", "Pasaron al rack")
        self.assertEqual(Saldo.objects.get(sku=self.sku, ubicacion=reserva, estado=Saldo.EN_PUTAWAY).cantidad, 3)
        self.assertEqual(Saldo.objects.get(sku=self.sku, ubicacion=self.tarima, estado=Saldo.EN_PUTAWAY).cantidad, 1)
        fila = next(f for f in realidad_recepcion(self.orden) if f["sku"] == self.sku)
        self.assertEqual({t["tarima"].codigo: t["recibidas"] for t in fila["tarimas"]}, {"TAR-01": 1, "RES-1-D-F-1": 3})
        marcar_danadas(self.orden, self.linea, reserva, 2, "mesa", "Llegaron golpeadas")
        self.assertEqual(Saldo.objects.get(sku=self.sku, ubicacion=reserva, estado=Saldo.EN_PUTAWAY).cantidad, 1)
        self.assertEqual(self.suma(Saldo.CUARENTENA), 2)
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (2, 2))
        with self.assertRaisesMessage(ValueError, "solo hay 1"):
            mover_cajas(self.orden, self.linea, reserva, self.tarima, 2, "mesa", "x")
        cerrar_recepcion(self.orden, "mesa")
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 2)

    def test_editar_linea_y_agregar_producto_con_cajas(self):
        self.contar(1)
        confirmar_tarima(self.orden, self.tarima, "piso1")
        editar_linea_recepcion(self.orden, self.linea, "L1-BIS", date(2027, 5, 1), 12, "mesa", "Venía mal el lote")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.lote_codigo, self.linea.cantidad_anunciada), ("L1-BIS", 12))
        self.assertTrue(Lote.objects.filter(sku=self.sku, codigo="L1-BIS", fecha_caducidad=date(2027, 5, 1)).exists())
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).lote.codigo, "L1-BIS")  # mismo stock, renombrado
        sku_c = SKU.objects.create(cliente=self.cliente, codigo="EXTRA", descripcion="Extra", requiere_lote=False)
        linea = agregar_linea_recepcion(self.orden, sku_c, "", None, 0, "mesa", "Llegó sin anunciar", contadas=2, tarima=self.tarima)
        self.assertEqual((linea.cantidad_anunciada, linea.cantidad_recibida), (0, 2))
        self.assertEqual(self.suma(Saldo.EN_PUTAWAY, sku=sku_c), 2)
        with self.assertRaisesMessage(ValueError, "en qué tarima"):
            agregar_linea_recepcion(self.orden, self.sku_b, "B2", None, 0, "mesa", "x", contadas=1)
