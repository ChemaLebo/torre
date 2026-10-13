"""poll_tracking: sincronización de pedido, incidencias RF/RET y umbrales."""
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import EventoAuditoria
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

    def test_el_retorno_abre_la_rf_aunque_las_automaticas_esten_pausadas(self):
        # Chema 2026-10-06 (PED-00067): la pausa se comía la RF del retorno; sin RF no hay dónde decidir el reenvío.
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        pedido.cliente.incidencias_auto_pausadas_hasta = timezone.localdate() + timedelta(days=30)
        pedido.cliente.save()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        services.poll_tracking()
        self.adapter.avanzar_estado(guia.numero, "RETORNO")
        services.poll_tracking()
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo="RF").exists())
        # Los demás avisos automáticos (p. ej. un intento fallido) siguen pausados.
        otro, g2 = self._pedido_recolectado()
        self.adapter.avanzar_estado(g2.numero, "INTENTO_FALLIDO")
        services.poll_tracking()
        self.assertFalse(Incidencia.objects.filter(pedido=otro).exists())

    def _con_compromiso(self, guia, dias):
        """Estampa la fecha compromiso como lo haría la salida: hoy + dias."""
        Guia.objects.filter(pk=guia.pk).update(fecha_compromiso=timezone.localdate() + timedelta(days=dias))

    def test_retraso_abre_ret_al_pasar_la_promesa_y_no_duplica(self):
        """Chema 2026-09-30: retraso = pasar la fecha compromiso (desde la salida)
        sin entrega; ya no cuenta el "sin movimiento"."""
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        self._con_compromiso(guia, dias=-1)  # prometida ayer
        services.poll_tracking()
        self.assertEqual(Incidencia.objects.filter(pedido=pedido, tipo="RET").count(), 1)
        inc = Incidencia.objects.get(pedido=pedido, tipo="RET")
        self.assertTrue(inc.mensajes.filter(texto__contains="1 día de retraso").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(guia.pk), accion="alerta_retraso").exists())
        services.poll_tracking()  # con la RET abierta no se duplica, aunque siga moviéndose
        self.assertEqual(Incidencia.objects.filter(pedido=pedido, tipo="RET").count(), 1)

    def test_sin_compromiso_o_dentro_de_la_promesa_no_hay_retraso(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        self._envejecer(guia, horas=200)  # quieta días enteros: ya no importa
        services.poll_tracking()  # sin fecha compromiso = aún no salió: nada
        self.assertFalse(Incidencia.objects.filter(pedido=pedido, tipo="RET").exists())
        self._con_compromiso(guia, dias=0)  # prometida para hoy: todavía no es retraso
        services.poll_tracking()
        self.assertFalse(Incidencia.objects.filter(pedido=pedido, tipo="RET").exists())

    def test_al_entregar_la_ret_automatica_se_cierra_sola(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        self._con_compromiso(guia, dias=-2)
        services.poll_tracking()
        inc = Incidencia.objects.get(pedido=pedido, tipo="RET")
        self.adapter.avanzar_estado(guia.numero, "ENTREGADO")
        services.poll_tracking()
        inc.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, inc.estado), ("ENTREGADO", Incidencia.CERRADA))
        self.assertTrue(inc.mensajes.filter(texto__contains="Retraso cerrado automáticamente").exists())
        self.assertFalse(pedido.incidencia_activa)

    def test_recoleccion_del_carrier_es_estatus_aparte_y_avisa_una_vez(self):
        """Chema 2026-09-25: el carrier recoge antes del manifiesto → se estampa
        ts_recolectado_carrier (guía y pedido), el compromiso corre desde ahí,
        Shopify y el comprador se enteran una vez; el manifiesto no lo mueve."""
        from datetime import datetime

        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO", es_local=True)
        guia = services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, "GUIA_GENERADA")
        ts = timezone.make_aware(datetime(2026, 9, 24, 19, 52))
        with patch("apps.integraciones.services.marcar_fulfillment") as fulfillment, \
             patch("apps.mensajeria.services.enviar_en_camino") as en_camino, \
             self.captureOnCommitCallbacks(execute=True):
            services._procesar_rastreo(guia, {"estado": "RECOLECTADO", "descripcion": "Picked Up", "ts_evento": ts, "raw": {}}, timezone.now())
        guia.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((guia.estado, guia.ts_recolectado_carrier, pedido.ts_recolectado_carrier), ("RECOLECTADO", ts, ts))
        # Chema 2026-10-12: si el carrier ya la tiene, la salida se registra sola (sin escaneo).
        self.assertEqual(pedido.estado, "RECOLECTADO")
        salida = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="salida_sin_escaneo")
        self.assertEqual((salida.delta["origen"], salida.delta["guia"]), ("carrier", guia.numero))
        self.assertEqual(guia.fecha_compromiso, ts.date() + timedelta(days=1))  # local: día siguiente desde la recolección
        en_camino.assert_called_once_with(pedido)
        fulfillment.assert_called_once()
        self.assertTrue(fulfillment.call_args.kwargs.get("notificar"))
        with patch("apps.integraciones.services.marcar_fulfillment") as fulfillment2, \
             patch("apps.mensajeria.services.enviar_en_camino") as en_camino2, \
             self.captureOnCommitCallbacks(execute=True):
            services._procesar_rastreo(guia, {"estado": "EN_TRANSITO", "descripcion": "In transit", "ts_evento": ts + timedelta(hours=5), "raw": {}}, timezone.now())
        guia.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual(guia.ts_recolectado_carrier, ts)  # segundo evento: no se vuelve a estampar ni a avisar
        self.assertEqual(pedido.estado, "EN_TRANSITO")  # ya salió: el tránsito sí lo mueve
        en_camino2.assert_not_called()
        fulfillment2.assert_not_called()
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="recolectado_por_carrier").exists())
        self.assertEqual(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="salida_sin_escaneo").count(), 1)

    def test_el_carrier_reporta_una_caja_y_solo_esa_sale(self):
        """Chema 2026-10-12: pedido en dos cajas; el carrier reporta la caja 2 → sale
        sola (DESPACHADO, kardex), la 1 sigue en Salida y el pedido queda parcial."""
        from decimal import Decimal

        from apps.envios.models import Paquete, PaqueteLinea
        from apps.inventario.models import Movimiento

        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO", es_local=False)
        from apps.catalogo.models import SKU
        from apps.pedidos.models import LineaPedido
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX-C", descripcion="Six", peso_gr=4000)
        linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=2, reservada=True, cantidad_pickeada=2)
        cajas = [Paquete.objects.create(pedido=pedido, numero=n, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO) for n in (1, 2)]
        for caja in cajas:
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1)
        services.generar_guias(pedido)
        pedido.refresh_from_db()
        c1, c2 = cajas
        c2.refresh_from_db()
        with patch("apps.integraciones.services.marcar_fulfillment"), patch("apps.mensajeria.services.enviar_en_camino"), \
             patch("apps.inventario.services.despachar") as despachar, self.captureOnCommitCallbacks(execute=True):
            services._procesar_rastreo(c2.guia_activa, {"estado": "EN_TRANSITO", "descripcion": "In transit", "ts_evento": timezone.now(), "raw": {}}, timezone.now())
        pedido.refresh_from_db()
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual((c1.estado, c2.estado, pedido.estado), (Paquete.EMPACADO, Paquete.DESPACHADO, "PARCIALMENTE_DESPACHADO"))
        despachar.assert_called_once_with(sku, 2, pedido.folio)  # la unidad de venta sale del kardex con la primera caja
        salida = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="salida_sin_escaneo")
        self.assertEqual((salida.delta["origen"], salida.delta["caja"]), ("carrier", 2))
        self.assertFalse(Movimiento.objects.filter(tipo=Movimiento.SALIDA).exists())  # despachar está parchado: solo se verifica la llamada

    def test_sin_guia_lista_el_carrier_solo_avisa(self):
        """Pedido todavía EMPACADO (sin todas las guías): no hay salida automática; el aviso de siempre."""
        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO", es_local=True)
        guia = services.generar_guia(pedido)
        from apps.pedidos.models import Pedido
        Pedido.objects.filter(pk=pedido.pk).update(estado="EMPACADO")
        pedido.refresh_from_db()
        with patch("apps.integraciones.services.marcar_fulfillment") as fulfillment, patch("apps.mensajeria.services.enviar_en_camino") as en_camino, \
             self.captureOnCommitCallbacks(execute=True):
            services._procesar_rastreo(guia, {"estado": "RECOLECTADO", "descripcion": "Picked Up", "ts_evento": timezone.now(), "raw": {}}, timezone.now())
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, "EMPACADO")
        en_camino.assert_called_once_with(pedido)
        fulfillment.assert_called_once()
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="salida_sin_escaneo").exists())

    def test_pedido_rezagado_con_guia_ya_entregada_se_cierra_en_el_siguiente_poll(self):
        """2026-09-25 (PED-00053): salió sin manifiesto, el carrier la entregó y
        luego se registró la salida; sin cambio de estado de la guía el pedido
        se quedaba RECOLECTADO para siempre."""
        pedido, guia = self._pedido_recolectado()
        Guia.objects.filter(pk=guia.pk).update(estado="ENTREGADO", ultimo_evento="Delivered")
        guia.refresh_from_db()
        services._procesar_rastreo(guia, {"estado": "ENTREGADO", "descripcion": "Delivered", "raw": {}}, timezone.now())
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, "ENTREGADO")
        self.assertIsNotNone(pedido.ts_entregado)

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

    def test_una_guia_que_se_mueve_pero_llega_tarde_tambien_es_retraso(self):
        from apps.incidencias.models import Incidencia

        pedido, guia = self._pedido_recolectado()
        self._con_compromiso(guia, dias=-1)
        self.adapter.avanzar_estado(guia.numero, "EN_TRANSITO")
        services.poll_tracking()  # el mismo poll trae movimiento nuevo y aun así está tarde
        guia.refresh_from_db()
        self.assertEqual(guia.estado, Guia.EN_TRANSITO)
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo="RET").exists())


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

    def test_la_caja_que_regresa_despues_de_que_las_demas_se_entregaron_cierra_el_pedido(self):
        """PED-00255 (2026-10-12): 3 cajas entregadas y la 4a regresó después;
        el pedido se quedaba EN_TRANSITO para siempre."""
        self.pedido.transicionar("RECOLECTADO", motivo="Manifiesto (test)")
        self.assertEqual(self._poll(self.g1, "ENTREGADO"), "EN_TRANSITO")
        self.assertEqual(self._poll(self.g2, "RETORNO"), "ENTREGADO")
        self.pedido.refresh_from_db()
        self.assertIsNotNone(self.pedido.ts_entregado)

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
