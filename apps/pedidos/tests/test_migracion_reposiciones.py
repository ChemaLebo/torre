"""Migración pedidos 0020 (Chema 2026-10-05): las líneas de reposición del
modelo viejo (LineaPedido.reposicion_de) se funden en su línea original —
repuestas, repuestas con stock, pickeadas y despachadas— y sus renglones de
caja pasan a la original apuntando (repone_a) a la caja despachada cuya guía
quedó "sustituida"; la línea de reposición desaparece."""
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.test import TestCase

from apps.catalogo.models import SKU
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.pedidos.models import LineaPedido


class FundirReposicionesTests(TestCase):
    def test_funde_la_linea_de_reposicion_en_la_original_y_apunta_a_la_caja_sustituida(self):
        fundir = import_module("apps.pedidos.migrations.0020_fundir_lineas_de_reposicion").fundir
        cliente = crear_cliente()
        pedido = crear_pedido(cliente, crear_tienda(cliente), estado="PENDIENTE")
        sku = SKU.objects.create(cliente=cliente, codigo="CCPL24L", descripcion="24 pack", peso_gr=9000)
        # Original: 2 piezas, una en cada caja despachada; la guía de la caja 2 quedó sustituida.
        orig = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=2, reservada=True, cantidad_pickeada=2, cantidad_despachada=2)
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("9"), carrier="imile", estado=Paquete.DESPACHADO)
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("9"), carrier="imile", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=orig, cantidad=1)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=orig, cantidad=1)
        Guia.objects.create(pedido=pedido, paquete=c1, carrier="imile", numero="IM-1", estado=Guia.ENTREGADO)
        Guia.objects.create(pedido=pedido, paquete=c2, carrier="imile", numero="IM-2", estado=Guia.EN_TRANSITO, sustituida_motivo="extraviada")
        # Reposición (modelo viejo): línea nueva de 1 pieza, pickeada, en su caja 3 ya planeada.
        rep = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=1, reservada=True, cantidad_pickeada=1, reposicion_de=orig)
        c3 = Paquete.objects.create(pedido=pedido, numero=3, peso_kg=Decimal("9"), carrier="99minutos", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=c3, linea_pedido=rep, cantidad=1)
        # Otra reposición sin stock (faltante): suma repuestas pero no reservadas.
        otra = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=1, reservada=False, reposicion_de=orig)

        fundir(apps, None)

        orig.refresh_from_db()
        self.assertFalse(LineaPedido.objects.filter(pk__in=[rep.pk, otra.pk]).exists())
        self.assertEqual(pedido.lineas.count(), 1)
        self.assertEqual(
            (orig.cantidad, orig.cantidad_repuesta, orig.cantidad_repuesta_reservada, orig.cantidad_pickeada, orig.cantidad_despachada),
            (2, 2, 1, 3, 2),
        )
        self.assertEqual((orig.por_surtir, orig.con_stock, orig.pendiente, orig.faltante), (4, 3, 0, True))
        fila = PaqueteLinea.objects.get(paquete=c3)
        self.assertEqual((fila.linea_pedido_id, fila.cantidad, fila.repone_a_id), (orig.pk, 1, c2.pk))  # la caja con guía sustituida
        self.assertEqual(orig.origen_reposicion, [
            {"caja": c2.pk, "numero": 2, "cantidad": 1}, {"caja": c2.pk, "numero": 2, "cantidad": 1},
        ])
        self.assertEqual((c3.es_reposicion, c3.cajas_que_repone, c2.cajas_de_reposicion), (True, [2], [3]))
