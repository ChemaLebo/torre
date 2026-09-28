"""Salida sin guía de carrier (Chema 2026-09-28): Mesa fuerza "local" para un
pedido (reposiciones que entrega el cliente o alguien propio) aunque no haya
flota propia ni sea local: cajas con guía interna LOCAL-* a la tarifa local,
corral SAL-LOCAL, sin cotizar a nadie."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.core.models import PerfilUsuario
from apps.envios.models import Paquete
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class SalidaSinGuiaTests(PisoTestCase):
    def setUp(self):
        self.crear_stock(cantidad=20)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def test_pendiente_foraneo_se_planea_local_y_su_guia_es_interna(self):
        pedido = self.crear_pedido(cantidad=2, es_local=False)
        with self.captureOnCommitCallbacks(execute=True):
            resultado = services.replanear_con_carrier(pedido, "local", self.mesa)
        pedido.refresh_from_db()
        self.assertEqual(pedido.carrier_forzado, "local")
        cajas = resultado["cajas"]
        self.assertTrue(cajas)
        self.assertEqual({(c.carrier, c.servicio) for c in cajas}, {("local", "entrega_local")})
        self.assertEqual({c.precio_cotizado for c in cajas}, {Decimal("100")})
        empacado = self.dejar_empacado(pedido)
        guia = services.generar_guia(empacado)
        self.assertTrue(guia.numero.startswith("LOCAL-"))
        self.assertEqual((guia.carrier, guia.proveedor), ("local", "local"))

    def test_caja_ya_empacada_se_recotiza_a_local_sin_cotizar_a_nadie(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2, es_local=False))
        caja = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO,
            precio_cotizado=Decimal("150"),
        )
        resultado = services.replanear_con_carrier(pedido, "local", self.mesa)
        caja.refresh_from_db()
        self.assertEqual(resultado["modo"], "recotizadas")
        self.assertEqual((caja.carrier, caja.servicio, caja.precio_cotizado), ("local", "entrega_local", Decimal("100")))
        self.assertEqual(pedido.paquetes.count(), 1)  # la caja física se queda

    def test_con_algo_en_la_calle_no_se_cambia(self):
        pedido = self.crear_pedido(cantidad=1, es_local=False, estado=Pedido.RECOLECTADO)
        with self.assertRaises(ValueError):
            services.replanear_con_carrier(pedido, "local", self.mesa)
