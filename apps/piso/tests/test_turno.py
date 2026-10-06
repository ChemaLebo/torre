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


class ColaUnicaTests(PisoTestCase):
    """Chema 2026-10-06: EMPEZAR / CONTINUAR es la ÚNICA puerta. Un operador
    no abre por URL un pedido que no es suyo ni el que la cola le da (FIFO
    global, sin importar cliente); Mesa sí, pero queda el evento
    `tomado_fuera_de_orden`."""

    def setUp(self):
        from apps.core.models import Cliente

        self.login_piso()
        self.crear_stock(cantidad=50)
        self.viejo = self.crear_pedido(cantidad=1)
        self.nuevo = self.crear_pedido(cantidad=1)
        otro_cliente = Cliente.objects.create(nombre="Infinitea", slug="infinitea", integracion_envios="envia")
        Pedido.objects.filter(pk=self.viejo.pk).update(cliente=otro_cliente)  # el más viejo es de OTRO cliente

    def test_por_url_solo_se_abre_el_que_toca(self):
        from apps.pedidos.services import iniciar_picking

        iniciar_picking(self.nuevo, self.operador)  # mío (p. ej. una transferencia): se abre aunque no sea el más viejo
        self.assertEqual(self.client.get(reverse("piso:picking_pedido", args=[self.nuevo.pk])).status_code, 200)
        # Dos pickings libres: la cola da el más viejo (de Infinitea, FIFO global); el otro no se abre.
        iniciar_picking(self.viejo, self.operador)
        Pedido.objects.filter(pk__in=[self.nuevo.pk, self.viejo.pk]).update(asignado_a=None)
        Pedido.objects.filter(pk=self.viejo.pk).update(ts_picking=self.viejo.creado)  # se empezó antes
        respuesta = self.client.get(reverse("piso:picking_pedido", args=[self.nuevo.pk]), follow=True)
        self.assertRedirects(respuesta, reverse("piso:home"), fetch_redirect_response=False)
        self.assertContains(respuesta, f"{self.nuevo.folio} no es el que sigue: te toca {self.viejo.folio}")
        respuesta = self.client.post(
            reverse("piso:picking_pedido", args=[self.nuevo.pk]), {"codigo": "7501234567890", "cantidad": "1"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 409)
        self.assertIn("te toca", respuesta.json()["error"])
        self.assertIsNone(Pedido.objects.get(pk=self.nuevo.pk).asignado_a)  # no se lo quedó
        # EMPEZAR manda al que toca, y ese sí se abre.
        respuesta = self.client.post(reverse("piso:home"), {"accion": "siguiente"})
        self.assertRedirects(respuesta, reverse("piso:picking_pedido", args=[self.viejo.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("piso:picking_pedido", args=[self.viejo.pk])).status_code, 200)

    def test_empaque_por_url_tambien_sigue_la_cola(self):
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking

        iniciar_picking(self.nuevo, self.operador)
        confirmar_linea_pick(self.nuevo.lineas.get(), 1, self.operador)
        Pedido.objects.filter(pk=self.nuevo.pk).update(asignado_a=None)  # listo para empacar, libre
        # A medias y libre va antes que el pendiente viejo: la cola lo da y se abre.
        self.assertEqual(self.client.get(reverse("piso:empaque_pedido", args=[self.nuevo.pk])).status_code, 200)
        # Un empacado sin guía más viejo (libre) pasa adelante: el otro ya no se abre.
        empacado = self.dejar_empacado(self.crear_pedido(cantidad=1))
        Pedido.objects.filter(pk=empacado.pk).update(asignado_a=None, ts_picking=self.viejo.creado)
        respuesta = self.client.get(reverse("piso:empaque_pedido", args=[self.nuevo.pk]), follow=True)
        self.assertContains(respuesta, f"te toca {empacado.folio}")

    def test_mesa_abre_por_url_y_queda_el_evento(self):
        from apps.core.models import EventoAuditoria
        from apps.pedidos.services import iniciar_picking

        iniciar_picking(self.nuevo, self.operador)
        iniciar_picking(self.viejo, self.operador)
        Pedido.objects.filter(pk__in=[self.nuevo.pk, self.viejo.pk]).update(asignado_a=None)
        Pedido.objects.filter(pk=self.viejo.pk).update(ts_picking=self.viejo.creado)
        mesa = get_user_model().objects.create_user("mesa-cola", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.assertEqual(self.client.get(reverse("piso:picking_pedido", args=[self.nuevo.pk])).status_code, 200)
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(self.nuevo.pk), accion="tomado_fuera_de_orden")
        self.assertEqual(evento.delta["tocaba"], self.viejo.folio)
        # El que sí tocaba no deja evento.
        self.client.get(reverse("piso:picking_pedido", args=[self.viejo.pk]))
        self.assertEqual(EventoAuditoria.objects.filter(accion="tomado_fuera_de_orden").count(), 1)
