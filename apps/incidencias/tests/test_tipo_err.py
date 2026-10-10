"""Producto erróneo (`ERR`, Chema 2026-10-10): tipo nuevo, P1, con el producto
que llegó en su lugar en `sku`; el portal lo redacta para el personal del
cliente y los avisos lo nombran en palabras."""
from django.test import TestCase

from apps.catalogo.models import SKU
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.mensajeria.services import TIPO_INCIDENCIA_LEGIBLE
from apps.portal.forms import TIPOS_INCIDENCIA_PORTAL

from .utils import crear_cliente, crear_pedido


class TipoProductoErroneoTests(TestCase):
    def test_nace_p1_visible_al_cliente_y_con_el_producto_que_llego(self):
        cliente = crear_cliente()
        pedido = crear_pedido(cliente)
        otro = SKU.objects.create(cliente=cliente, codigo="C12", descripcion="Caja 12")
        inc = abrir_incidencia(cliente, Incidencia.TIPO_ERR, Incidencia.ORIGEN_CLIENTE, pedido=pedido, sku=otro, texto="Otro producto")
        self.assertEqual((inc.prioridad, inc.interna, inc.sku, inc.get_tipo_display()), ("P1", False, otro, "Producto erróneo"))

    def test_el_portal_y_los_avisos_lo_nombran_para_el_cliente(self):
        self.assertEqual(dict(TIPOS_INCIDENCIA_PORTAL)[Incidencia.TIPO_ERR], "Le llegó un producto distinto al comprador")
        self.assertEqual(TIPO_INCIDENCIA_LEGIBLE["ERR"], "producto erróneo")
        self.assertIn(Incidencia.TIPO_ERR, dict(Incidencia.TIPOS))
