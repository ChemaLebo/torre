"""Fixtures compartidos de los reportes: cliente con tienda, SKUs, ubicación
de picking, usuarios de Mesa y portal."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, PerfilUsuario
from apps.integraciones.models import Tienda


def crear_usuario(username, rol, cliente=None):
    usuario = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=usuario, rol=rol, cliente=cliente)
    return usuario


class ReportesTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        cls.otro = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno", integracion_envios="envia")
        cls.tienda = Tienda.objects.create(cliente=cls.colima, dominio="colima-mx.myshopify.com")
        cls.six = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six",
            peso_gr=4000, precio_declarado=Decimal(300), requiere_lote=True,
        )
        cls.caja = SKU.objects.create(
            cliente=cls.colima, codigo="PARAMO-C12", descripcion="Páramo caja 12",
            peso_gr=5700, precio_declarado=Decimal(390),
        )
        cls.ajeno = SKU.objects.create(
            cliente=cls.otro, codigo="MEZ-750", descripcion="Mezcal 750", peso_gr=1200,
            precio_declarado=Decimal(900),
        )
        cls.picking = Ubicacion.objects.create(codigo="A-01-1", tipo="picking")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.piso = crear_usuario("piso1", "piso")
