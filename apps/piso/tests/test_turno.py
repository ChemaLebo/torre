"""Mi turno (C2): LA card con prioridad del servidor y EMPEZAR sin carreras.

Prioridad: 1º EN_PICKING ya empezados, 2º PENDIENTE por creado asc. El POST
accion=siguiente inicia picking con select_for_update: si otro operador ganó
el pedido entre el render y el POST, se toma el que sigue SIN error visible.
"""
from django.urls import reverse

from apps.pedidos.models import Pedido

from .base import PisoTestCase


class MiTurnoCardTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:home")

    def test_card_muestra_el_pendiente_mas_viejo_con_boton_empezar(self):
        primero = self.crear_pedido(cantidad=1)
        self.crear_pedido(cantidad=2)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["siguiente"].pk, primero.pk)
        self.assertContains(respuesta, primero.folio)
        self.assertContains(respuesta, "EMPEZAR")

    def test_prioriza_en_picking_ya_empezado_sobre_pendientes(self):
        from apps.pedidos.services import iniciar_picking

        self.crear_pedido(cantidad=1)
        empezado = self.crear_pedido(cantidad=1)
        iniciar_picking(empezado, self.operador)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["siguiente"].pk, empezado.pk)
        self.assertContains(respuesta, "ya empezado")

    def test_contadores_y_reloj_del_corte_en_el_header(self):
        self.crear_pedido(cantidad=1)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "por pickear")
        self.assertContains(respuesta, "por empacar")
        self.assertContains(respuesta, "en salida")
        self.assertContains(respuesta, "Corte")

    def test_sin_cola_muestra_todo_al_dia(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Todo al día")


class MiTurnoSiguienteTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:home")

    def test_empezar_inicia_picking_y_redirige(self):
        pedido = self.crear_pedido(cantidad=1)
        respuesta = self.client.post(self.url, {
            "accion": "siguiente", "pedido_id": pedido.pk,
        })
        self.assertRedirects(
            respuesta, reverse("piso:picking_pedido", args=[pedido.pk]),
            fetch_redirect_response=False,
        )
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EN_PICKING)
        self.assertIsNotNone(pedido.ts_picking)

    def test_carrera_dos_tomas_seguidas_agarran_pedidos_distintos(self):
        primero = self.crear_pedido(cantidad=1)
        segundo = self.crear_pedido(cantidad=1)
        # Dos operadores con la MISMA card vieja (ambos vieron a `primero`).
        r1 = self.client.post(self.url, {"accion": "siguiente", "pedido_id": primero.pk})
        r2 = self.client.post(self.url, {"accion": "siguiente", "pedido_id": primero.pk})
        self.assertRedirects(
            r1, reverse("piso:picking_pedido", args=[primero.pk]),
            fetch_redirect_response=False,
        )
        # El segundo NO truena ni duplica: se lleva el que sigue.
        self.assertRedirects(
            r2, reverse("piso:picking_pedido", args=[segundo.pk]),
            fetch_redirect_response=False,
        )
        primero.refresh_from_db()
        segundo.refresh_from_db()
        self.assertEqual(primero.estado, Pedido.EN_PICKING)
        self.assertEqual(segundo.estado, Pedido.EN_PICKING)

    def test_reanudar_un_pedido_empezado_regresa_a_su_picking(self):
        from apps.pedidos.services import iniciar_picking

        pedido = self.crear_pedido(cantidad=2)
        iniciar_picking(pedido, self.operador)
        respuesta = self.client.post(self.url, {
            "accion": "siguiente", "pedido_id": pedido.pk, "empezado": "1",
        })
        self.assertRedirects(
            respuesta, reverse("piso:picking_pedido", args=[pedido.pk]),
            fetch_redirect_response=False,
        )

    def test_reanudar_un_pedido_completo_manda_al_wizard_de_empaque(self):
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking

        pedido = self.crear_pedido(cantidad=1)
        iniciar_picking(pedido, self.operador)
        confirmar_linea_pick(pedido.lineas.get(), 1, self.operador)
        respuesta = self.client.post(self.url, {
            "accion": "siguiente", "pedido_id": pedido.pk, "empezado": "1",
        })
        self.assertRedirects(
            respuesta, reverse("piso:empaque_pedido", args=[pedido.pk]),
            fetch_redirect_response=False,
        )

    def test_sin_pendientes_ni_abiertos_avisa_todo_al_dia(self):
        respuesta = self.client.post(self.url, {"accion": "siguiente"}, follow=True)
        self.assertContains(respuesta, "Todo al día")
