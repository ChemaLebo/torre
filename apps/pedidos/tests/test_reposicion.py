"""Reposición de producto (Chema 2026-09-28): líneas nuevas en el MISMO pedido
ligadas a la original, reserva de stock (sin stock: faltante que espera), el
pedido regresa a PENDIENTE sin dueño con un plan solo de lo repuesto, lo que ya
salió queda estampado como despachado, y al entregarse la reposición la
compensación aprobada se paga sola."""
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Compensacion, Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.inventario.services import recibir, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


@override_settings(ENVIA_API_KEY="", MEDIA_ROOT=tempfile.mkdtemp(prefix="torre-repo-"))
class ReposicionTests(TestCase):
    """Bodega mínima: A con 10 piezas ubicadas, B sin existencias."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.a = SKU.objects.create(cliente=self.cliente, codigo="A-SIX", descripcion="Six A", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        self.b = SKU.objects.create(cliente=self.cliente, codigo="B-SIX", descripcion="Six B", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.mesa = get_user_model().objects.create_user(username="mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.a, cantidad_anunciada=10)
        recibir(linea, 10, 0, self.mesa)
        ubicar(self.a, 10, self.pic, None, self.mesa)

    def pedido_entregado(self):
        """A × 2 y B × 1 entregados en una caja; sin cantidad_despachada (anterior al parcial)."""
        pedido = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana",
                                       cp="01780", estado=Pedido.ENTREGADO, asignado_a=self.mesa)
        la = LineaPedido.objects.create(pedido=pedido, sku=self.a, cantidad=2, reservada=True, cantidad_pickeada=2)
        lb = LineaPedido.objects.create(pedido=pedido, sku=self.b, cantidad=1, reservada=True, cantidad_pickeada=1)
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("6"), carrier="estafeta",
                                      estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=la, cantidad=2)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=lb, cantidad=1)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero="EST-1", proveedor="mock",
                            estado=Guia.ENTREGADO)
        return pedido, la, lb

    def test_repone_lo_elegido_y_regresa_el_pedido_a_picking(self):
        pedido, la, lb = self.pedido_entregado()
        with self.captureOnCommitCallbacks(execute=True):
            nuevas = services.reponer_lineas(pedido, [(la, 1), (lb, 1)], self.mesa)
        pedido.refresh_from_db()
        la.refresh_from_db()
        lb.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertIsNone(pedido.asignado_a)
        self.assertEqual([(n.sku.codigo, n.cantidad, n.reposicion_de_id, n.reservada) for n in nuevas],
                         [("A-SIX", 1, la.pk, True), ("B-SIX", 1, lb.pk, False)])  # B sin stock: faltante
        self.assertTrue(nuevas[0].es_reposicion)
        # Lo que ya salió queda como despachado: el plan no lo repite.
        self.assertEqual((la.cantidad_despachada, lb.cantidad_despachada), (2, 1))
        self.assertEqual([l.pk for l in pedido.lineas_por_surtir], [nuevas[0].pk])
        self.assertTrue(pedido.pendiente_de_completar)
        planeadas = list(Paquete.objects.filter(pedido=pedido, estado=Paquete.PLANEADO))
        self.assertEqual(len(planeadas), 1)
        self.assertEqual([(pl.linea_pedido_id, pl.cantidad) for pl in planeadas[0].lineas.all()], [(nuevas[0].pk, 1)])
        self.assertEqual(Paquete.objects.filter(pedido=pedido, estado=Paquete.DESPACHADO).count(), 1)  # la caja vieja sigue
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="reposicion")
        self.assertEqual(evento.delta["sin_stock"], ["B-SIX"])

    def test_solo_desde_la_calle_o_entregado_y_nunca_mas_piezas_que_las_pedidas(self):
        pedido, la, _ = self.pedido_entregado()
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 3)], self.mesa)
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 0)], self.mesa)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EMPACADO)
        pedido.refresh_from_db()
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 1)], self.mesa)
        self.assertEqual(pedido.lineas.count(), 2)  # nada cambió

    def test_al_entregar_la_reposicion_la_compensacion_se_paga_sola(self):
        from apps.envios.services import _transicionar_pedido

        pedido, la, _ = self.pedido_entregado()
        inc = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=pedido, texto="Rota")
        comp = Compensacion.objects.create(incidencia=inc, tipo=Compensacion.TIPO_REPOSICION, monto=Decimal(180),
                                           lineas=[{"linea_id": la.pk, "sku": "A-SIX", "cantidad": 1}],
                                           estado=Compensacion.APROBADA)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EN_TRANSITO)
        pedido.refresh_from_db()
        self.assertTrue(_transicionar_pedido(pedido, "ENTREGADO", motivo="Entregado"))
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.PAGADA)
        self.assertEqual(comp.referencia_pago, f"{pedido.folio} entregado")
        self.assertIsNotNone(comp.fecha_pago)
        self.assertTrue(inc.mensajes.filter(texto__contains="se entregó").exists())
