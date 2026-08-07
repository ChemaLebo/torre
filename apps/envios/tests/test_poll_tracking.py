"""poll_tracking: sincronización de pedido, incidencias RF/RET y umbrales."""
from datetime import timedelta

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
