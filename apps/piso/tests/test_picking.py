"""Picking en piso: iniciar la ola y escanear línea por línea contra el SKU real."""
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.pedidos.models import LineaPedido, Pedido

from .base import PisoTestCase


class PickingPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=3)
        self.url_detalle = reverse("piso:picking_pedido", args=[self.pedido.pk])

    def _iniciar(self):
        self.client.post(reverse("piso:picking"), {
            "accion": "iniciar", "pedido_id": self.pedido.pk,
        })
        self.pedido.refresh_from_db()

    def test_iniciar_picking_transiciona_y_estampa_ts(self):
        self._iniciar()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertIsNotNone(self.pedido.ts_picking)

    def test_soltar_pedido_lo_deja_libre_con_su_avance_y_otro_lo_toma(self):
        from django.contrib.auth.models import User

        from apps.core.models import EventoAuditoria, PerfilUsuario

        self._iniciar()
        self.client.post(self.url_detalle, {"codigo": "7501234567890", "cantidad": "1"})
        self.assertEqual(self.pedido.asignado_a, self.operador)
        respuesta = self.client.post(self.url_detalle, {"accion": "soltar", "motivo": "Faltante de producto"}, follow=True)
        self.assertRedirects(respuesta, reverse("piso:picking"), fetch_redirect_response=False)
        self.assertContains(respuesta, "liberado con su avance")
        self.pedido.refresh_from_db()
        self.assertIsNone(self.pedido.asignado_a)
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 1)  # el avance no se pierde
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(self.pedido.pk), accion="pedido_soltado")
        self.assertEqual((evento.delta["de"], evento.delta["motivo"]), ("piso1", "Faltante de producto"))
        # Otro operador lo ve en la lista, lo abre y al escanear se vuelve su dueño.
        otro = User.objects.create_user("piso2", password="pin-piso")
        PerfilUsuario.objects.create(usuario=otro, rol=PerfilUsuario.ROL_PISO, pin="2222")
        self.client.force_login(otro)
        respuesta = self.client.get(reverse("piso:picking"))
        self.assertContains(respuesta, self.pedido.folio)
        self.client.post(self.url_detalle, {"codigo": "7501234567890", "cantidad": "1"})
        self.pedido.refresh_from_db()
        self.assertEqual((self.pedido.asignado_a, self.pedido.lineas.get().cantidad_pickeada), (otro, 2))
        # Y el primero ya no puede soltar lo que no es suyo.
        self.client.force_login(self.operador)
        respuesta = self.client.post(self.url_detalle, {"accion": "soltar"}, follow=True)
        self.assertContains(respuesta, "lo tiene piso2")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, otro)

    def test_lista_agrupa_por_cliente_y_muestra_los_de_otros_sin_boton(self):
        from django.contrib.auth.models import User

        from apps.core.models import Cliente, PerfilUsuario
        from apps.pedidos.models import Pedido as P

        otro_cliente = Cliente.objects.create(nombre="Infinitea", slug="infinitea", integracion_envios="envia")
        ajeno = self.crear_pedido(cantidad=1)  # con reserva: sin ella sería "sin inventario"
        P.objects.filter(pk=ajeno.pk).update(cliente=otro_cliente)
        self._iniciar()  # el mío queda EN_PICKING conmigo
        otro = User.objects.create_user("piso2", password="pin-piso")
        PerfilUsuario.objects.create(usuario=otro, rol=PerfilUsuario.ROL_PISO, pin="2222")
        self.client.force_login(otro)
        respuesta = self.client.get(reverse("piso:picking"))
        nombres = [c["cliente"].nombre for c in respuesta.context["clientes"]]
        self.assertEqual(nombres, ["Cervecería Colima", "Infinitea"])
        self.assertContains(respuesta, "lo tiene <b>piso1</b>")
        self.assertContains(respuesta, "Lo está surtiendo piso1")
        self.assertContains(respuesta, "Iniciar picking")  # el de Infinitea, pendiente, sí se puede tomar
        self.assertNotContains(respuesta, f'href="{self.url_detalle}"')  # el de piso1 no se abre

    def test_iniciar_pedido_ya_tomado_avisa_la_carrera(self):
        # Dos tablets con la misma lista: el segundo POST de "iniciar" llega
        # cuando el pedido ya está EN_PICKING — la vista valida bajo lock y
        # avisa sin drama (jamás doble inicio ni transición inválida).
        self._iniciar()
        respuesta = self.client.post(reverse("piso:picking"), {
            "accion": "iniciar", "pedido_id": self.pedido.pk,
        }, follow=True)
        self.assertContains(respuesta, "Otro operador ya tomó")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    def test_escaneo_equivocado_no_pickea_y_avisa(self):
        self._iniciar()
        respuesta = self.client.post(self.url_detalle, {
            "codigo": "0000000000000", "cantidad": "1",
        }, follow=True)
        self.assertContains(respuesta, "Código equivocado")
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 0)

    def test_escaneo_correcto_por_codigo_de_barras(self):
        self._iniciar()
        self.client.post(self.url_detalle, {
            "codigo": self.sku.codigo_barras, "cantidad": "2",
        }, follow=True)
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 2)

    def test_completar_pedido_manda_a_empaque(self):
        self._iniciar()
        self.client.post(self.url_detalle, {"codigo": self.sku.codigo_barras, "cantidad": "2"})
        respuesta = self.client.post(self.url_detalle, {
            "codigo": self.sku.codigo, "cantidad": "1",  # el código de SKU también vale
        })
        self.assertRedirects(
            respuesta, reverse("piso:empaque_pedido", args=[self.pedido.pk]),
            fetch_redirect_response=False,
        )
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 3)

    def test_no_se_puede_pickear_de_mas(self):
        self._iniciar()
        self.client.post(self.url_detalle, {"codigo": self.sku.codigo_barras, "cantidad": "3"})
        respuesta = self.client.post(self.url_detalle, {
            "codigo": self.sku.codigo_barras, "cantidad": "1",
        }, follow=True)
        self.assertContains(respuesta, "ya está completa")
        linea = self.pedido.lineas.get()
        self.assertEqual(linea.cantidad_pickeada, 3)

    def test_detalle_de_pedido_no_en_picking_redirige(self):
        respuesta = self.client.get(self.url_detalle)  # sigue PENDIENTE
        self.assertRedirects(respuesta, reverse("piso:picking"), fetch_redirect_response=False)


class PickingParcialTests(PisoTestCase):
    """Fulfillment parcial (Chema 2026-09-22): la línea sin inventario no
    detiene la ola — se ve con el tag "Sin inventario", el escáner la rechaza
    y el pedido cierra sin ella."""

    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.agotado = SKU.objects.create(
            cliente=self.cliente, codigo="AGOTADO-SIX", codigo_barras="7509999999999",
            descripcion="Six agotado", peso_gr=2000, requiere_lote=False,
        )
        self.pedido = self.crear_pedido(cantidad=2)
        self.faltante = LineaPedido.objects.create(pedido=self.pedido, sku=self.agotado, cantidad=1)
        self.url_detalle = reverse("piso:picking_pedido", args=[self.pedido.pk])

    def _iniciar(self):
        self.client.post(reverse("piso:picking"), {"accion": "iniciar", "pedido_id": self.pedido.pk})
        self.pedido.refresh_from_db()

    def test_la_lista_avisa_y_deja_iniciar(self):
        respuesta = self.client.get(reverse("piso:picking"))
        self.assertContains(respuesta, "Sin inventario · 1 pza")
        self.assertContains(respuesta, "Iniciar picking")
        self._iniciar()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    def test_el_detalle_muestra_la_faltante_fuera_de_la_ruta(self):
        self._iniciar()
        respuesta = self.client.get(self.url_detalle)
        self.assertContains(respuesta, "no se surte en esta ola")
        self.assertContains(respuesta, "AGOTADO-SIX")
        self.assertNotContains(respuesta, 'data-codigo="AGOTADO-SIX"')  # no es una línea de la ruta
        self.assertContains(respuesta, "0 / 2")  # el avance cuenta solo lo que se surte

    def test_escanear_la_faltante_se_rechaza(self):
        self._iniciar()
        respuesta = self.client.post(self.url_detalle, {"codigo": "7509999999999", "cantidad": "1"}, follow=True)
        self.assertContains(respuesta, "SIN INVENTARIO")
        self.faltante.refresh_from_db()
        self.assertEqual(self.faltante.cantidad_pickeada, 0)

    def test_completar_lo_que_hay_manda_a_empaque(self):
        self._iniciar()
        respuesta = self.client.post(self.url_detalle, {"codigo": self.sku.codigo_barras, "cantidad": "2"})
        self.assertRedirects(
            respuesta, reverse("piso:empaque_pedido", args=[self.pedido.pk]), fetch_redirect_response=False,
        )

    def test_el_json_del_escaner_cierra_sin_la_faltante(self):
        self._iniciar()
        respuesta = self.client.post(
            self.url_detalle, {"codigo": self.sku.codigo_barras, "cantidad": "2"}, HTTP_ACCEPT="application/json",
        )
        datos = respuesta.json()
        self.assertTrue(datos["completo"])
        self.assertEqual(datos["avance"], {"pickeadas": 2, "total": 2})

    def test_todo_sin_inventario_no_arranca_ni_entra_a_la_cola(self):
        self.pedido.delete()
        pedido = self.crear_pedido(cantidad=1, reservar_stock=False)
        respuesta = self.client.get(reverse("piso:picking"))
        self.assertContains(respuesta, "espera inventario")
        self.assertNotContains(respuesta, "Iniciar picking")
        respuesta = self.client.post(
            reverse("piso:picking"), {"accion": "iniciar", "pedido_id": pedido.pk}, follow=True,
        )
        self.assertContains(respuesta, "no tiene nada que surtir")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        # EMPEZAR en Mi turno tampoco lo ofrece: no hay ola que empezar.
        respuesta = self.client.post(reverse("piso:home"), {"accion": "siguiente"}, follow=True)
        self.assertContains(respuesta, "Todo al día: no hay pedidos en la cola.")

    def test_esperando_inventario_se_ve_en_picking_y_no_en_salida_ni_en_el_corral(self):
        # Ya salió lo que había (A despachada); B espera stock con el mismo folio.
        self.pedido.delete()
        pedido = self.crear_pedido(cantidad=2, estado=Pedido.PARCIALMENTE_DESPACHADO)
        pedido.lineas.update(cantidad_pickeada=2, cantidad_despachada=2)
        LineaPedido.objects.create(pedido=pedido, sku=self.agotado, cantidad=1)
        self.assertTrue(pedido.esperando_inventario)
        respuesta = self.client.get(reverse("piso:picking"))
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "espera inventario")
        self.assertContains(respuesta, "ya salió una parte")
        self.assertNotContains(respuesta, "Iniciar picking")
        self.assertNotContains(self.client.get(reverse("piso:salida")), pedido.folio)
        home = self.client.get(reverse("piso:home"))
        self.assertNotContains(home, pedido.folio)
        self.assertContains(home, "0 en salida")
