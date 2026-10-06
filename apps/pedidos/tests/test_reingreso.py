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


@override_settings(ENVIA_API_KEY="")
class ReingresoPorCajaTests(BaseReingreso):
    """PED-00067 (Chema 2026-10-06): la decisión es POR CAJA. Una caja que el
    carrier regresó aparece por decidir aunque la otra se haya entregado y el
    pedido esté ENTREGADO; el reingreso lleva solo el contenido de esa caja."""

    def pedido_dos_cajas(self, estado=Pedido.ENTREGADO):
        from apps.envios.models import Paquete, PaqueteLinea

        pedido = self.pedido_con_linea(4, estado=estado)
        linea = pedido.lineas.get()
        LineaPedido.objects.filter(pk=linea.pk).update(cantidad_pickeada=4, cantidad_despachada=4, reservada=True)
        cajas = []
        for n in (1, 2):
            caja = Paquete.objects.create(pedido=pedido, numero=n, peso_kg=Decimal("5"), carrier="imile", estado=Paquete.DESPACHADO)
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
            cajas.append(caja)
        Guia.objects.create(pedido=pedido, paquete=cajas[0], carrier="imile", numero="IM-1", estado=Guia.ENTREGADO)
        Guia.objects.create(pedido=pedido, paquete=cajas[1], carrier="imile", numero="IM-2", estado=Guia.RETORNO)
        return pedido, cajas

    def test_la_caja_regresada_se_decide_sola_aunque_el_pedido_este_entregado(self):
        from apps.envios.models import Paquete

        pedido, (c1, c2) = self.pedido_dos_cajas()
        self.assertEqual([c.numero for c in services.cajas_por_reingresar(pedido)], [2])
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        orden = services.registrar_reingreso(pedido, self.operador)
        self.assertEqual(orden.lineas.get().cantidad_anunciada, 2)  # solo lo de la caja 2, no las 4 despachadas
        c1.refresh_from_db()
        c2.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((c1.reingreso_estado, c2.reingreso_estado, pedido.reingreso_estado, pedido.estado),
                         ("", Paquete.REINGRESADO, Pedido.REINGRESADO, Pedido.ENTREGADO))
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))
        self.assertEqual(EventoAuditoria.objects.get(accion="reingreso_creado", entidad_id=orden.folio).delta["cajas"], [2])
        with self.assertRaisesMessage(ValueError, "no tiene cajas regresadas por decidir"):
            services.registrar_reingreso(pedido, self.operador)
        # Si después regresa también la caja 1, vuelve a aparecer y se decide aparte.
        Guia.objects.filter(numero="IM-1").update(estado=Guia.RETORNO)
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        self.assertEqual([c.numero for c in services.cajas_por_reingresar(pedido)], [1])
        services.marcar_no_recuperado(pedido, self.operador, motivo="el carrier la perdió")
        c1.refresh_from_db()
        self.assertEqual(c1.reingreso_estado, Paquete.NO_RECUPERADO)
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))

    def test_cancelacion_tardia_decide_todas_las_cajas_que_salieron(self):
        from apps.envios.models import Paquete

        pedido, (c1, c2) = self.pedido_dos_cajas(estado=Pedido.EN_TRANSITO)
        Guia.objects.filter(numero__in=["IM-1", "IM-2"]).update(estado=Guia.EN_TRANSITO)
        services.cancelar(pedido, self.operador, motivo="Cancelación tardía")
        pedido.refresh_from_db()
        self.assertTrue(pedido.cancelacion_tardia)
        self.assertEqual([c.numero for c in services.cajas_por_reingresar(pedido)], [1, 2])
        orden = services.registrar_reingreso(pedido, self.operador)
        self.assertEqual(orden.lineas.get().cantidad_anunciada, 4)
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.reingreso_estado), (Pedido.CANCELADO, Pedido.REINGRESADO))
        self.assertEqual(set(Paquete.objects.filter(pedido=pedido).values_list("reingreso_estado", flat=True)), {Paquete.REINGRESADO})

    def test_pedidos_que_salieron_enteros_siguen_decidiendose_por_pedido(self):
        from apps.envios.models import Paquete

        pedido = self.pedido_con_linea(3, estado=Pedido.RETORNADO)
        LineaPedido.objects.filter(pedido=pedido).update(cantidad_pickeada=3)
        # Plan sin empacar por caja (salió entero): la caja se queda PLANEADO con su guía.
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("5"), carrier="estafeta", estado=Paquete.PLANEADO)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero="EST-VIEJA", estado=Guia.RETORNO)
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        self.assertEqual(services.registrar_reingreso(pedido, self.operador).lineas.get().cantidad_anunciada, 3)
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))

    def test_la_migracion_copia_la_decision_del_pedido_a_sus_cajas(self):
        from importlib import import_module

        from django.apps import apps as registro

        from apps.envios.models import Paquete

        copiar = import_module("apps.envios.migrations.0023_paquete_reingreso_estado").copiar_decisiones
        pedido, (c1, c2) = self.pedido_dos_cajas(estado=Pedido.RETORNADO)
        Pedido.objects.filter(pk=pedido.pk).update(reingreso_estado=Pedido.NO_RECUPERADO)
        copiar(registro, None)
        self.assertEqual(set(Paquete.objects.filter(pedido=pedido).values_list("reingreso_estado", flat=True)), {Paquete.NO_RECUPERADO})
        pedido.refresh_from_db()
        self.assertNotIn(pedido, list(services.reingresos_por_decidir()))
