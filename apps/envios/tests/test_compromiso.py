"""Compromiso de entrega (Chema 2026-09-25): días prometidos por el carrier al
comprar la guía, fecha límite estampada al salir de bodega, lunes a sábado
(iMile todos los días) y local = día siguiente."""
from datetime import date, datetime

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.envios.models import Guia
from apps.envios.services import (
    dias_desde_estimado, dias_promesa_de, estampar_compromiso, fecha_compromiso,
)

from .base import crear_cliente, crear_pedido, crear_tienda


def _dt(y, m, d, h=12):
    return timezone.make_aware(datetime(y, m, d, h))


class CompromisoTests(TestCase):
    def test_dias_desde_estimado(self):
        self.assertEqual(dias_desde_estimado("2-4 días"), 4)
        self.assertEqual(dias_desde_estimado("4 días · llega 2026-09-28"), 4)
        self.assertEqual(dias_desde_estimado("1-2 days"), 2)
        self.assertEqual(dias_desde_estimado("1 día"), 1)
        self.assertEqual(dias_desde_estimado("next day"), 1)
        self.assertIsNone(dias_desde_estimado(""))
        self.assertIsNone(dias_desde_estimado("sin dato"))

    def test_calendario_lunes_a_sabado_y_todos_los_dias(self):
        sabado = _dt(2026, 9, 26)  # sábado
        self.assertEqual(fecha_compromiso(sabado, 1, "noventa9Minutos"), date(2026, 9, 28))  # el domingo no cuenta
        self.assertEqual(fecha_compromiso(sabado, 1, "imile"), date(2026, 9, 27))  # iMile entrega en domingo
        self.assertEqual(fecha_compromiso(_dt(2026, 9, 24), 4, "estafeta"), date(2026, 9, 29))  # jue + 4 hábiles = mar
        self.assertEqual(fecha_compromiso(_dt(2026, 9, 24), 0, "estafeta"), date(2026, 9, 24))

    @override_settings(TORRE={**__import__("django.conf").conf.settings.TORRE, "DIAS_PROMESA_FORANEO_DEFAULT": 5})
    def test_dias_promesa_local_siempre_dia_siguiente_y_foraneo_del_cotizador(self):
        cliente = crear_cliente()
        tienda = crear_tienda(cliente)
        local = crear_pedido(cliente, tienda, es_local=True)
        foraneo = crear_pedido(cliente, tienda, es_local=False)

        class Caja:
            estimado_entrega = "2-4 días"

        self.assertEqual(dias_promesa_de(local, "estafeta", Caja()), 1)
        self.assertEqual(dias_promesa_de(foraneo, "estafeta", Caja()), 4)
        self.assertEqual(dias_promesa_de(foraneo, "estafeta", None), 5)

    def test_estampar_al_salir_es_idempotente(self):
        cliente = crear_cliente()
        tienda = crear_tienda(cliente)
        pedido = crear_pedido(cliente, tienda, es_local=False)
        guia = Guia.objects.create(pedido=pedido, carrier="noventa9Minutos", numero="1", proveedor="mock", dias_promesa=4)
        self.assertEqual(estampar_compromiso(guia, _dt(2026, 9, 24)), date(2026, 9, 29))
        self.assertEqual(estampar_compromiso(guia, _dt(2026, 10, 1)), date(2026, 9, 29))  # ya estampada: no se mueve
        guia.refresh_from_db()
        self.assertEqual(guia.fecha_compromiso, date(2026, 9, 29))
        cancelada = Guia.objects.create(pedido=pedido, carrier="estafeta", numero="2", proveedor="mock", estado=Guia.CANCELADA)
        self.assertIsNone(estampar_compromiso(cancelada, _dt(2026, 9, 24)))
