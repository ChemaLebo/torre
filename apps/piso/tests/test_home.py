"""Home del piso: resumen de tareas del día y cierre de restocks de cancelación."""
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.inventario.models import TareaConteo
from apps.pedidos.models import Pedido

from .base import PisoTestCase


class HomePisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.url = reverse("piso:home")

    def test_sin_tareas_todo_al_dia(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Todo al día")

    def test_resumen_cuenta_las_tareas_del_dia(self):
        self.crear_stock(cantidad=50)
        self.crear_pedido(cantidad=2)  # PENDIENTE → cuenta como "por pickear"
        TareaConteo.objects.create(sku=self.sku)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "por pickear")
        self.assertContains(respuesta, "COLIMITA-SIX")

    def test_cancelar_en_picking_libera_al_instante_sin_tarea_de_restock(self):
        from apps.inventario.services import disponible
        from apps.pedidos.services import cancelar, iniciar_picking

        self.crear_stock(cantidad=50)
        pedido = self.crear_pedido(cantidad=4)
        iniciar_picking(pedido, self.operador)
        cancelar(pedido, self.operador, motivo="Cambio de opinión del comprador")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertEqual(disponible(self.sku), 50)
        self.assertNotContains(self.client.get(self.url), "restock pendiente")

    def test_confirmar_restock_legacy_cierra_la_cancelacion(self):
        from apps.inventario.services import disponible
        from apps.pedidos.services import iniciar_picking

        self.crear_stock(cantidad=50)
        pedido = self.crear_pedido(cantidad=4)
        iniciar_picking(pedido, self.operador)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.CANCELACION_PENDIENTE)
        self.assertContains(self.client.get(self.url), pedido.folio)
        respuesta = self.client.post(self.url, {
            "accion": "confirmar_restock", "pedido_id": pedido.pk,
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertEqual(disponible(self.sku), 50)

    def test_confirmar_restock_de_pedido_sin_cancelacion_avisa(self):
        self.crear_stock(cantidad=50)
        pedido = self.crear_pedido(cantidad=1)
        respuesta = self.client.post(self.url, {
            "accion": "confirmar_restock", "pedido_id": pedido.pk,
        }, follow=True)
        self.assertContains(respuesta, "no tiene cancelación pendiente")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)


class CompletarEmpaquetadoTests(PisoTestCase):
    """Mi turno → "Completar empaquetado": lo que sigue en la mesa de empaque
    (cajas sin empacar, sin guía o sin foto de cierre), del operador que lo
    tiene; Mesa ve todos. Nada de eso cuenta como "en salida"."""

    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:home")

    def _con_guia(self, pedido):
        from apps.pedidos.services import generar_guia
        generar_guia(pedido)
        pedido.refresh_from_db()
        return pedido

    def test_empacado_sin_guia_aparece_con_lo_que_falta(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Completar empaquetado")
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "sin guía")
        self.assertContains(respuesta, "1 en empaque · 0 en salida")
        self.assertNotContains(respuesta, "En salida")

    def test_con_guia_pero_sin_cierre_sigue_en_la_mesa_y_al_cerrar_pasa_a_salida(self):
        pedido = self._con_guia(self.dejar_empacado(self.crear_pedido(cantidad=2)))
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Completar empaquetado")
        self.assertContains(respuesta, "falta foto de cierre")
        self.evidencia_cierre(pedido)
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, "Completar empaquetado")
        self.assertContains(respuesta, "En salida")
        self.assertContains(respuesta, "0 en empaque · 1 en salida")

    def test_picking_completo_sin_empacar_cuenta_como_por_empacar(self):
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking

        pedido = self.crear_pedido(cantidad=2)
        iniciar_picking(pedido, self.operador)
        confirmar_linea_pick(pedido.lineas.get(), 2, self.operador)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Completar empaquetado")
        self.assertContains(respuesta, "por empacar")

    def test_lo_de_otro_operador_no_es_mio_pero_mesa_lo_ve(self):
        from django.contrib.auth import get_user_model

        otro = get_user_model().objects.create_user("piso2", password="x")
        PerfilUsuario.objects.create(usuario=otro, rol=PerfilUsuario.ROL_PISO, pin="2222")
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        Pedido.objects.filter(pk=pedido.pk).update(asignado_a=otro)
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, "Completar empaquetado")

        mesa = get_user_model().objects.create_user("mesa1", password="x")
        PerfilUsuario.objects.create(usuario=mesa, rol=PerfilUsuario.ROL_MESA, pin="3333")
        self.client.force_login(mesa)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Completar empaquetado")
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "piso2")
