"""Reparto de carriers por porcentajes (envios.reparto): tamaño de bloque y
tope, baraja reproducible y exacta, cursor/base, elegir_carrier respeta
reglas → local → reparto → default, plan y guías con la carta, sin fallback
al fallar, eventos de auditoría y reporte mensual."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios import cotizador, reparto, services
from apps.envios.adapters import ErrorCarrier, MockAdapter
from apps.envios.models import Guia, ReglaEnvio
from apps.pedidos.models import LineaPedido

from .base import crear_cliente, crear_pedido, crear_tienda

PESOS = {"noventa9Minutos": 75, "estafeta": 25}
TORRE_CON_FLOTA = {**settings.TORRE, "FLOTA_PROPIA": True}


class TamanoBloqueTests(SimpleTestCase):
    def test_minimo_que_representa_los_pesos(self):
        self.assertEqual(reparto.tamano_bloque(PESOS), 4)
        self.assertEqual(reparto.tamano_bloque({"a": 70, "b": 30}), 10)
        self.assertEqual(reparto.tamano_bloque({"a": 92, "b": 8}), 25)
        self.assertEqual(reparto.tamano_bloque({"a": 100}), 1)
        self.assertEqual(reparto.tamano_bloque({"a": 50, "b": 50}), 2)
        self.assertEqual(reparto.tamano_bloque({}), 0)
        self.assertEqual(reparto.tamano_bloque({"a": 0, "b": 100}), 1)

    def test_tope_redondea_y_la_suma_siempre_es_el_bloque(self):
        pesos = {"a": 92.25, "b": 7.75}  # exacto pediría 400 cartas
        self.assertEqual(reparto.tamano_bloque(pesos), 100)
        self.assertEqual(reparto.cartas_por_carrier(pesos, 100), {"a": 92, "b": 8})
        tercios = {"a": 33.33, "b": 33.33, "c": 33.34}
        self.assertEqual(reparto.tamano_bloque(tercios), 100)
        self.assertEqual(sum(reparto.cartas_por_carrier(tercios, 100).values()), 100)
        self.assertEqual(reparto.cartas_por_carrier(tercios, 100), {"a": 33, "b": 33, "c": 34})
        self.assertEqual(reparto.tamano_bloque({"a": 92, "b": 8}, tope=10), 10)
        self.assertEqual(reparto.cartas_por_carrier({"a": 92, "b": 8}, 10), {"a": 9, "b": 1})

    def test_resumen_avisa_los_redondeos(self):
        exacto = reparto.resumen_pesos(PESOS)
        self.assertEqual(exacto["tam"], 4)
        self.assertEqual(exacto["redondeados"], [])
        self.assertEqual([(f["carrier"], f["cartas"]) for f in exacto["filas"]], [("estafeta", 1), ("noventa9Minutos", 3)])
        redondeado = reparto.resumen_pesos({"imile": 92.25, "noventa9Minutos": 7.75})
        self.assertEqual(redondeado["tam"], 100)
        self.assertEqual(redondeado["redondeados"], ["imile 92.25 → 92", "noventa9Minutos 7.75 → 8"])


class BarajaTests(SimpleTestCase):
    def test_reproducible_y_exacta(self):
        una = reparto.baraja("colima", 7, PESOS)
        self.assertEqual(una, reparto.baraja("colima", 7, PESOS))
        self.assertEqual(len(una), 4)
        self.assertEqual(una.count("noventa9Minutos"), 3)
        self.assertEqual(una.count("estafeta"), 1)
        grande = reparto.baraja("colima", 0, {"a": 92.25, "b": 7.75})
        self.assertEqual((len(grande), grande.count("a"), grande.count("b")), (100, 92, 8))

    def test_la_semilla_es_slug_y_bloque(self):
        bloques = [reparto.baraja("colima", b, PESOS) for b in range(12)]
        self.assertGreater(len({tuple(b) for b in bloques}), 1)  # no siempre el mismo orden
        self.assertNotEqual(
            [reparto.baraja("colima", b, {"a": 50, "b": 50}) for b in range(20)],
            [reparto.baraja("nocturno", b, {"a": 50, "b": 50}) for b in range(20)],
        )
        self.assertEqual(reparto.semilla("colima", 3), reparto.semilla("colima", 3))
        self.assertNotEqual(reparto.semilla("colima", 3), reparto.semilla("colima-3", 0))


class SacarCartaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente(integracion_envios="reparto", reparto_pesos=PESOS)
        self.tienda = crear_tienda(self.cliente)

    def _pedidos(self, n):
        return [crear_pedido(self.cliente, self.tienda) for _ in range(n)]

    def test_sigue_la_baraja_bloque_a_bloque_y_es_exacta(self):
        cartas = [reparto.sacar_carta(p) for p in self._pedidos(8)]
        esperadas = reparto.baraja(self.cliente.slug, 0, PESOS) + reparto.baraja(self.cliente.slug, 1, PESOS)
        self.assertEqual(cartas, esperadas)
        self.assertEqual((cartas.count("noventa9Minutos"), cartas.count("estafeta")), (6, 2))
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.reparto_cursor, 8)
        self.assertEqual(reparto.posicion(self.cliente), (2, 1, 4))

    def test_persiste_en_el_pedido_y_es_idempotente(self):
        [pedido] = self._pedidos(1)
        carta = reparto.sacar_carta(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.reparto_carrier, carta)
        self.assertEqual(reparto.sacar_carta(pedido), carta)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.reparto_cursor, 1)
        eventos = EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="reparto_carrier")
        self.assertEqual(eventos.count(), 1)
        self.assertEqual(eventos.get().delta, {"n": 0, "bloque": 0, "posicion": 0, "tam": 4, "carta": carta})
        self.assertEqual(eventos.get().cliente, self.cliente)

    def test_cambiar_pesos_arranca_bloque_nuevo_desde_la_base(self):
        for p in self._pedidos(3):
            reparto.sacar_carta(p)  # bloque 0 a medias (3 de 4)
        self.cliente.refresh_from_db()
        self.cliente.reparto_pesos = {"noventa9Minutos": 50, "estafeta": 50}
        self.cliente.reparto_base = self.cliente.reparto_cursor  # lo que hace la ficha
        self.cliente.save()
        nuevas = [reparto.sacar_carta(p) for p in self._pedidos(4)]
        pesos = {"noventa9Minutos": 50, "estafeta": 50}
        self.assertEqual(nuevas, reparto.baraja(self.cliente.slug, 0, pesos) + reparto.baraja(self.cliente.slug, 1, pesos))
        self.assertEqual(nuevas.count("estafeta"), 2)

    def test_sin_pesos_es_error_explicito(self):
        self.cliente.reparto_pesos = {}
        self.cliente.save()
        [pedido] = self._pedidos(1)
        with self.assertRaises(ValueError):
            reparto.sacar_carta(pedido)


class ElegirCarrierRepartoTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente(integracion_envios="reparto", reparto_pesos=PESOS)
        self.tienda = crear_tienda(self.cliente)

    def _cursor(self):
        self.cliente.refresh_from_db()
        return self.cliente.reparto_cursor

    def test_reparto_saca_carta_una_sola_vez(self):
        pedido = crear_pedido(self.cliente, self.tienda, es_local=False)
        carrier, servicio = services.elegir_carrier(pedido)
        self.assertEqual(carrier, reparto.baraja(self.cliente.slug, 0, PESOS)[0])
        self.assertEqual(servicio, services.SERVICIO_DEFAULT)
        self.assertEqual(services.elegir_carrier(pedido), (carrier, servicio))
        self.assertEqual(services.carriers_del_pedido(pedido), [carrier])
        self.assertEqual(self._cursor(), 1)

    def test_regla_explicita_gana_y_no_consume_carta(self):
        ReglaEnvio.objects.create(cliente=self.cliente, prioridad=1, condicion={"cp_prefijo": "97"}, carrier="fedex", servicio="ground")
        merida = crear_pedido(self.cliente, self.tienda, cp="97000")
        self.assertEqual(services.elegir_carrier(merida), ("fedex", "ground"))
        self.assertEqual(services.carriers_del_pedido(merida), ["fedex"])  # el plan también la respeta
        merida.refresh_from_db()
        self.assertEqual(merida.reparto_carrier, "")
        self.assertEqual(self._cursor(), 0)
        otro = crear_pedido(self.cliente, self.tienda, cp="44100")
        self.assertIn(services.elegir_carrier(otro)[0], PESOS)
        self.assertEqual(self._cursor(), 1)

    @override_settings(TORRE=TORRE_CON_FLOTA)
    def test_local_con_flota_va_antes_que_el_reparto(self):
        local = crear_pedido(self.cliente, self.tienda, es_local=True, cp="28017")
        self.assertEqual(services.elegir_carrier(local), ("local", "entrega_local"))
        self.assertEqual(self._cursor(), 0)

    def test_sin_pesos_cae_al_default_sin_explotar(self):
        self.cliente.reparto_pesos = {}
        self.cliente.save()
        pedido = crear_pedido(self.cliente, self.tienda)
        self.assertEqual(services.elegir_carrier(pedido), ("paquetexpress", services.SERVICIO_DEFAULT))
        self.assertEqual(self._cursor(), 0)

    def test_los_demas_clientes_no_cambian(self):
        envia = crear_cliente(integracion_envios="envia")
        directo = crear_cliente(integracion_envios="99minutos")
        self.assertEqual(services.elegir_carrier(crear_pedido(envia, crear_tienda(envia)))[0], "paquetexpress")
        self.assertEqual(services.elegir_carrier(crear_pedido(directo, crear_tienda(directo)))[0], "noventa9Minutos")
        self.assertEqual(services.carriers_del_pedido(crear_pedido(envia, crear_tienda(envia))), list(settings.TORRE["CARRIERS_COTIZAR"]))


class PlanYGuiasRepartoTests(TestCase):
    """El plan y las guías respetan la carta: solo se cotiza ese carrier y,
    si falla, no hay handover a otro."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente(integracion_envios="reparto", reparto_pesos=PESOS)
        self.tienda = crear_tienda(self.cliente)
        self.six = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=4000, precio_declarado=Decimal(300))

    def _pedido(self, cp="44100"):
        pedido = crear_pedido(self.cliente, self.tienda, cp=cp, es_local=False)
        LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=1, reservada=True)
        return pedido

    def test_el_plan_solo_cotiza_la_carta(self):
        pedido = self._pedido()
        [paquete] = cotizador.planificar_envio(pedido)
        pedido.refresh_from_db()
        self.assertTrue(pedido.reparto_carrier)
        self.assertEqual(paquete.carrier, pedido.reparto_carrier)
        with patch("apps.envios.services.cotizar_lane_carrier", wraps=services.cotizar_lane_carrier) as clc:
            cotizador.cotizar_lane("06600", 8, carriers=["estafeta"])
        self.assertEqual([c.args[0] for c in clc.call_args_list], ["estafeta"])

    def test_las_guias_salen_con_la_carta(self):
        pedidos = [self._pedido() for _ in range(4)]
        guias = [services.generar_guia(p) for p in pedidos]
        for pedido, guia in zip(pedidos, guias):
            pedido.refresh_from_db()
            self.assertEqual(guia.carrier, pedido.reparto_carrier)
        self.assertEqual(sorted(g.carrier for g in guias), ["estafeta"] + ["noventa9Minutos"] * 3)

    def test_plan_viejo_con_otro_carrier_se_replanea_a_la_carta(self):
        from apps.envios.models import Paquete
        pedido = self._pedido()
        Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4.2"), carrier="fedex", servicio="ground")
        [guia] = services.generar_guias(pedido)
        pedido.refresh_from_db()
        self.assertEqual(guia.carrier, pedido.reparto_carrier)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="replan_paquete").exists())

    def test_sin_fallback_si_la_carta_no_cotiza(self):
        # puntopost no cubre Mérida en el mock: la carta se queda, el pedido sin plan.
        self.cliente.reparto_pesos = {"puntopost": 100}
        self.cliente.save()
        pedido = self._pedido(cp="97000")
        with self.assertRaises(ValueError):
            cotizador.planificar_envio(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.reparto_carrier, "puntopost")
        self.assertEqual(pedido.paquetes.count(), 0)

    def test_sin_fallback_si_la_carta_falla_al_generar(self):
        pedido = self._pedido()
        with patch.object(MockAdapter, "generar", side_effect=ErrorCarrier("caído")), self.assertRaises(ErrorCarrier):
            services.generar_guias(pedido)
        pedido.refresh_from_db()
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 0)
        self.assertTrue(pedido.reparto_carrier)  # la carta no se devuelve
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.reparto_cursor, 1)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="error_generacion_guia").exists())
        # El reintento reusa la misma carta: cero cartas nuevas.
        guia = services.generar_guia(pedido)
        self.assertEqual(guia.carrier, pedido.reparto_carrier)
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.reparto_cursor, 1)


class ReporteMesTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente(integracion_envios="reparto", reparto_pesos=PESOS)
        self.tienda = crear_tienda(self.cliente)
        self.ahora = timezone.now()

    def _rango(self):
        return self.ahora - timedelta(days=1), self.ahora + timedelta(days=1)

    def test_cuenta_cartas_efectivas_forzados_y_pendientes(self):
        con_carta = crear_pedido(self.cliente, self.tienda, reparto_carrier="noventa9Minutos")
        Guia.objects.create(pedido=con_carta, carrier="noventa9Minutos", numero="A", proveedor="mock")
        forzado_despues = crear_pedido(self.cliente, self.tienda, reparto_carrier="noventa9Minutos")
        Guia.objects.create(pedido=forzado_despues, carrier="fedex", numero="B", proveedor="mock")
        crear_pedido(self.cliente, self.tienda, reparto_carrier="estafeta")  # sin guía aún
        crear_pedido(self.cliente, self.tienda, reparto_carrier="estafeta", estado="CANCELADO")
        por_regla = crear_pedido(self.cliente, self.tienda)
        Guia.objects.create(pedido=por_regla, carrier="paquetexpress", numero="C", proveedor="mock")
        crear_pedido(self.cliente, self.tienda)  # sin carta ni guía: pendiente, no cuenta como forzado
        otro = crear_cliente(integracion_envios="envia")
        crear_pedido(otro, crear_tienda(otro))

        [bloque] = reparto.reporte_mes(*self._rango())
        self.assertEqual(bloque["cliente"], self.cliente)
        self.assertEqual((bloque["pedidos"], bloque["cartas"], bloque["forzados_regla"]), (6, 4, 1))
        filas = {f["carrier"]: f for f in bloque["filas"]}
        self.assertEqual((filas["noventa9Minutos"]["cartas"], filas["noventa9Minutos"]["efectivas"], filas["noventa9Minutos"]["otro_carrier"]), (2, 1, 1))
        self.assertEqual((filas["estafeta"]["cartas"], filas["estafeta"]["sin_guia"], filas["estafeta"]["canceladas"]), (2, 1, 1))
        self.assertEqual((filas["noventa9Minutos"]["porcentaje"], filas["estafeta"]["peso"]), (50.0, 25))
        self.assertEqual((bloque["bloque"], bloque["posicion"], bloque["tam"]), (0, 1, 4))

    def test_cliente_que_ya_no_reparte_sale_por_sus_cartas_historicas(self):
        crear_pedido(self.cliente, self.tienda, reparto_carrier="estafeta")
        self.cliente.integracion_envios = "envia"
        self.cliente.save()
        [bloque] = reparto.reporte_mes(*self._rango())
        self.assertFalse(bloque["en_reparto"])
        self.assertEqual(bloque["cartas"], 1)
        self.assertEqual(reparto.reporte_mes(*self._rango(), cliente=crear_cliente()), [])

    def test_sin_pedidos_el_cliente_en_reparto_aparece_vacio(self):
        [bloque] = reparto.reporte_mes(*self._rango())
        self.assertEqual(bloque["cartas"], 0)
        self.assertEqual([f["carrier"] for f in bloque["filas"]], ["estafeta", "noventa9Minutos"])
