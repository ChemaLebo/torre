"""Cajas de empaque (B1): tara honesta en el esperado + dims manuales."""
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.catalogo.models import Caja, SKU
from apps.core.models import Cliente
from apps.envios.models import Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def foto(nombre="contenido.jpg"):
    return SimpleUploadedFile(nombre, b"bytes", content_type="image/jpeg")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TaraCajaTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Tara Demo", slug="tara-demo", integracion_envios="envia",
        )
        self.sku = SKU.objects.create(
            cliente=self.cliente, codigo="P1", descripcion="Producto", peso_gr=1000,
        )
        self.caja = Caja.objects.create(
            cliente=self.cliente, nombre="Mediana",
            largo_cm=30, ancho_cm=20, alto_cm=15, peso_gr=200,
        )
        self.pedido = Pedido.objects.create(
            cliente=self.cliente, origen="manual", estado=Pedido.EN_PICKING,
            comprador_nombre="Ana", cp="01780", peso_esperado_gr=2000,
        )
        self.linea = LineaPedido.objects.create(
            pedido=self.pedido, sku=self.sku, cantidad=2,
            cantidad_pickeada=2, reservada=True,
        )
        self.paquete = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("2.10"),
            carrier="fedex", servicio="ground",
        )
        PaqueteLinea.objects.create(
            paquete=self.paquete, linea_pedido=self.linea, cantidad=2,
        )

    @override_settings(TORRE_PESO_MODO="bloquear")
    def test_esperado_incluye_la_tara_de_la_caja(self):
        # neto 2000 + tara 200 = 2200 exactos: pasa. Sin tara habría sido 10%.
        with patch("apps.inventario.services.confirmar_pick"):
            services.empacar_caja(self.paquete, None, 2200, foto(), caja=self.caja)
        self.paquete.refresh_from_db()
        self.assertEqual(self.paquete.caja, self.caja)
        self.assertEqual(self.paquete.peso_real_gr, 2200)

    @override_settings(TORRE_PESO_MODO="bloquear")
    def test_sin_caja_el_esperado_es_solo_el_neto(self):
        with self.assertRaises(ValueError):
            services.empacar_caja(self.paquete, None, 2200, foto())  # 10% arriba

    def test_dims_manuales_sobrescriben_el_plan(self):
        # 3 cajas encintadas = un bulto con medidas propias, tal cual a la guía.
        with patch("apps.inventario.services.confirmar_pick"):
            services.empacar_caja(self.paquete, None, 2050, foto(), dims=(50, 40, 30))
        self.paquete.refresh_from_db()
        self.assertEqual(
            (self.paquete.largo_cm, self.paquete.ancho_cm, self.paquete.alto_cm),
            (50, 40, 30),
        )

    def test_caja_elegida_prellena_pero_dims_manuales_ganan(self):
        with patch("apps.inventario.services.confirmar_pick"):
            services.empacar_caja(
                self.paquete, None, 2200, foto(), caja=self.caja, dims=(60, 50, 40),
            )
        self.paquete.refresh_from_db()
        self.assertEqual(self.paquete.caja, self.caja)
        self.assertEqual(self.paquete.largo_cm, 60)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), TORRE_PESO_MODO="bloquear")
class TaraManualTests(TaraCajaTests):
    def test_tara_manual_sin_caja_del_catalogo(self):
        # Bulto especial: sin caja elegida, la tara tecleada entra al esperado.
        with patch("apps.inventario.services.confirmar_pick"):
            services.empacar_caja(self.paquete, None, 2350, foto(), tara_gr=350)
        self.paquete.refresh_from_db()
        self.assertEqual(self.paquete.peso_real_gr, 2350)  # 2000 neto + 350 manual

    def test_tara_manual_le_gana_a_la_de_la_caja(self):
        with patch("apps.inventario.services.confirmar_pick"):
            services.empacar_caja(
                self.paquete, None, 2500, foto(), caja=self.caja, tara_gr=500,
            )
        self.paquete.refresh_from_db()
        self.assertEqual(self.paquete.caja, self.caja)  # la caja se guarda igual
