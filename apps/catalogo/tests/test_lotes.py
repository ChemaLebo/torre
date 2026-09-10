"""Servicios de lotes: alta y corrección auditadas, obtener_o_crear que completa
la caducidad, y las sugerencias (anunciado en la ASN → misma ASN → con stock →
reciente, con corte por antigüedad)."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.catalogo.services import (
    actualizar_caducidad, crear_lote, lotes_cliente, lotes_recientes_cliente,
    lotes_sugeridos, obtener_o_crear_lote,
)
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo


class BaseLotes(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.sku = SKU.objects.create(cliente=cls.cliente, codigo="PARAMO-SIX", descripcion="Páramo")
        cls.otro = SKU.objects.create(cliente=cls.cliente, codigo="TICUS-SIX", descripcion="Ticús")
        cls.ubic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        cls.usuario = get_user_model().objects.create_user(username="mesa1", password="x12345678")

    def envejecer(self, lote, dias):
        Lote.objects.filter(pk=lote.pk).update(creado=timezone.now() - timedelta(days=dias))
        lote.refresh_from_db()


class ObtenerOCrearTests(BaseLotes):
    def test_crea_y_luego_reusa_completando_caducidad(self):
        lote = obtener_o_crear_lote(self.sku, " L-1 ", None)
        self.assertEqual(lote.codigo, "L-1")
        self.assertIsNone(lote.fecha_caducidad)
        mismo = obtener_o_crear_lote(self.sku, "L-1", date(2027, 1, 1))
        self.assertEqual(mismo.pk, lote.pk)
        self.assertEqual(mismo.fecha_caducidad, date(2027, 1, 1))
        otra = obtener_o_crear_lote(self.sku, "L-1", date(2028, 1, 1))
        self.assertEqual(otra.fecha_caducidad, date(2027, 1, 1))

    def test_sin_codigo_es_error(self):
        with self.assertRaises(ValueError):
            obtener_o_crear_lote(self.sku, "  ")


class CrearYActualizarTests(BaseLotes):
    def test_alta_auditada_y_duplicado_rechazado(self):
        lote = crear_lote(self.sku, "L-2", date(2027, 5, 1), self.usuario)
        evento = EventoAuditoria.objects.get(entidad="lote", accion="alta")
        self.assertEqual(evento.entidad_id, "PARAMO-SIX/L-2")
        self.assertEqual(evento.delta["caducidad"], "2027-05-01")
        with self.assertRaisesMessage(ValueError, "ya existe"):
            crear_lote(self.sku, "L-2", None, self.usuario)
        self.assertEqual(Lote.objects.filter(sku=self.sku).count(), 1)
        self.assertIsNotNone(lote.creado)

    def test_actualizar_caducidad_audita_antes_y_despues(self):
        lote = crear_lote(self.sku, "L-3", None, self.usuario)
        actualizar_caducidad(lote, date(2027, 6, 1), self.usuario)
        evento = EventoAuditoria.objects.get(accion="caducidad_actualizada")
        self.assertEqual(evento.delta, {"antes": None, "ahora": "2027-06-01"})
        actualizar_caducidad(lote, date(2027, 6, 1), self.usuario)
        self.assertEqual(EventoAuditoria.objects.filter(accion="caducidad_actualizada").count(), 1)


class SugerenciasTests(BaseLotes):
    def test_orden_asn_stock_reciente_y_corte(self):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        LineaASN.objects.create(
            orden=orden, sku=self.sku, cantidad_anunciada=10, lote_codigo="L-ASN", fecha_caducidad=date(2027, 3, 1),
        )
        LineaASN.objects.create(orden=orden, sku=self.otro, cantidad_anunciada=5, lote_codigo="L-OTRO")
        con_stock = Lote.objects.create(sku=self.sku, codigo="L-STOCK", fecha_caducidad=date(2026, 12, 1))
        Saldo.objects.create(sku=self.sku, ubicacion=self.ubic, lote=con_stock, estado=Saldo.UBICADO_VENDIBLE, cantidad=4)
        Lote.objects.create(sku=self.sku, codigo="L-RECIENTE")
        viejo = Lote.objects.create(sku=self.sku, codigo="L-VIEJO")
        self.envejecer(viejo, 200)
        self.envejecer(con_stock, 300)

        sugeridos = lotes_sugeridos(self.sku, orden)
        self.assertEqual(
            [(s["codigo"], s["origen"]) for s in sugeridos],
            [("L-ASN", "asn"), ("L-OTRO", "orden"), ("L-STOCK", "stock"), ("L-RECIENTE", "reciente")],
        )
        self.assertEqual(sugeridos[0]["caducidad"], "2027-03-01")
        self.assertEqual(sugeridos[2]["caducidad"], "2026-12-01")
        self.assertNotIn("L-VIEJO", [s["codigo"] for s in sugeridos])

    def test_sin_orden_solo_stock_y_recientes(self):
        Lote.objects.create(sku=self.sku, codigo="L-1")
        self.assertEqual([s["origen"] for s in lotes_sugeridos(self.sku)], ["reciente"])

    def test_lote_creado_para_otro_sku_de_la_misma_orden_se_sugiere(self):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=1)
        LineaASN.objects.create(orden=orden, sku=self.otro, cantidad_anunciada=1)
        Lote.objects.create(sku=self.otro, codigo="L-TECLEADO")
        self.assertIn("L-TECLEADO", [s["codigo"] for s in lotes_sugeridos(self.sku, orden)])

    def test_lotes_recientes_y_lotes_cliente(self):
        a = Lote.objects.create(sku=self.sku, codigo="L-A")
        b = Lote.objects.create(sku=self.otro, codigo="L-B")
        self.envejecer(b, 400)
        Saldo.objects.create(sku=self.otro, ubicacion=self.ubic, lote=b, estado=Saldo.EN_PUTAWAY, cantidad=3)
        c = Lote.objects.create(sku=self.otro, codigo="L-C")
        self.envejecer(c, 400)
        self.assertEqual(set(lotes_recientes_cliente(self.cliente)), {"L-A", "L-B"})
        piezas = {(l.sku.codigo, l.codigo): l.piezas for l in lotes_cliente(self.cliente)}
        self.assertEqual(piezas, {("PARAMO-SIX", "L-A"): 0, ("TICUS-SIX", "L-B"): 3, ("TICUS-SIX", "L-C"): 0})
        self.assertIsNotNone(a.creado)
