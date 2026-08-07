"""Escenario base compartido por los tests de inventario."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente, PerfilUsuario
from apps.inventario.models import Saldo


class InventarioTestCase(TestCase):
    """Cliente + SKU + ubicaciones listas para probar los servicios."""

    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", buffer_stock=0,
        )
        self.sku = SKU.objects.create(
            cliente=self.cliente,
            codigo="COLIMITA-SIX",
            descripcion="Colimita Lager six pack",
            peso_gr=2500,
            precio_declarado=Decimal("180.00"),
            requiere_lote=False,
        )
        self.ubic_recepcion = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.ubic_picking = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.ubic_retorno = Ubicacion.objects.create(codigo="RET-01", tipo=Ubicacion.RETORNO)

    # ── Helpers ──

    def poner_vendible(self, cantidad, sku=None, lote=None, ubicacion=None):
        """Siembra saldo vendible directo (arrange; los servicios se prueban aparte)."""
        return Saldo.objects.create(
            sku=sku or self.sku,
            ubicacion=ubicacion or self.ubic_picking,
            lote=lote,
            estado=Saldo.UBICADO_VENDIBLE,
            cantidad=cantidad,
        )

    def crear_firmante(self, username, rol, pin):
        user = get_user_model().objects.create_user(username=username, password="x12345678")
        PerfilUsuario.objects.create(usuario=user, rol=rol, pin=pin)
        return user

    def crear_lote(self, codigo, fecha_caducidad=None, sku=None):
        return Lote.objects.create(sku=sku or self.sku, codigo=codigo, fecha_caducidad=fecha_caducidad)

    def suma(self, estado, sku=None):
        from django.db.models import Sum
        total = Saldo.objects.filter(sku=sku or self.sku, estado=estado).aggregate(t=Sum("cantidad"))["t"]
        return total or 0
