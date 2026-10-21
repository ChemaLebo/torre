"""Relleno de Paquete.ts_etiquetas / ts_listo_salida (migración envios 0030):
toda caja con cierre recibe ambos sellos con la hora del cierre; sin cierre, nada."""
from decimal import Decimal

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from apps.envios.models import Paquete

from .base import crear_cliente, crear_pedido, crear_tienda


class RellenoTandaTests(TestCase):
    def test_estampa_los_sellos_desde_el_cierre(self):
        from importlib import import_module
        desde_cierre = import_module("apps.envios.migrations.0030_paquete_tanda").desde_cierre

        cliente = crear_cliente()
        pedido = crear_pedido(cliente, crear_tienda(cliente))
        cierre = timezone.now()
        cerrada = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("2"), carrier="estafeta", ts_cierre=cierre)
        abierta = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("2"), carrier="estafeta")
        fuera = Paquete.objects.create(pedido=pedido, numero=3, peso_kg=Decimal("2"), carrier="estafeta", estado=Paquete.DESPACHADO)
        ya = Paquete.objects.create(pedido=pedido, numero=4, peso_kg=Decimal("2"), carrier="estafeta", ts_cierre=cierre, ts_etiquetas=cierre, ts_listo_salida=cierre)

        desde_cierre(apps, None)
        for caja in (cerrada, abierta, fuera, ya):
            caja.refresh_from_db()
        self.assertEqual((cerrada.ts_etiquetas, cerrada.ts_listo_salida), (cierre, cierre))
        self.assertEqual((abierta.ts_etiquetas, abierta.ts_listo_salida), (None, None))
        self.assertEqual((fuera.ts_etiquetas, fuera.ts_listo_salida), (None, None))
        self.assertEqual((ya.ts_etiquetas, ya.ts_listo_salida), (cierre, cierre))  # idempotente
