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


class OpcionesAgrupadasTests(TestCase):
    """El dropdown distingue variantes del mismo producto."""

    def test_etiqueta_incluye_la_variante_cuando_existe(self):
        from apps.core.models import Cliente

        from ..models import SKU, opciones_sku_agrupadas

        cliente = Cliente.objects.create(nombre="Infinitea", slug="infinitea")
        SKU.objects.create(cliente=cliente, codigo="TN-500", descripcion="Té negro", variante="500 g")
        SKU.objects.create(cliente=cliente, codigo="TN-1K", descripcion="Té negro", variante="1 kg")
        SKU.objects.create(cliente=cliente, codigo="CHAI", descripcion="Chai masala")
        etiquetas = [texto for _, opciones in opciones_sku_agrupadas(cliente) for _, texto in opciones]
        self.assertIn("Té negro (500 g) — TN-500", etiquetas)
        self.assertIn("Té negro (1 kg) — TN-1K", etiquetas)
        self.assertIn("Chai masala — CHAI", etiquetas)

    def test_excluir_kits_los_saca_del_dropdown(self):
        from apps.core.models import Cliente

        from ..models import SKU, opciones_sku_agrupadas

        cliente = Cliente.objects.create(nombre="Infinitea 2", slug="infinitea-2")
        SKU.objects.create(cliente=cliente, codigo="TEABOX", descripcion="TeaBox", es_kit=True)
        SKU.objects.create(cliente=cliente, codigo="CHAI", descripcion="Chai")
        con_kits = [t for _, ops in opciones_sku_agrupadas(cliente) for _, t in ops]
        sin_kits = [t for _, ops in opciones_sku_agrupadas(cliente, excluir_kits=True) for _, t in ops]
        self.assertTrue(any("TEABOX" in t for t in con_kits))
        self.assertFalse(any("TEABOX" in t for t in sin_kits))
        self.assertTrue(any("CHAI" in t for t in sin_kits))


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
