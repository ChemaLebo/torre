"""Cupo exacto de kits (B2): productos_por_kit + declaración por caja."""
from unittest.mock import patch

from django.test import TestCase

from apps.catalogo.models import SKU
from apps.core.models import Cliente
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


class CupoKitTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Kits", slug="kits-cupo", integracion_envios="envia",
        )
        self.kit = SKU.objects.create(
            cliente=self.cliente, codigo="BOX3", descripcion="Mystery 3",
            es_kit=True, peso_gr=350, productos_por_kit=3,
        )
        self.te = SKU.objects.create(
            cliente=self.cliente, codigo="TE-1", descripcion="Té", peso_gr=100,
        )
        self.te2 = SKU.objects.create(
            cliente=self.cliente, codigo="TE-2", descripcion="Té 2", peso_gr=100,
        )

    def _pedido(self, cajas=1):
        pedido = Pedido.objects.create(
            cliente=self.cliente, origen="manual", estado=Pedido.EN_PICKING,
            comprador_nombre="Ana", cp="01780", peso_esperado_gr=350 * cajas,
        )
        linea = LineaPedido.objects.create(
            pedido=pedido, sku=self.kit, cantidad=cajas,
            cantidad_pickeada=cajas, reservada=True,
        )
        return pedido, linea

    def test_cupo_exacto_rechaza_de_mas_y_de_menos(self):
        _, linea = self._pedido()
        with patch("apps.inventario.services.reservar", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                services.declarar_contenido_kit(linea, [(self.te, 2)], None)
            self.assertIn("3 producto(s)", str(ctx.exception))
            with self.assertRaises(ValueError):
                services.declarar_contenido_kit(linea, [(self.te, 4)], None)
            services.declarar_contenido_kit(linea, [(self.te, 2), (self.te2, 1)], None)
        self.assertEqual(
            sum(h.cantidad for h in linea.componentes.all()), 3,
        )

    def test_stepper_declara_caja_por_caja_y_el_gate_exige_todas(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        pedido, linea = self._pedido(cajas=2)
        with patch("apps.inventario.services.reservar", return_value=True):
            services.declarar_contenido_kit(linea, [(self.te, 3)], None, caja=1)
            # Re-declarar la misma caja truena; la caja 2 aún falta.
            with self.assertRaises(ValueError):
                services.declarar_contenido_kit(linea, [(self.te, 3)], None, caja=1)
            with patch("apps.inventario.services.confirmar_pick"):
                with self.assertRaises(ValueError) as ctx:
                    services.empacar(
                        pedido, None, 950,
                        fotos=[SimpleUploadedFile("f.jpg", b"x", content_type="image/jpeg")],
                    )
            self.assertIn("BOX3 (3/6)", str(ctx.exception))
            services.declarar_contenido_kit(linea, [(self.te2, 3)], None, caja=2)
        hijas = list(linea.componentes.order_by("kit_caja"))
        self.assertEqual([h.kit_caja for h in hijas], [1, 2])

    def test_cupo_cero_sigue_libre(self):
        self.kit.productos_por_kit = 0
        self.kit.save(update_fields=["productos_por_kit"])
        _, linea = self._pedido()
        with patch("apps.inventario.services.reservar", return_value=True):
            services.declarar_contenido_kit(linea, [(self.te, 5)], None)
        self.assertEqual(sum(h.cantidad for h in linea.componentes.all()), 5)

    def test_caja_fuera_de_rango(self):
        _, linea = self._pedido(cajas=1)
        with self.assertRaises(ValueError):
            services.declarar_contenido_kit(linea, [(self.te, 3)], None, caja=2)
