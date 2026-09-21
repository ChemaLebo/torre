"""Entrega en bodega sin guía (pedidos.entregar_sin_guia + comando): desde
pendiente, picking o empacado el pedido queda ENTREGADO, el stock sale del
kardex, las cajas cierran, el operador se libera y Shopify recibe el
fulfillment sin rastreo. Con guía generada o ya en la calle, no aplica."""
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.catalogo.models import Ubicacion
from apps.core.models import EventoAuditoria
from apps.envios.models import Paquete
from apps.inventario.models import Movimiento, Saldo
from apps.inventario.services import confirmar_pick, reservar
from apps.pedidos.models import LineaPedido, Pedido
from apps.pedidos.services import entregar_sin_guia

from .test_servicios import BaseServicios


class EntregaSinGuiaTests(BaseServicios):
    def setUp(self):
        self.anaquel = Ubicacion.objects.create(codigo="PIC-1-I-F-1", tipo=Ubicacion.PICKING)
        Saldo.objects.create(sku=self.sku, ubicacion=self.anaquel, estado=Saldo.UBICADO_VENDIBLE, cantidad=10)
        self.operador = get_user_model().objects.create_user("piso-x", password="x12345678")

    def _pedido(self, estado=Pedido.PENDIENTE, pickeadas=0, con_caja=True, asignado=None):
        pedido = self.pedido_directo(estado=estado, asignado_a=asignado)
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=2, cantidad_pickeada=pickeadas)
        self.assertTrue(reservar(self.sku, 2, pedido.folio))
        linea.reservada = True
        linea.save(update_fields=["reservada"])
        if con_caja:
            Paquete.objects.create(pedido=pedido, numero=1, peso_kg=5, carrier="imile", servicio="ground")
        return pedido

    def _entregar(self, pedido, **kw):
        with patch("apps.integraciones.services.marcar_fulfillment") as fulfillment, \
             self.captureOnCommitCallbacks(execute=True):
            pedido = entregar_sin_guia(pedido, "consola", **kw)
        return pedido, fulfillment

    def _vendible(self):
        return Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).first().cantidad

    def test_desde_picking_confirma_lo_que_falta_despacha_y_libera_al_operador(self):
        pedido = self._pedido(estado=Pedido.EN_PICKING, pickeadas=1, asignado=self.operador)
        pedido, fulfillment = self._entregar(pedido, recibio="Diego", motivo="Recolección en bodega")
        self.assertEqual(pedido.estado, Pedido.ENTREGADO)
        self.assertIsNone(pedido.asignado_a)
        self.assertIsNotNone(pedido.ts_entregado)
        self.assertIsNotNone(pedido.ts_recolectado)
        self.assertEqual(pedido.lineas.get().cantidad_pickeada, 2)
        self.assertEqual(self._vendible(), 8)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado__in=[Saldo.RESERVADO, Saldo.EN_EMPAQUE]).exists())
        self.assertEqual(Movimiento.objects.get(sku=self.sku, tipo=Movimiento.SALIDA).delta, -2)
        self.assertEqual(pedido.paquetes.get().estado, Paquete.DESPACHADO)
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="entregado_sin_guia")
        self.assertEqual((evento.delta["recibio"], evento.delta["estado_inicial"], evento.motivo), ("Diego", "EN_PICKING", "Recolección en bodega"))
        fulfillment.assert_called_once_with(pedido)

    def test_desde_pendiente_pasa_por_picking_y_desde_empacado_solo_despacha(self):
        pendiente = self._pedido(estado=Pedido.PENDIENTE, con_caja=False)
        pendiente, _ = self._entregar(pendiente)
        self.assertEqual(pendiente.estado, Pedido.ENTREGADO)
        self.assertIsNotNone(pendiente.ts_picking)
        self.assertEqual(self._vendible(), 8)

        empacado = self._pedido(estado=Pedido.EMPACADO, pickeadas=2, con_caja=False)
        confirmar_pick(self.sku, 2, empacado.folio)  # empacar ya movió reservado → en_empaque
        empacado, _ = self._entregar(empacado)
        self.assertEqual(empacado.estado, Pedido.ENTREGADO)
        self.assertEqual(self._vendible(), 6)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_EMPAQUE).exists())

    def test_con_guia_o_en_la_calle_no_aplica_y_no_toca_nada(self):
        pedido = self._pedido(estado=Pedido.GUIA_GENERADA, pickeadas=2)
        with self.assertRaisesMessage(ValueError, "cancela la guía primero"):
            entregar_sin_guia(pedido, "consola")
        pedido = self._pedido(estado=Pedido.EN_TRANSITO, pickeadas=2, con_caja=False)
        with self.assertRaisesMessage(ValueError, "solo aplica antes de generar la guía"):
            entregar_sin_guia(pedido, "consola")
        self.assertEqual(self._vendible(), 10)

    def test_comando_simula_sin_aplicar_y_entrega_con_aplicar(self):
        pedido = self._pedido(estado=Pedido.EN_PICKING, pickeadas=2, asignado=self.operador)
        salida = StringIO()
        call_command("entregar_sin_guia", pedido.folio, stdout=salida)
        self.assertIn("Simulación: nada cambió", salida.getvalue())
        self.assertIn("pide 2 · pickeadas 2", salida.getvalue())
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EN_PICKING)
        salida = StringIO()
        with patch("apps.integraciones.services.marcar_fulfillment"), self.captureOnCommitCallbacks(execute=True):
            call_command("entregar_sin_guia", pedido.folio, "--aplicar", "--recibio", "Diego", "--usuario", "piso-x", stdout=salida)
        self.assertIn("entregado sin guía", salida.getvalue())
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.asignado_a), (Pedido.ENTREGADO, None))
        with self.assertRaises(CommandError):
            call_command("entregar_sin_guia", "PED-NO-EXISTE", "--aplicar")
