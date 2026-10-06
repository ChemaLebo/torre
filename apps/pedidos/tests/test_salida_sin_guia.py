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

    def test_reposicion_con_una_caja_ya_entregada_solo_replanea_la_nueva(self):
        """El caso de PED-00045 (Chema 2026-09-28): caja 1 entregada con carrier;
        la reposición sale sin guía y la caja 1 no se toca."""
        from decimal import Decimal as D

        from apps.envios.models import Guia, PaqueteLinea
        from apps.pedidos.models import LineaPedido

        pedido = self.crear_pedido(cantidad=2, es_local=False, estado=Pedido.ENTREGADO)
        linea = pedido.lineas.get()
        LineaPedido.objects.filter(pk=linea.pk).update(cantidad_pickeada=2)
        caja1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=D("4"), carrier="estafeta",
                                       estado=Paquete.DESPACHADO, precio_cotizado=D("150"))
        PaqueteLinea.objects.create(paquete=caja1, linea_pedido=linea, cantidad=2)
        Guia.objects.create(pedido=pedido, paquete=caja1, carrier="estafeta", numero="EST-1", proveedor="mock",
                            estado=Guia.ENTREGADO)
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 1)], self.mesa)
        pedido.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            resultado = services.replanear_con_carrier(pedido, "local", self.mesa)
        caja1.refresh_from_db()
        self.assertEqual((caja1.carrier, caja1.estado, caja1.precio_cotizado), ("estafeta", Paquete.DESPACHADO, D("150")))
        self.assertEqual([c.carrier for c in resultado["cajas"]], ["local"])
        nueva = resultado["cajas"][0]
        # El replaneo conserva el origen: el renglón nuevo sigue apuntando a la caja 1 y a la MISMA línea.
        self.assertEqual([(pl.linea_pedido_id, pl.cantidad, pl.repone_a_id) for pl in nueva.lineas.all()], [(linea.pk, 1, caja1.pk)])
        self.assertEqual(pedido.paquetes.count(), 2)

    def test_con_algo_en_la_calle_no_se_cambia(self):
        pedido = self.crear_pedido(cantidad=1, es_local=False, estado=Pedido.RECOLECTADO)
        with self.assertRaises(ValueError):
            services.replanear_con_carrier(pedido, "local", self.mesa)
        # Pendiente pero ya salió entero (sin nada por surtir): tampoco.
        entero = self.crear_pedido(cantidad=1, es_local=False, estado=Pedido.PENDIENTE)
        entero.lineas.update(cantidad_despachada=1)
        with self.assertRaises(ValueError):
            services.replanear_con_carrier(entero, "local", self.mesa)
