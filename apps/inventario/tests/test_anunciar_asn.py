"""anunciar_asn: alta de ASN compartida por Mesa y portal, con lote por línea."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.models import OrdenEntrada
from apps.inventario.services import anunciar_asn


class AnunciarAsnTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.ajeno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="COLIMITA-SIX", descripcion="Colimita")
        self.sku_ajeno = SKU.objects.create(cliente=self.ajeno, codigo="MEZCAL", descripcion="Mezcal")
        self.usuario = get_user_model().objects.create_user(username="mesa1", password="x12345678")

    def test_crea_orden_lineas_con_lote_y_evento(self):
        hoy = timezone.localdate()
        orden = anunciar_asn(
            self.cliente, hoy, 2,
            [(self.sku, 10, "L-1", date(2027, 1, 1)), (self.sku, 5, "", None)],
            self.usuario, origen="mesa", motivo="Capturada por Mesa",
        )
        self.assertEqual(orden.estado, OrdenEntrada.ANUNCIADA)
        self.assertEqual(orden.tarimas, 2)
        lineas = list(orden.lineas.order_by("id"))
        self.assertEqual([(l.cantidad_anunciada, l.lote_codigo, l.fecha_caducidad) for l in lineas],
                         [(10, "L-1", date(2027, 1, 1)), (5, "", None)])
        evento = EventoAuditoria.objects.get(entidad="asn", entidad_id=orden.folio, accion="anunciada_mesa")
        self.assertEqual(evento.delta["lineas"], [
            {"sku": "COLIMITA-SIX", "cantidad": 10, "lote": "L-1"}, {"sku": "COLIMITA-SIX", "cantidad": 5},
        ])
        self.assertEqual(evento.motivo, "Capturada por Mesa")

    def test_sku_ajeno_revierte_todo(self):
        with self.assertRaisesMessage(ValueError, "no es de"):
            anunciar_asn(self.cliente, timezone.localdate(), 0,
                         [(self.sku, 1, "", None), (self.sku_ajeno, 1, "", None)], self.usuario, origen="portal")
        self.assertEqual(OrdenEntrada.objects.count(), 0)
