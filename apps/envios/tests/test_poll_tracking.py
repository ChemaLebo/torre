"""poll_tracking: sincronización de pedido, incidencias RF/RET y umbrales."""
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.envios import services
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, ReglaEnvio

from .base import crear_cliente, crear_pedido, crear_tienda


@override_settings(ENVIA_API_KEY="")
class PollTrackingTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.adapter = MockAdapter()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)

    def _pedido_recolectado(self, **kwargs):
        """Pedido con guía, ya recolectado por manifiesto (flujo normal de piso)."""
        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO", **kwargs)
        guia = services.generar_guia(pedido)
        pedido.refresh_from_db()
        pedido.transicionar("RECOLECTADO", motivo="Manifiesto firmado (test)")
        return pedido, guia

    def _envejecer(self, guia, horas):
        Guia.objects.filter(pk=guia.pk).update(
            ts_ultimo_movimiento=timezone.now() - timedelta(hours=horas)
        )

    def test_poll_rutea_adapter_por_proveedor_de_la_guia(self):
        """Cada guía se rastrea con la integración que la emitió, no con una global."""
        pedido = crear_pedido(self.cliente, self.tienda, estado="RECOLECTADO")
        Guia.objects.create(pedido=pedido, carrier="estafeta", numero="MOCK-0077", proveedor="mock")
        Guia.objects.create(pedido=pedido, carrier="noventa9Minutos", numero="99M-1", proveedor="envia")
        pedidos_proveedor = []

        def fabrica(carrier=None, proveedor=None):
            pedidos_proveedor.append(proveedor)
            return MockAdapter()

        with patch.object(services, "get_adapter", side_effect=fabrica):
            services.poll_tracking()
        self.assertEqual(sorted(pedidos_proveedor), ["envia", "mock"])

    def test_en_transito_actualiza_guia_y_pedido(self):
        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        resumen = services.poll_tracking()
        guia.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual(guia.estado, Guia.EN_TRANSITO)
        self.assertEqual(pedido.estado, "EN_TRANSITO")
        self.assertEqual(resumen["actualizadas"], 1)
        self.assertIsNotNone(guia.ts_ultimo_movimiento)

    def test_entregado_cierra_guia_y_pedido(self):
        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        services.poll_tracking()
        self.adapter.avanzar_estado(guia.numero, "ENTREGADO")
        services.poll_tracking()
        guia.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual(guia.estado, Guia.ENTREGADO)
        self.assertEqual(pedido.estado, "ENTREGADO")
        # Guía terminal: el siguiente poll ya no la rastrea.
        resumen = services.poll_tracking()
        self.assertEqual(resumen["rastreadas"], 0)

    def test_intento_fallido_abre_incidencia_rf_p1(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "INTENTO_FALLIDO")
        services.poll_tracking()
        guia.refresh_from_db()
        self.assertEqual(guia.estado, Guia.INTENTO_FALLIDO)
        incidencia = Incidencia.objects.get(pedido=pedido)
        self.assertEqual(incidencia.tipo, "RF")
        self.assertEqual(incidencia.prioridad, "P1")

    def test_retorno_marca_pedido_retornado_y_abre_incidencia(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        services.poll_tracking()
        self.adapter.avanzar_estado(guia.numero, "RETORNO")
        services.poll_tracking()
        guia.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual(guia.estado, Guia.RETORNO)
        self.assertEqual(pedido.estado, "RETORNADO")
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo="RF").exists())

    def test_sin_movimiento_foraneo_abre_ret_y_no_duplica(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        services.poll_tracking()  # fija estado y último evento
        self._envejecer(guia, horas=80)  # umbral foráneo: 72 h
        services.poll_tracking()
        self.assertEqual(Incidencia.objects.filter(pedido=pedido, tipo="RET").count(), 1)
        # Segundo poll con la incidencia activa: no duplica.
        self._envejecer(guia, horas=90)
        services.poll_tracking()
        self.assertEqual(Incidencia.objects.filter(pedido=pedido, tipo="RET").count(), 1)

    def test_sin_movimiento_respeta_umbral_por_ruta(self):
        from apps.incidencias.models import Incidencia

        # Regla que fuerza carrier externo también para locales.
        ReglaEnvio.objects.create(cliente=None, prioridad=1, condicion={}, carrier="paquetexpress", servicio="ground")
        pedido_local, guia_local = self._pedido_recolectado(es_local=True, cp="28017")
        pedido_foraneo, guia_foraneo = self._pedido_recolectado()
        for guia in (guia_local, guia_foraneo):
            self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        services.poll_tracking()
        # 30 h sin movimiento: dispara local (24 h), todavía no foráneo (72 h).
        self._envejecer(guia_local, horas=30)
        self._envejecer(guia_foraneo, horas=30)
        services.poll_tracking()
        self.assertTrue(Incidencia.objects.filter(pedido=pedido_local, tipo="RET").exists())
        self.assertFalse(Incidencia.objects.filter(pedido=pedido_foraneo, tipo="RET").exists())

    def test_guias_locales_no_se_rastrean(self):
        # Guía carrier "local" (flota propia / datos viejos): el poller la
        # ignora. Se genera con el flag encendido porque sin flota
        # (TORRE["FLOTA_PROPIA"]=False, default) ya no se emiten guías "local".
        from django.conf import settings
        from django.test import override_settings

        pedido = crear_pedido(self.cliente, self.tienda, es_local=True, cp="28017")
        with override_settings(TORRE={**settings.TORRE, "FLOTA_PROPIA": True}):
            services.generar_guia(pedido)
        resumen = services.poll_tracking()
        self.assertEqual(resumen["rastreadas"], 0)

    def test_sin_movimiento_no_cuenta_si_hubo_evento_nuevo(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        self._envejecer(guia, horas=80)
        # El mismo poll trae movimiento nuevo (cambio de estado): resetea el reloj.
        services.poll_tracking()
        guia.refresh_from_db()
        self.assertEqual(guia.estado, Guia.EN_TRANSITO)
        self.assertFalse(Incidencia.objects.filter(pedido=pedido, tipo="RET").exists())


# La lista blanca de producción cambia con el negocio (2026-09-20: solo imile);
# estas pruebas ejercitan el mecanismo con los carriers que conoce la tabla mock.
TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


@override_settings(TORRE=TORRE_CARRIERS_CLASICOS)
class MultiGuiaTests(TestCase):
    """El pedido se mueve por el CONJUNTO de sus guías: una caja entregada
    no entrega el pedido, una caja regresada no lo retorna, y mientras queden
    cajas en bodega (PARCIALMENTE_DESPACHADO) el tracking no lo toca."""

    def setUp(self):
        from decimal import Decimal

        from apps.envios.models import Paquete

        MockAdapter.reiniciar()
        self.adapter = MockAdapter()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        for numero in (1, 2):
            Paquete.objects.create(
                pedido=self.pedido, numero=numero, peso_kg=Decimal("2"),
                carrier="estafeta", servicio="ground",
            )
        guias = services.generar_guias(self.pedido)
        self.g1, self.g2 = sorted(guias, key=lambda g: g.paquete.numero)
        self.pedido.refresh_from_db()

    def _poll(self, guia, estado):
        self.adapter.avanzar_estado(guia.numero, estado)
        services.poll_tracking()
        self.pedido.refresh_from_db()
        return self.pedido.estado

    def test_una_caja_entregada_no_entrega_el_pedido(self):
        self.pedido.transicionar("RECOLECTADO", motivo="Manifiesto (test)")
        self.assertEqual(self._poll(self.g1, "EN_TRANSITO"), "EN_TRANSITO")
        self.assertEqual(self._poll(self.g1, "ENTREGADO"), "EN_TRANSITO")
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.estado, Guia.ENTREGADO)
        self.assertEqual(self._poll(self.g2, "ENTREGADO"), "ENTREGADO")

    def test_una_caja_regresada_no_retorna_el_pedido(self):
        from apps.incidencias.models import Incidencia

        self.pedido.transicionar("RECOLECTADO", motivo="Manifiesto (test)")
        self._poll(self.g1, "EN_TRANSITO")
        self.assertEqual(self._poll(self.g1, "RETORNO"), "EN_TRANSITO")
        self.assertTrue(Incidencia.objects.filter(pedido=self.pedido, tipo="RF").exists())
        # La caja que sigue viva decide: entregada → el pedido queda entregado.
        self.assertEqual(self._poll(self.g2, "ENTREGADO"), "ENTREGADO")

    def test_todas_las_cajas_regresadas_retorna_el_pedido(self):
        self.pedido.transicionar("RECOLECTADO", motivo="Manifiesto (test)")
        self._poll(self.g1, "EN_TRANSITO")
        self._poll(self.g1, "RETORNO")
        self.assertEqual(self._poll(self.g2, "RETORNO"), "RETORNADO")

    def test_con_cajas_en_bodega_el_tracking_no_mueve_el_pedido(self):
        self.pedido.transicionar("PARCIALMENTE_DESPACHADO", motivo="Salió la caja 1 (test)")
        self.assertEqual(self._poll(self.g1, "EN_TRANSITO"), "PARCIALMENTE_DESPACHADO")
        self.assertEqual(self._poll(self.g1, "ENTREGADO"), "PARCIALMENTE_DESPACHADO")
        # Sale la última caja: RECOLECTADO, y el tracking de la 2 cierra el pedido.
        self.pedido.transicionar("RECOLECTADO", motivo="Salió la caja 2 (test)")
        self.assertEqual(self._poll(self.g2, "ENTREGADO"), "ENTREGADO")


class EventosGuiaTests(TestCase):
    """El poller guarda el historial de rastreo en EventoGuia: uno por
    movimiento visto con el mock (sin historial), el historial completo del
    carrier cuando lo manda, siempre deduplicado."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.adapter = MockAdapter()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        self.guia = services.generar_guia(self.pedido)
        self.pedido.refresh_from_db()
        self.pedido.transicionar("RECOLECTADO", motivo="Manifiesto (test)")

    def test_un_evento_por_movimiento_sin_duplicar(self):
        from apps.envios.models import EventoGuia

        self.adapter.avanzar_estado(self.guia.numero, "EN_TRANSITO")
        services.poll_tracking()
        services.poll_tracking()  # mismo estado y descripción: nada nuevo
        self.adapter.avanzar_estado(self.guia.numero, "ENTREGADO")
        services.poll_tracking()
        eventos = list(EventoGuia.objects.filter(guia=self.guia))
        self.assertEqual([e.estado for e in eventos], ["EN_TRANSITO", "ENTREGADO"])
        self.assertTrue(all(e.ts_visto for e in eventos))

    def test_historial_del_carrier_con_su_hora(self):
        from datetime import datetime, timezone as tz

        from apps.envios.models import EventoGuia

        t1 = datetime(2026, 9, 14, 10, 0, tzinfo=tz.utc)
        t2 = datetime(2026, 9, 15, 8, 30, tzinfo=tz.utc)
        info = {
            "estado": "EN_RUTA", "descripcion": "Out for delivery", "ts_evento": t2, "raw": {},
            "eventos": [
                {"estado": "RECOLECTADO", "crudo": "pickup", "descripcion": "Picked up", "ts": t1, "raw": {"a": 1}},
                {"estado": "EN_RUTA", "crudo": "ofd", "descripcion": "Out for delivery", "ts": t2, "raw": {"a": 2}},
            ],
        }
        self.assertEqual(services._guardar_eventos(self.guia, info, "EN_RUTA", True, timezone.now()), 2)
        self.assertEqual(services._guardar_eventos(self.guia, info, "", False, timezone.now()), 0)
        eventos = list(EventoGuia.objects.filter(guia=self.guia))
        self.assertEqual([(e.estado, e.ts_carrier) for e in eventos], [("RECOLECTADO", t1), ("EN_RUTA", t2)])
        self.assertEqual(eventos[0].raw, {"a": 1})


@override_settings(ENVIA_API_KEY="")
class EventosShopifyDesdePollerTests(TestCase):
    """Cada cambio de estado de la guía también viaja a Shopify como
    FulfillmentEvent (lazy, best-effort): el poller solo lo dispara."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.adapter = MockAdapter()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)

    def test_el_avance_se_manda_con_estado_descripcion_y_hora(self):
        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        guia = services.generar_guia(pedido)
        pedido.refresh_from_db()
        pedido.transicionar("RECOLECTADO", motivo="Manifiesto firmado (test)")
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        with patch("apps.integraciones.services.registrar_evento_fulfillment") as evento:
            services.poll_tracking()
        evento.assert_called_once()
        args, kwargs = evento.call_args
        self.assertEqual((args[0].pk, args[1].pk, args[2]), (pedido.pk, guia.pk, "EN_TRANSITO"))
        guia.refresh_from_db()
        self.assertEqual(kwargs["ts"], guia.ts_ultimo_movimiento)

    def test_shopify_caido_no_detiene_el_rastreo(self):
        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        guia = services.generar_guia(pedido)
        pedido.refresh_from_db()
        pedido.transicionar("RECOLECTADO", motivo="Manifiesto firmado (test)")
        self.adapter.avanzar_estado(guia.numero, "ENTREGADO")
        with patch("apps.integraciones.services.registrar_evento_fulfillment", side_effect=RuntimeError("caído")):
            resumen = services.poll_tracking()
        self.assertEqual(resumen["actualizadas"], 1)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, "ENTREGADO")
