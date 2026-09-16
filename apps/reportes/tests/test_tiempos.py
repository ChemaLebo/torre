"""Tiempos logísticos: horas por paso, eventos del carrier y promedios por
estado destino, zona y paquetería."""
from datetime import timedelta

from django.utils import timezone

from apps.envios.models import EventoGuia, Guia
from apps.pedidos.models import Pedido
from apps.reportes import tiempos
from apps.reportes.base import limites

from .base import ReportesTestCase


class TiemposTests(ReportesTestCase):
    def setUp(self):
        self.t0 = timezone.now() - timedelta(days=3)
        hoy = timezone.localdate()
        self.inicio, self.fin = limites(hoy - timedelta(days=7), hoy)

    def _pedido(self, cp, carrier, horas_bodega, horas_transito=None, **extra):
        creado = self.t0
        salida = creado + timedelta(hours=horas_bodega)
        entregado = salida + timedelta(hours=horas_transito) if horas_transito is not None else None
        pedido = Pedido.objects.create(
            cliente=self.colima, comprador_nombre="Ana", cp=cp,
            estado=Pedido.ENTREGADO if entregado else Pedido.EN_TRANSITO, **extra,
        )
        Pedido.objects.filter(pk=pedido.pk).update(
            creado=creado, ts_picking=creado + timedelta(hours=1), ts_empacado=creado + timedelta(hours=2),
            ts_guia=creado + timedelta(hours=2, minutes=5), ts_recolectado=salida,
            ts_en_transito=salida + timedelta(hours=1), ts_entregado=entregado,
        )
        guia = Guia.objects.create(pedido=pedido, carrier=carrier, numero=f"G-{pedido.pk}", proveedor="mock",
                                   estado=Guia.ENTREGADO if entregado else Guia.EN_TRANSITO)
        return Pedido.objects.get(pk=pedido.pk), guia

    def test_filas_con_horas_y_eventos_del_carrier(self):
        pedido, guia = self._pedido("44100", "estafeta", horas_bodega=4, horas_transito=30)
        EventoGuia.objects.create(guia=guia, estado=Guia.RECOLECTADO, crudo="pickup", ts_carrier=pedido.ts_recolectado + timedelta(hours=2))
        EventoGuia.objects.create(guia=guia, estado=Guia.EN_RUTA, crudo="out", ts_carrier=pedido.ts_entregado - timedelta(hours=3))
        EventoGuia.objects.create(guia=guia, estado=Guia.ENTREGADO, crudo="ok", ts_carrier=pedido.ts_entregado)
        [fila] = tiempos.generar(self.colima, self.inicio, self.fin, {"solo_entregados": False}, es_mesa=False)["filas"]
        self.assertEqual(fila[:4], [pedido.folio, "estafeta", "Jalisco", "metro"])
        self.assertEqual(fila[9], pedido.ts_recolectado + timedelta(hours=2))  # 1er evento paquetería
        self.assertEqual(fila[11], pedido.ts_entregado - timedelta(hours=3))  # en ruta
        self.assertEqual(fila[13:], [4.0, 30.0, 34.0])

    def test_promedios_por_estado_zona_y_paqueteria(self):
        self._pedido("44100", "estafeta", horas_bodega=4, horas_transito=30)
        self._pedido("06600", "noventa9Minutos", horas_bodega=2, horas_transito=10)
        self._pedido("06700", "noventa9Minutos", horas_bodega=6)  # en tránsito, sin entrega
        Pedido.objects.create(cliente=self.colima, comprador_nombre="X", cp="44100", estado=Pedido.CANCELADO)
        r = tiempos.generar(self.colima, self.inicio, self.fin, {"solo_entregados": False}, es_mesa=False)
        self.assertEqual(len(r["filas"]), 3)
        self.assertIn(("entregados", 2), r["resumen"])
        por_carrier = {f[0]: f for f in r["grupos"][2]["filas"]}
        self.assertEqual(por_carrier["noventa9Minutos"][1:], [2, 1, 4.0, 10.0, 12.0])
        self.assertEqual(por_carrier["estafeta"][1:], [1, 1, 4.0, 30.0, 34.0])
        por_zona = {f[0]: f for f in r["grupos"][1]["filas"]}
        self.assertEqual(por_zona["local"][1], 2)
        por_estado = {f[0]: f for f in r["grupos"][0]["filas"]}
        self.assertEqual(set(por_estado), {"Jalisco", "Ciudad de México"})
        solo = tiempos.generar(self.colima, self.inicio, self.fin, {"solo_entregados": True}, es_mesa=False)
        self.assertEqual(len(solo["filas"]), 2)
