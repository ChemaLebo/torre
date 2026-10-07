"""Guía propia (Chema 2026-10-07, PED-00317/00319): forzar "Sin guía" sobre
cajas ya empacadas emite las guías internas en ese mismo paso y el pedido va
a Salida, en vez de quedarse "sin guía" en la mesa. Y _carrier_de_paquete
con aplicar=False no deja rastro (diagnóstico)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.models import Paquete, PaqueteLinea
from apps.envios.services import _carrier_de_paquete
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class GuiaPropiaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        self.pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = self.pedido.lineas.get()
        self.c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO, ts_cierre=self.pedido.creado)
        self.c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO, ts_cierre=self.pedido.creado)
        PaqueteLinea.objects.create(paquete=self.c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=self.c2, linea_pedido=linea, cantidad=2)

    def test_forzar_local_con_cajas_empacadas_manda_a_salida(self):
        r = services.replanear_con_carrier(self.pedido, "local", self.mesa)
        self.pedido.refresh_from_db()
        self.assertEqual(r["modo"], "recotizadas")
        self.assertEqual(sorted(g.numero for g in r["guias"]), [f"LOCAL-{self.pedido.folio}-1", f"LOCAL-{self.pedido.folio}-2"])
        self.assertEqual((self.pedido.estado, self.pedido.carrier_forzado), (Pedido.GUIA_GENERADA, "local"))
        self.assertTrue(self.pedido.empaque_completo)
        self.login_piso()
        self.assertContains(self.client.get(reverse("piso:salida")), self.pedido.folio)

    def test_forzar_local_sin_cajas_empacadas_no_emite_nada(self):
        Paquete.objects.filter(pedido=self.pedido).update(estado=Paquete.PLANEADO)
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.EN_PICKING)
        self.pedido.refresh_from_db()
        r = services.replanear_con_carrier(self.pedido, "local", self.mesa)
        self.assertEqual((r["modo"], r["guias"]), ("replaneadas", []))
        self.assertFalse(self.pedido.guias.exists())

    def test_resolver_carrier_sin_aplicar_no_deja_rastro(self):
        Pedido.objects.filter(pk=self.pedido.pk).update(carrier_forzado="local")
        self.pedido.refresh_from_db()
        antes = EventoAuditoria.objects.count()
        self.assertEqual(_carrier_de_paquete(self.pedido, self.c1, aplicar=False), ("local", "entrega_local"))
        self.c1.refresh_from_db()
        self.assertEqual((self.c1.carrier, EventoAuditoria.objects.count()), ("estafeta", antes))
        self.assertEqual(_carrier_de_paquete(self.pedido, self.c1), ("local", "entrega_local"))  # con aplicar sí guarda y audita
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.carrier, "local")
        self.assertEqual(EventoAuditoria.objects.count(), antes + 1)
