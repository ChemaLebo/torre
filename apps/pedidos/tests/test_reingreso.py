"""Reingreso de mercancía: cancelación en bodega con inventario REAL (lo pickeado
vuelve a put-away como orden de reingreso, el resto libera reserva), decisiones
de Mesa sobre pedidos que ya salieron (registrar_reingreso / marcar_no_recuperado)
y la lista de pedidos por decidir."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia
from apps.incidencias.models import Incidencia
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo
from apps.inventario.services import disponible, recibir, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def suma(sku, estado):
    from django.db.models import Sum
    return Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"] or 0


@override_settings(ENVIA_API_KEY="")
class BaseReingreso(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.sku = SKU.objects.create(
            cliente=self.cliente, codigo="COLIMITA-SIX", descripcion="Colimita", peso_gr=2500,
            precio_declarado=Decimal("180.00"), requiere_lote=False,
        )
        self.rec = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.operador = get_user_model().objects.create_user(username="piso1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.operador, rol="piso", pin="1111")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=10)
        recibir(linea, 10, 0, self.operador)
        ubicar(self.sku, 10, self.pic, None, self.operador)

    def pedido_con_linea(self, cantidad=4, estado=Pedido.PENDIENTE):
        pedido = Pedido.objects.create(
            cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana", cp="01780", estado=estado,
        )
        LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=cantidad)
        return pedido


class CancelarEnBodegaTests(BaseReingreso):
    def test_empacado_manda_lo_pickeado_a_put_away_y_nace_el_reingreso(self):
        pedido = self.pedido_con_linea(4)
        self.assertTrue(services._reservar_linea(pedido.lineas.get()))
        services.iniciar_picking(pedido, self.operador)
        services.confirmar_linea_pick(pedido.lineas.get(), 4, self.operador)
        from django.core.files.uploadedfile import SimpleUploadedFile
        with override_settings(MEDIA_ROOT="/tmp/torre-test-reingreso"):
            services.empacar(pedido, self.operador, 10000, [SimpleUploadedFile("c.jpg", b"x", content_type="image/jpeg")])
        self.assertEqual(suma(self.sku, Saldo.EN_EMPAQUE), 4)
        self.assertEqual(suma(self.sku, Saldo.UBICADO_VENDIBLE), 6)

        services.cancelar(pedido, self.operador, motivo="El cliente ya no lo quiere")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertEqual(suma(self.sku, Saldo.EN_EMPAQUE), 0)
        self.assertEqual(suma(self.sku, Saldo.EN_PUTAWAY), 4)
        self.assertEqual(suma(self.sku, Saldo.CUARENTENA), 0)
        self.assertEqual(disponible(self.sku), 6)
        orden = pedido.reingresos.get()
        self.assertEqual((orden.tipo, orden.estado, orden.pedido_id), ("reingreso", "RECIBIDA", pedido.pk))
        linea = orden.lineas.get()
        self.assertEqual((linea.cantidad_anunciada, linea.cantidad_recibida), (4, 4))
        ubicar(self.sku, 4, self.pic, None, self.operador)
        self.assertEqual(disponible(self.sku), 10)

    def test_en_picking_con_carrito_a_medias(self):
        pedido = self.pedido_con_linea(4)
        self.assertTrue(services._reservar_linea(pedido.lineas.get()))
        services.iniciar_picking(pedido, self.operador)
        services.confirmar_linea_pick(pedido.lineas.get(), 1, self.operador)
        self.assertEqual(disponible(self.sku), 6)

        services.cancelar(pedido, self.operador, motivo="Cancelado a medio pick")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertEqual(suma(self.sku, Saldo.UBICADO_VENDIBLE), 9)
        self.assertEqual(suma(self.sku, Saldo.RESERVADO), 0)
        self.assertEqual(suma(self.sku, Saldo.EN_PUTAWAY), 1)
        self.assertEqual(disponible(self.sku), 9)
        self.assertEqual(pedido.reingresos.get().lineas.get().cantidad_recibida, 1)

    def test_guia_activa_se_cancela_con_el_carrier(self):
        pedido = self.pedido_con_linea(2, estado=Pedido.GUIA_GENERADA)
        Guia.objects.create(pedido=pedido, carrier="estafeta", numero="MOCK-0001", proveedor="mock")
        services.cancelar(pedido, self.operador, motivo="Cancelar con guía")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", accion="cancelada_carrier").exists())


class DecisionesMesaTests(BaseReingreso):
    def pedido_fuera(self, estado):
        pedido = self.pedido_con_linea(3, estado=estado)
        LineaPedido.objects.filter(pedido=pedido).update(cantidad_pickeada=3)
        return pedido

    def test_registrar_reingreso_crea_orden_anunciada_con_lo_despachado(self):
        pedido = self.pedido_fuera(Pedido.RETORNADO)
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        orden = services.registrar_reingreso(pedido, self.operador)
        self.assertEqual((orden.tipo, orden.estado, orden.pedido_id), ("reingreso", "ANUNCIADA", pedido.pk))
        self.assertEqual(orden.lineas.get().cantidad_anunciada, 3)
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.reingreso_estado), (Pedido.RETORNADO, Pedido.REINGRESADO))
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))
        with self.assertRaisesMessage(ValueError, "ya tiene decisión"):
            services.registrar_reingreso(pedido, self.operador)
        recibir(orden.lineas.get(), 2, 1, self.operador)
        self.assertEqual(suma(self.sku, Saldo.EN_PUTAWAY), 2)
        self.assertEqual(suma(self.sku, Saldo.CUARENTENA), 1)

    def test_no_recuperado_resuelve_la_can_cancela_y_sale_de_la_lista(self):
        pedido = self.pedido_fuera(Pedido.EN_TRANSITO)
        services.cancelar(pedido, self.operador, motivo="Cancelación tardía")
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.cancelacion_tardia), (Pedido.EN_TRANSITO, True))
        incidencia = Incidencia.objects.get(pedido=pedido, tipo="CAN")
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        services.marcar_no_recuperado(pedido, self.operador, motivo="Perdido por el carrier")
        pedido.refresh_from_db()
        incidencia.refresh_from_db()
        self.assertEqual((pedido.reingreso_estado, pedido.estado), (Pedido.NO_RECUPERADO, Pedido.CANCELADO))
        self.assertEqual(incidencia.estado, "RESUELTA")
        self.assertTrue(EventoAuditoria.objects.filter(accion="inventario_no_recuperado", entidad_id=str(pedido.pk)).exists())
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))
        self.assertEqual(suma(self.sku, Saldo.EN_PUTAWAY), 0)

    def test_registrar_reingreso_de_cancelacion_tardia_cancela_el_pedido(self):
        pedido = self.pedido_fuera(Pedido.RECOLECTADO)
        services.cancelar(pedido, self.operador, motivo="Cancelación tardía")
        services.registrar_reingreso(pedido, self.operador)
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.reingreso_estado), (Pedido.CANCELADO, Pedido.REINGRESADO))
        # La CAN sigue abierta hasta que la mercancía llegue y Mesa la resuelva.
        self.assertEqual(Incidencia.objects.get(pedido=pedido, tipo="CAN").estado, Incidencia.ABIERTA)

    def test_resolver_la_can_cancela_y_deja_el_reingreso_por_decidir(self):
        from apps.incidencias.services import resolver

        pedido = self.pedido_fuera(Pedido.EN_TRANSITO)
        services.cancelar(pedido, self.operador, motivo="Cancelación tardía")
        incidencia = Incidencia.objects.get(pedido=pedido, tipo="CAN")
        resolver(incidencia, "Comprador confirmó que no lo quiere; el carrier lo trae de vuelta.", self.operador)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        orden = services.registrar_reingreso(pedido, self.operador)
        self.assertEqual(orden.lineas.get().cantidad_anunciada, 3)
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))

    def test_retornado_se_queda_retornado_al_decidir(self):
        pedido = self.pedido_fuera(Pedido.RETORNADO)
        services.registrar_reingreso(pedido, self.operador)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RETORNADO)

    def test_decision_sobre_pedido_en_bodega_es_error(self):
        pedido = self.pedido_con_linea(1)
        with self.assertRaisesMessage(ValueError, "no ha salido de bodega"):
            services.marcar_no_recuperado(pedido, self.operador, motivo="x")
        with self.assertRaises(ValueError):
            services.registrar_reingreso(pedido, self.operador)
