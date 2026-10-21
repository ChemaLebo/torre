"""Regreso reportado (Chema 2026-10-21): "Redirected" de envia.com manda la
caja a Reingresos por decidir con su RF sin dar la guía por regresada; si
después llega "entregado" la marca se limpia sola; y el retorno manual desde
Mesa pasa la guía a RETORNO por el mismo camino que un retorno del carrier."""
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios import services
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Incidencia
from apps.pedidos.models import LineaPedido
from apps.pedidos.services import cajas_por_reingresar, reportar_regreso_caja

from .base import crear_cliente, crear_pedido, crear_tienda


def _info(descripcion, estado=None, status=None):
    raw = {"status": status or descripcion, "rto": 0}
    return {
        "estado": estado, "descripcion": descripcion, "ts_evento": None, "raw": raw,
        "eventos": [{"estado": estado or "", "crudo": estado or "", "descripcion": descripcion, "ts": None, "raw": raw}],
    }


@override_settings(ENVIA_API_KEY="")
class RegresoReportadoTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="EN_TRANSITO")
        sku = SKU.objects.create(cliente=self.cliente, codigo="CAJA-24", descripcion="Caja 24", peso_gr=9000, requiere_lote=False, precio_declarado=Decimal(500))
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=1, reservada=True, cantidad_pickeada=1, cantidad_despachada=1)
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("9"), carrier="estafeta", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.linea, cantidad=1)
        self.guia = Guia.objects.create(pedido=self.pedido, paquete=self.caja, carrier="estafeta", numero="205870980061070999C4OC", proveedor="envia", estado=Guia.RECOLECTADO)

    def test_redirected_deja_la_caja_por_decidir_sin_tocar_la_guia(self):
        resultado = services.procesar_evento_carrier("envia", self.guia.numero, _info("Redirected"))
        self.assertTrue(resultado["ok"])
        self.guia.refresh_from_db(); self.caja.refresh_from_db()
        self.assertEqual(self.guia.estado, Guia.RECOLECTADO)  # no se da por regresada
        self.assertIsNotNone(self.caja.regreso_reportado)
        self.assertEqual([c.pk for c in cajas_por_reingresar(self.pedido)], [self.caja.pk])
        rf = Incidencia.objects.get(pedido=self.pedido, tipo="RF")
        self.assertIn("Redirected", rf.mensajes.first().texto)
        evento = EventoAuditoria.objects.get(entidad="paquete", entidad_id=str(self.caja.pk), accion="regreso_reportado")
        self.assertEqual(evento.delta["origen"], "carrier")
        # Un segundo Redirected no vuelve a reportar ni abre otra RF.
        services.procesar_evento_carrier("envia", self.guia.numero, _info("Redirected"))
        self.assertEqual(Incidencia.objects.filter(pedido=self.pedido, tipo="RF").count(), 1)

    def test_entregado_despues_limpia_el_regreso_reportado(self):
        services.procesar_evento_carrier("envia", self.guia.numero, _info("Redirected"))
        services.procesar_evento_carrier("envia", self.guia.numero, _info("Delivered", estado=Guia.ENTREGADO))
        self.guia.refresh_from_db(); self.caja.refresh_from_db(); self.pedido.refresh_from_db()
        self.assertEqual((self.guia.estado, self.caja.regreso_reportado, self.pedido.estado), (Guia.ENTREGADO, None, "ENTREGADO"))
        self.assertEqual(cajas_por_reingresar(self.pedido), [])
        self.assertTrue(EventoAuditoria.objects.filter(entidad="paquete", entidad_id=str(self.caja.pk), accion="regreso_descartado").exists())

    def test_retorno_manual_desde_mesa(self):
        reportar_regreso_caja(self.pedido, self.caja, None, "la trajo el chofer", manual=True)
        self.guia.refresh_from_db(); self.caja.refresh_from_db(); self.pedido.refresh_from_db()
        self.assertEqual((self.guia.estado, self.pedido.estado), (Guia.RETORNO, "RETORNADO"))
        self.assertEqual(self.guia.ultimo_evento, "la trajo el chofer")
        self.assertIsNotNone(self.caja.regreso_reportado)
        self.assertTrue(Incidencia.objects.filter(pedido=self.pedido, tipo="RF").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(self.guia.pk), accion="retorno_manual").exists())
        with self.assertRaisesMessage(ValueError, "no ha salido"):
            caja2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal("9"), carrier="estafeta", estado=Paquete.EMPACADO)
            reportar_regreso_caja(self.pedido, caja2, None, "x")
