"""Reconciliación de inventario por CSV: exportación, lectura, previa y aplicar
(todo o nada, doble firma una vez, Conteo por renglón, sin incidencias DES) y
el ajuste acotado a una ubicación."""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import EventoAuditoria
from apps.incidencias.models import Incidencia
from apps.inventario.models import Ajuste, Conteo, Movimiento, Saldo
from apps.inventario.services import (
    COLUMNAS_CSV_CONTEO, aplicar_ajuste, exportar_conteo, leer_csv_conteo,
    previa_reconciliacion, reconciliar_conteo, reservar,
)

from .base import InventarioTestCase


def csv_texto(*renglones, encabezado="codigo,descripcion,lote,caducidad,ubicacion,vendible_actual,contado"):
    return encabezado + "\n" + "\n".join(renglones) + "\n"


class BaseReconciliacion(InventarioTestCase):
    def setUp(self):
        super().setUp()
        self.ubic_reserva = Ubicacion.objects.create(codigo="RES-01", tipo=Ubicacion.RESERVA)
        self.sku_lote = SKU.objects.create(
            cliente=self.cliente, codigo="PARAMO-SIX", descripcion="Páramo six pack",
            peso_gr=2500, precio_declarado=Decimal("190.00"), requiere_lote=True,
        )
        self.kit = SKU.objects.create(
            cliente=self.cliente, codigo="TEABOX", descripcion="TeaBox", es_kit=True,
        )
        self.lote_a = self.crear_lote("L-A", date(2026, 12, 1), sku=self.sku_lote)
        self.poner_vendible(20)
        self.poner_vendible(7, sku=self.sku_lote, lote=self.lote_a)
        self.firma1 = self.crear_firmante("piso1", "piso", "1111")
        self.firma2 = self.crear_firmante("mesa1", "mesa", "3333")

    def aplicar(self, texto, **extra):
        filas, errores = leer_csv_conteo(texto)
        self.assertEqual(errores, [])
        return reconciliar_conteo(
            self.cliente, filas, Ajuste.MOTIVO_RECONCILIACION_INV, self.firma2, **extra,
        )


class ExportarConteoTests(BaseReconciliacion):
    def test_una_fila_por_anaquel_y_lote_con_contado_prellenado(self):
        filas = exportar_conteo(self.cliente)
        por = {(f["codigo"], f["lote"]): f for f in filas}
        self.assertEqual(por[("COLIMITA-SIX", "")]["contado"], 20)
        self.assertEqual(por[("COLIMITA-SIX", "")]["ubicacion"], "A-01-1")
        self.assertEqual(por[("PARAMO-SIX", "L-A")]["caducidad"], "2026-12-01")
        self.assertEqual(por[("PARAMO-SIX", "L-A")]["vendible_actual"], 7)
        self.assertNotIn(("TEABOX", ""), por)

    def test_sku_sin_stock_sale_con_fila_vacia(self):
        SKU.objects.create(cliente=self.cliente, codigo="NUEVO", descripcion="Sin stock")
        filas = [f for f in exportar_conteo(self.cliente) if f["codigo"] == "NUEVO"]
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["ubicacion"], "")
        self.assertEqual(filas[0]["contado"], 0)

    def test_columnas_del_contrato(self):
        self.assertEqual(
            COLUMNAS_CSV_CONTEO,
            ("codigo", "descripcion", "lote", "caducidad", "ubicacion", "vendible_actual", "contado"),
        )


class LeerCsvTests(TestCase):
    def test_lee_filas_e_ignora_vacias(self):
        filas, errores = leer_csv_conteo(csv_texto("A,,,,,,3", ",,,,,,", "B,x,,,,,"))
        self.assertEqual(errores, [])
        self.assertEqual([f["codigo"] for f in filas], ["A", "B"])
        self.assertEqual(filas[0]["numero"], 2)
        self.assertEqual(filas[1]["numero"], 4)
        self.assertEqual(filas[1]["contado"], "")

    def test_encabezados_con_espacios_y_columnas_minimas(self):
        filas, errores = leer_csv_conteo(" codigo , contado \nA,5\n")
        self.assertEqual(errores, [])
        self.assertEqual(filas[0]["contado"], "5")

    def test_sin_columnas_minimas_es_error_de_archivo(self):
        filas, errores = leer_csv_conteo("sku,piezas\nA,5\n")
        self.assertEqual(filas, [])
        self.assertIn("codigo y contado", errores[0])


class PreviaTests(BaseReconciliacion):
    def test_delta_contra_el_vendible_y_omitidos(self):
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,,,,,20,18", "PARAMO-SIX,,L-A,,,7,"))
        previa = previa_reconciliacion(self.cliente, filas)
        self.assertEqual(previa["errores"], [])
        r1, r2 = previa["renglones"]
        self.assertEqual((r1["vendible_actual"], r1["delta"]), (20, -2))
        self.assertTrue(r2["omitir"])
        self.assertEqual(len(previa["aplicables"]), 1)
        self.assertEqual(previa["omitidos"], 1)

    def test_errores_bloquean(self):
        filas, _ = leer_csv_conteo(csv_texto(
            "NOEXISTE,,,,,,1", "TEABOX,,,,,,1", "COLIMITA-SIX,,,,,,abc",
            "COLIMITA-SIX,,,,,,-1", "PARAMO-SIX,,,,,,3", "COLIMITA-SIX,,,,ZZZ-9,,3",
        ))
        previa = previa_reconciliacion(self.cliente, filas)
        textos = " ".join(previa["errores"])
        self.assertIn("SKU desconocido", textos)
        self.assertIn("es un kit", textos)
        self.assertIn("no es un número", textos)
        self.assertIn("no puede ser negativo", textos)
        self.assertIn("maneja lotes", textos)
        self.assertIn("no existe", textos)
        self.assertEqual(previa["aplicables"], [])

    def test_renglon_repetido_es_error(self):
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,,,,,,18", "COLIMITA-SIX,,,,,,19"))
        previa = previa_reconciliacion(self.cliente, filas)
        self.assertIn("repetido", previa["errores"][0])

    def test_avisos_nombre_distinto_stock_movido_y_apartado(self):
        self.assertTrue(reservar(self.sku, 15, "PED-00001"))
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,Otro nombre,,,,25,10"))
        previa = previa_reconciliacion(self.cliente, filas)
        avisos = " ".join(previa["avisos"])
        self.assertIn("no coincide con el catálogo", avisos)
        self.assertIn("se movió desde la exportación", avisos)
        self.assertIn("apartadas", avisos)
        self.assertEqual(previa["renglones"][0]["apartado"], 15)

    def test_stock_no_contado_se_lista(self):
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,,,,,,18"))
        previa = previa_reconciliacion(self.cliente, filas)
        self.assertEqual(
            [(n["codigo"], n["lote"]) for n in previa["no_contados"]], [("PARAMO-SIX", "L-A")],
        )

    def test_por_ubicacion_compara_solo_ese_anaquel(self):
        self.poner_vendible(5, ubicacion=self.ubic_reserva)
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,,,,res-01,,4", "COLIMITA-SIX,,,,A-01-1,,20"))
        previa = previa_reconciliacion(self.cliente, filas)
        self.assertEqual(previa["errores"], [])
        r_res, r_pic = previa["renglones"]
        self.assertEqual((r_res["vendible_actual"], r_res["delta"]), (5, -1))
        self.assertTrue(r_pic["omitir"])
        self.assertEqual([n["codigo"] for n in previa["no_contados"]], ["PARAMO-SIX"])


class ReconciliarTests(BaseReconciliacion):
    def test_aplica_ajustes_conteos_y_evento_sin_incidencias(self):
        resumen = self.aplicar(
            csv_texto("COLIMITA-SIX,,,,,20,18", "PARAMO-SIX,,L-A,,,7,9", "PARAMO-SIX,,L-B,2027-01-15,,0,4"),
            nota="Conteo tras pruebas", archivo="conteo.csv",
        )
        self.assertEqual(resumen["ajustes"], 3)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 18)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE, sku=self.sku_lote), 13)
        lote_b = Lote.objects.get(sku=self.sku_lote, codigo="L-B")
        self.assertEqual(lote_b.fecha_caducidad, date(2027, 1, 15))
        self.assertEqual(Saldo.objects.get(sku=self.sku_lote, lote=lote_b).cantidad, 4)
        ajustes = Ajuste.objects.order_by("folio")
        self.assertEqual([a.delta for a in ajustes], [-2, 2, 4])
        self.assertTrue(all(a.motivo == Ajuste.MOTIVO_RECONCILIACION_INV for a in ajustes))
        self.assertTrue(all(a.conteo_id for a in ajustes))
        self.assertEqual(Conteo.objects.count(), 3)
        conteo = Conteo.objects.get(sku=self.sku)
        self.assertEqual((conteo.esperado, conteo.contado, conteo.contador), (20, 18, "mesa1"))
        self.assertEqual(Movimiento.objects.filter(tipo=Movimiento.AJUSTE).count(), 3)
        self.assertEqual(Incidencia.objects.count(), 0)
        evento = EventoAuditoria.objects.get(accion="reconciliacion_csv")
        self.assertEqual(evento.delta["ajustes"], 3)
        self.assertEqual(evento.delta["archivo"], "conteo.csv")
        self.assertEqual(evento.motivo, "Conteo tras pruebas")

    def test_sin_cambios_ni_captura_no_toca_nada(self):
        with self.assertRaisesMessage(ValueError, "nada que aplicar"):
            self.aplicar(csv_texto("COLIMITA-SIX,,,,,,20", "PARAMO-SIX,,L-A,,,,"))
        self.assertEqual(Ajuste.objects.count(), 0)

    def test_errores_en_el_archivo_no_aplican_nada(self):
        with self.assertRaisesMessage(ValueError, "renglones con error"):
            self.aplicar(csv_texto("COLIMITA-SIX,,,,,,18", "NOEXISTE,,,,,,1"))
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 20)

    def test_solo_mesa_o_superusuario_aplica(self):
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,,,,,,18"))
        with self.assertRaisesMessage(ValueError, "la aplica Mesa de Control"):
            reconciliar_conteo(self.cliente, filas, Ajuste.MOTIVO_RECONCILIACION_INV, self.firma1)
        with self.assertRaisesMessage(ValueError, "la aplica Mesa de Control"):
            reconciliar_conteo(self.cliente, filas, Ajuste.MOTIVO_RECONCILIACION_INV, None)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 20)
        self.assertEqual(Conteo.objects.count(), 0)

    def test_la_firma_es_el_usuario_de_mesa_sin_doble_pin(self):
        self.aplicar(csv_texto("COLIMITA-SIX,,,,,,18"))
        ajuste = Ajuste.objects.get()
        self.assertEqual((ajuste.autorizo_1, ajuste.autorizo_2), ("mesa1", "mesa1"))
        self.assertEqual(Movimiento.objects.get(referencia=ajuste.folio).actor, "mesa1")
        evento = EventoAuditoria.objects.get(accion="reconciliacion_csv")
        self.assertEqual((evento.delta["firma"], evento.delta["doble_firma"]), ("mesa1", False))

    def test_todo_o_nada_cuando_un_renglon_falla_al_aplicar(self):
        self.poner_vendible(5, ubicacion=self.ubic_reserva)
        Ubicacion.objects.filter(pk=self.ubic_reserva.pk).update(activo=False)
        with self.assertRaises(ValueError):
            self.aplicar(csv_texto("COLIMITA-SIX,,,,A-01-1,,18", "COLIMITA-SIX,,,,RES-01,,4"))
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 25)
        self.assertEqual(Ajuste.objects.count(), 0)
        self.assertEqual(EventoAuditoria.objects.filter(accion="reconciliacion_csv").count(), 0)

    def test_motivo_fuera_del_catalogo(self):
        filas, _ = leer_csv_conteo(csv_texto("COLIMITA-SIX,,,,,,18"))
        with self.assertRaisesMessage(ValueError, "fuera del catálogo"):
            reconciliar_conteo(self.cliente, filas, "inventado", self.firma2)

    def test_por_ubicacion_agrega_y_quita_en_ese_anaquel(self):
        self.poner_vendible(5, ubicacion=self.ubic_reserva)
        self.aplicar(csv_texto("COLIMITA-SIX,,,,RES-01,5,9", "COLIMITA-SIX,,,,A-01-1,20,17"))
        self.assertEqual(Saldo.objects.get(sku=self.sku, ubicacion=self.ubic_reserva).cantidad, 9)
        self.assertEqual(Saldo.objects.get(sku=self.sku, ubicacion=self.ubic_picking).cantidad, 17)

    def test_sku_sin_stock_con_ubicacion_entra_ahi(self):
        nuevo = SKU.objects.create(cliente=self.cliente, codigo="NUEVO", descripcion="Nuevo", requiere_lote=False)
        self.aplicar(csv_texto("NUEVO,,,,RES-01,0,6"))
        self.assertEqual(Saldo.objects.get(sku=nuevo).ubicacion, self.ubic_reserva)

    def test_lote_existente_sin_caducidad_la_toma_del_archivo(self):
        lote = self.crear_lote("L-C", None, sku=self.sku_lote)
        self.poner_vendible(3, sku=self.sku_lote, lote=lote)
        self.aplicar(csv_texto("PARAMO-SIX,,L-C,2027-03-01,,3,5"))
        lote.refresh_from_db()
        self.assertEqual(lote.fecha_caducidad, date(2027, 3, 1))


class AjusteConUbicacionTests(BaseReconciliacion):
    def test_quita_solo_de_ese_anaquel(self):
        self.poner_vendible(5, ubicacion=self.ubic_reserva)
        with self.assertRaisesMessage(ValueError, "en RES-01"):
            aplicar_ajuste(self.sku, -6, Ajuste.MOTIVO_CONTEO, "piso1", "1111", "mesa1", "3333",
                           ubicacion=self.ubic_reserva)
        aplicar_ajuste(self.sku, -5, Ajuste.MOTIVO_CONTEO, "piso1", "1111", "mesa1", "3333",
                       ubicacion=self.ubic_reserva)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, ubicacion=self.ubic_reserva).exists())
        self.assertEqual(Saldo.objects.get(sku=self.sku, ubicacion=self.ubic_picking).cantidad, 20)

    def test_rechaza_ubicacion_que_no_es_anaquel(self):
        with self.assertRaisesMessage(ValueError, "no es un anaquel"):
            aplicar_ajuste(self.sku, 1, Ajuste.MOTIVO_CONTEO, "piso1", "1111", "mesa1", "3333",
                           ubicacion=self.ubic_recepcion)
