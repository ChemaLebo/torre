"""Paquetería de respaldo por cliente (Chema 2026-10-09): si la paquetería
vigente no cotiza el pedido AL PLANEAR, se planea completo con la de
respaldo. Solo al planear; con paquetería forzada no hay respaldo; un fallo
al comprar sigue en DET como siempre."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios import cotizador, services
from apps.envios.adapters import ErrorCarrier, MockAdapter
from apps.envios.models import Guia
from apps.pedidos.models import LineaPedido

from .base import crear_cliente, crear_pedido, crear_tienda

MERIDA = "97000"  # puntopost no cubre Mérida en el mock


def _sin_cobertura(carriers):
    """MockAdapter.cotizar_lane que no cotiza esos carriers (los demás, como siempre)."""
    original = MockAdapter.cotizar_lane

    def cotizar_lane(self_, carrier, cp_destino, peso_kg, dims=None):
        if carrier in carriers:
            return {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        return original(self_, carrier, cp_destino, peso_kg, dims=dims)
    return patch.object(MockAdapter, "cotizar_lane", autospec=True, side_effect=cotizar_lane)


class RespaldoAlPlanearTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        # Reparto con una sola carta (puntopost) y estafeta de respaldo.
        self.cliente = crear_cliente(
            integracion_envios="reparto", reparto_pesos={"puntopost": 100}, carrier_respaldo="estafeta",
        )
        self.tienda = crear_tienda(self.cliente)
        self.six = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=4000, precio_declarado=Decimal(300))

    def _pedido(self, cp=MERIDA, **kwargs):
        pedido = crear_pedido(self.cliente, self.tienda, cp=cp, es_local=False, **kwargs)
        LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=1, reservada=True)
        return pedido

    def _eventos_respaldo(self, pedido):
        return list(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="plan_respaldo"))

    def test_si_la_carta_no_cotiza_se_planea_con_el_respaldo(self):
        pedido = self._pedido()
        [paquete] = cotizador.planificar_envio(pedido)
        self.assertEqual(paquete.carrier, "estafeta")
        self.assertIsNotNone(paquete.precio_cotizado)
        [evento] = self._eventos_respaldo(pedido)
        self.assertEqual((evento.delta["de"], evento.delta["a"], evento.delta["ok"], evento.delta["paquetes"]), (["puntopost"], "estafeta", True, 1))
        pedido.refresh_from_db()
        self.assertEqual(pedido.reparto_carrier, "puntopost")  # la carta se queda: es el dato del fallo

    def test_la_guia_del_plan_de_respaldo_se_compra_tal_cual(self):
        pedido = self._pedido()
        cotizador.planificar_envio(pedido)
        [guia] = services.generar_guias(pedido)
        self.assertEqual(guia.carrier, "estafeta")
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="replan_paquete").exists())

    def test_si_el_respaldo_tampoco_cotiza_es_el_error_de_siempre_con_las_dos(self):
        pedido = self._pedido()
        with _sin_cobertura({"estafeta"}), self.assertRaises(ValueError) as ctx:
            cotizador.planificar_envio(pedido)
        self.assertIsInstance(ctx.exception, cotizador.SinCotizacion)
        self.assertIn("puntopost", str(ctx.exception))
        self.assertIn("respaldo (estafeta)", str(ctx.exception))
        self.assertEqual(pedido.paquetes.count(), 0)
        [evento] = self._eventos_respaldo(pedido)
        self.assertFalse(evento.delta["ok"])

    def test_sin_respaldo_no_hay_segundo_intento(self):
        self.cliente.carrier_respaldo = ""
        self.cliente.save()
        pedido = self._pedido()
        with self.assertRaises(ValueError):
            cotizador.planificar_envio(pedido)
        self.assertEqual(self._eventos_respaldo(pedido), [])

    def test_con_paqueteria_forzada_no_hay_respaldo(self):
        pedido = self._pedido(carrier_forzado="puntopost")
        with self.assertRaises(ValueError):
            cotizador.planificar_envio(pedido)
        self.assertEqual(self._eventos_respaldo(pedido), [])
        self.assertEqual(pedido.paquetes.count(), 0)

    def test_respaldo_que_ya_se_intento_no_se_repite(self):
        self.cliente.carrier_respaldo = "puntopost"
        self.cliente.save()
        pedido = self._pedido()
        with self.assertRaises(ValueError):
            cotizador.planificar_envio(pedido)
        self.assertEqual(self._eventos_respaldo(pedido), [])

    def test_si_la_carta_cotiza_el_respaldo_no_entra(self):
        pedido = self._pedido(cp="44100")  # puntopost sí cubre Guadalajara
        [paquete] = cotizador.planificar_envio(pedido)
        self.assertEqual(paquete.carrier, "puntopost")
        self.assertEqual(self._eventos_respaldo(pedido), [])

    def test_fallo_al_comprar_sigue_en_det_sin_respaldo(self):
        pedido = self._pedido(cp="44100")
        cotizador.planificar_envio(pedido)
        with patch.object(MockAdapter, "generar", side_effect=ErrorCarrier("caído")), self.assertRaises(ErrorCarrier):
            services.generar_guias(pedido)
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 0)
        self.assertEqual(self._eventos_respaldo(pedido), [])
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="error_generacion_guia").exists())


class RespaldoClienteDirectoTests(TestCase):
    """Cliente en 99minutos directo: la lista vigente es solo noventa9Minutos;
    si no cotiza, el respaldo planea y su guía se compra sin replan."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente(integracion_envios="99minutos", carrier_respaldo="estafeta")
        self.tienda = crear_tienda(self.cliente)
        self.six = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=4000, precio_declarado=Decimal(300))

    def test_sin_cobertura_de_99minutos_sale_con_estafeta(self):
        pedido = crear_pedido(self.cliente, self.tienda, cp="44100", es_local=False)
        LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=1, reservada=True)
        with _sin_cobertura({"noventa9Minutos"}):
            [paquete] = cotizador.planificar_envio(pedido)
            self.assertEqual(paquete.carrier, "estafeta")
            [guia] = services.generar_guias(pedido)
        self.assertEqual(guia.carrier, "estafeta")
        self.assertEqual(services._carriers_vigentes(pedido), ["noventa9Minutos", "estafeta"])
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="replan_paquete").exists())
