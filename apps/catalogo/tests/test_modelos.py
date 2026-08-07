"""Catálogo: unicidad multi-tenant del código de SKU y defaults."""
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente


class SKUTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")

    def test_codigo_unico_por_cliente(self):
        SKU.objects.create(cliente=self.colima, codigo="TICUS-SIX", descripcion="Ticús six")
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                SKU.objects.create(cliente=self.colima, codigo="TICUS-SIX", descripcion="Duplicado")

    def test_mismo_codigo_en_otro_cliente_es_valido(self):
        """Multi-tenant: el código vive en el espacio de SU cliente."""
        SKU.objects.create(cliente=self.colima, codigo="SIX-01", descripcion="Cerveza")
        otro = SKU.objects.create(cliente=self.nocturno, codigo="SIX-01", descripcion="Mezcal")
        self.assertEqual(otro.codigo, "SIX-01")

    def test_defaults_operativos(self):
        sku = SKU.objects.create(cliente=self.colima, codigo="PARAMO-SIX", descripcion="Páramo six")
        self.assertTrue(sku.requiere_lote)
        self.assertFalse(sku.backorder_habilitado)
        self.assertTrue(sku.activo)
        self.assertEqual(sku.unidad, "pieza")


class UbicacionYLoteTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="CAYACO-SIX", descripcion="Cayaco six")

    def test_codigo_de_ubicacion_unico(self):
        Ubicacion.objects.create(codigo="SAL-PQX", tipo=Ubicacion.SALIDA)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Ubicacion.objects.create(codigo="SAL-PQX", tipo=Ubicacion.SALIDA)

    def test_lote_unico_por_sku(self):
        Lote.objects.create(sku=self.sku, codigo="L-2026-01")
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Lote.objects.create(sku=self.sku, codigo="L-2026-01")
