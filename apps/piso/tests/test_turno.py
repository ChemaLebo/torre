"""Mi turno (C2): LA card con prioridad del servidor y EMPEZAR sin carreras.

Prioridad (Chema 2026-09-22, "de atrás para adelante"): 1º mi empaque
incompleto (o libre), 2º mi picking a medias, 3º picking libre, 4º PENDIENTE
por prioridad de cola. El POST accion=siguiente inicia picking con
select_for_update: si otro operador ganó el pedido entre el render y el POST,
se toma el que sigue SIN error visible.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.pedidos.models import LineaPedido, Pedido

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
        self.assertContains(respuesta, "en empaque")
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
        # Dos operadores DISTINTOS con la MISMA card vieja (ambos vieron a
        # `primero`). El mismo operador no aplica: EMPEZAR lo regresaría a
        # su picking recién abierto, que es la regla.
        otro = get_user_model().objects.create_user("piso2", password="x")
        PerfilUsuario.objects.create(usuario=otro, rol=PerfilUsuario.ROL_PISO, pin="2222")
        r1 = self.client.post(self.url, {"accion": "siguiente", "pedido_id": primero.pk})
        self.client.force_login(otro)
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


class OrdenDeEmpezarTests(PisoTestCase):
    """Chema 2026-09-22, de atrás para adelante: mi empaque incompleto → mi
    picking a medias → picking libre → pedido nuevo. Lo de otro operador
    jamás: es suyo hasta transferencia aceptada."""

    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:home")
        self.otro = get_user_model().objects.create_user("piso2", password="x")
        PerfilUsuario.objects.create(usuario=self.otro, rol=PerfilUsuario.ROL_PISO, pin="2222")

    def _picking_a_medias(self, usuario):
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        pedido = self.crear_pedido(cantidad=2)
        iniciar_picking(pedido, usuario)
        confirmar_linea_pick(pedido.lineas.get(), 1, usuario)
        return pedido

    def _libre(self):
        from apps.pedidos.services import soltar_pedido
        pedido = self._picking_a_medias(self.otro)
        soltar_pedido(pedido, self.otro)
        return pedido

    def test_mi_empaque_incompleto_va_antes_que_todo(self):
        nuevo = self.crear_pedido(cantidad=1)
        self._picking_a_medias(self.operador)
        self._libre()
        empacado = self.dejar_empacado(self.crear_pedido(cantidad=1))  # sin guía: sigue en la mesa
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["siguiente"].pk, empacado.pk)
        self.assertContains(respuesta, "ya empezado — sin guía")
        respuesta = self.client.post(self.url, {"accion": "siguiente", "pedido_id": empacado.pk, "empezado": "1"})
        self.assertRedirects(
            respuesta, reverse("piso:empaque_pedido", args=[empacado.pk]), fetch_redirect_response=False,
        )
        self.assertEqual(Pedido.objects.get(pk=nuevo.pk).estado, Pedido.PENDIENTE)  # el nuevo no se tocó

    def test_mi_picking_a_medias_antes_que_el_libre_y_el_nuevo(self):
        self.crear_pedido(cantidad=1)
        self._libre()
        mio = self._picking_a_medias(self.operador)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["siguiente"].pk, mio.pk)
        self.assertContains(respuesta, "ya empezado — termínalo")
        respuesta = self.client.post(self.url, {"accion": "siguiente"})
        self.assertRedirects(
            respuesta, reverse("piso:picking_pedido", args=[mio.pk]), fetch_redirect_response=False,
        )

    def test_picking_libre_antes_que_el_nuevo(self):
        self.crear_pedido(cantidad=1)
        libre = self._libre()
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["siguiente"].pk, libre.pk)
        self.assertContains(respuesta, "lo soltaron")
        respuesta = self.client.post(self.url, {"accion": "siguiente"})
        self.assertRedirects(
            respuesta, reverse("piso:picking_pedido", args=[libre.pk]), fetch_redirect_response=False,
        )
        libre.refresh_from_db()
        self.assertIsNone(libre.asignado_a)  # se vuelve mío al escanear, no antes

    def test_lo_de_otro_operador_no_se_ofrece(self):
        self._picking_a_medias(self.otro)  # su picking
        ajeno = self.dejar_empacado(self.crear_pedido(cantidad=1))
        Pedido.objects.filter(pk=ajeno.pk).update(asignado_a=self.otro)  # su empaque incompleto
        nuevo = self.crear_pedido(cantidad=1)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["siguiente"].pk, nuevo.pk)
        self.assertContains(respuesta, "en cola")
        respuesta = self.client.post(self.url, {"accion": "siguiente", "pedido_id": nuevo.pk})
        self.assertRedirects(
            respuesta, reverse("piso:picking_pedido", args=[nuevo.pk]), fetch_redirect_response=False,
        )

    def test_esperando_inventario_no_cuenta_como_empaque_incompleto(self):
        from apps.catalogo.models import SKU
        pedido = self.crear_pedido(cantidad=1, estado=Pedido.PARCIALMENTE_DESPACHADO)
        pedido.lineas.update(cantidad_pickeada=1, cantidad_despachada=1)
        agotado = SKU.objects.create(
            cliente=self.cliente, codigo="AGOTADO", descripcion="Agotado", peso_gr=100, requiere_lote=False,
        )
        LineaPedido.objects.create(pedido=pedido, sku=agotado, cantidad=1)
        respuesta = self.client.get(self.url)
        self.assertIsNone(respuesta.context["siguiente"])
