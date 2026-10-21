"""Estatus de envia.com según el censo del 2026-10-21: "Shipped" es tránsito,
"Undeliverable" cuenta como intento fallido (7 de 11 se entregaron después),
y "Delayed" / "Information" / "N/A" son neutros: no mueven la guía pero quedan
en el historial con su texto."""
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.catalogo.models import SKU
from apps.envios import services
from apps.envios.adapters import MockAdapter, estatus_neutro_envia, normalizar_estado_envia
from apps.envios.models import EventoGuia, Guia, Paquete, PaqueteLinea
from apps.envios.webhooks import info_desde_envia
from apps.pedidos.models import LineaPedido

from .base import crear_cliente, crear_pedido, crear_tienda


class NormalizacionEnviaTests(TestCase):
    def test_shipped_undeliverable_y_neutros(self):
        self.assertEqual(normalizar_estado_envia("Shipped"), "EN_TRANSITO")
        self.assertEqual(normalizar_estado_envia("Undeliverable"), "INTENTO_FALLIDO")
        self.assertEqual(normalizar_estado_envia("1 delivery attempt"), "INTENTO_FALLIDO")
        self.assertEqual(normalizar_estado_envia("Delivered"), "ENTREGADO")
        self.assertEqual(normalizar_estado_envia("Returned"), "RETORNO")
        for neutro in ("Delayed", "Information", "N/A", "Redirected"):
            self.assertIsNone(normalizar_estado_envia(neutro), neutro)
            self.assertTrue(estatus_neutro_envia(neutro), neutro)
        self.assertFalse(estatus_neutro_envia("Shipped"))

    def test_webhook_conserva_el_estatus_crudo(self):
        numero, info = info_desde_envia({"tracking_number": "X1", "status": "Delayed"})
        self.assertEqual((numero, info["estado"], info["crudo"]), ("X1", None, "Delayed"))
        numero, info = info_desde_envia({"tracking_number": "X2", "status": "Shipped"})
        self.assertEqual(info["estado"], "EN_TRANSITO")


@override_settings(ENVIA_API_KEY="")
class EstatusNeutrosEnElPollerTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="RECOLECTADO")
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=2000, requiere_lote=False, precio_declarado=Decimal(180))
        linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=1, reservada=True, cantidad_pickeada=1, cantidad_despachada=1)
        caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("2"), carrier="estafeta", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1)
        self.guia = Guia.objects.create(pedido=self.pedido, paquete=caja, carrier="estafeta", numero="E-1", proveedor="envia", estado=Guia.RECOLECTADO)

    def test_delayed_no_mueve_la_guia_pero_queda_con_su_texto(self):
        _, info = info_desde_envia({"tracking_number": "E-1", "status": "Delayed"})
        services.procesar_evento_carrier("envia", "E-1", info)
        self.guia.refresh_from_db()
        self.assertEqual(self.guia.estado, Guia.RECOLECTADO)
        evento = EventoGuia.objects.get(guia=self.guia)
        self.assertEqual((evento.crudo, evento.descripcion, evento.estado), ("Delayed", "Delayed", ""))

    def test_shipped_pasa_a_transito(self):
        _, info = info_desde_envia({"tracking_number": "E-1", "status": "Shipped"})
        services.procesar_evento_carrier("envia", "E-1", info)
        self.guia.refresh_from_db(); self.pedido.refresh_from_db()
        self.assertEqual((self.guia.estado, self.pedido.estado), (Guia.EN_TRANSITO, "EN_TRANSITO"))
