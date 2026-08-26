"""Vistas de Mesa del vertical de pedidos manuales: alta en dos pasos y
cancelación por fila desde mesa:pedidos.

El alta pasa por pedidos.services.crear_pedido_manual y la cancelación por la
matriz de pedidos.services.cancelar: aquí se prueba el cableado de la vista
(dos pasos, roles, flashes y auditoría), no el dominio.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente, EventoAuditoria

from .test_vistas import crear_pedido, crear_usuario


class BasePedidoManualVista(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.inactivo = Cliente.objects.create(
            nombre="Cliente Churneado", slug="churneado", activo=False,
        )
        cls.sku = SKU.objects.create(
            cliente=cls.colima,
            codigo="COL-SIX",
            descripcion="Colimita six pack",
            peso_gr=2400,
            precio_declarado=Decimal("189.00"),
        )
        cls.sku_inactivo = SKU.objects.create(
            cliente=cls.colima,
            codigo="COL-VIEJO",
            descripcion="Etiqueta descontinuada",
            peso_gr=1000,
            activo=False,
        )
        cls.usuario_mesa = crear_usuario("mesa1", "mesa", pin="3333")
        cls.usuario_portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.usuario_piso = crear_usuario("piso1", "piso", pin="1111")


class PedidoNuevoVistaTests(BasePedidoManualVista):
    def setUp(self):
        self.url = reverse("mesa:pedido_nuevo")
        self.client.force_login(self.usuario_mesa)

    def _datos_post(self, **extra):
        datos = {
            "cliente": "colima",
            "comprador_nombre": "Lupita Ramírez",
            "comprador_tel": "+525512345678",
            "comprador_email": "",
            "calle": "Av. Constitución 380",
            "colonia": "Centro",
            "ciudad": "Ciudad de México",
            "estado": "CDMX",
            "cp": "01780",
            "valor_declarado": "",
            "nota_regalo": "",
            "sku_1": self.sku.pk,
            "cantidad_1": 3,
        }
        datos.update(extra)
        return datos

    def test_paso_uno_selector_solo_clientes_activos(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertNotContains(respuesta, "Cliente Churneado")
        # Sin cliente elegido todavía no hay formulario de captura.
        self.assertNotContains(respuesta, "Nombre del comprador")

    def test_paso_dos_form_acotado_al_cliente(self):
        respuesta = self.client.get(self.url, {"cliente": "colima"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Nombre del comprador")
        self.assertContains(respuesta, "Colimita six pack")
        # El SKU inactivo no se ofrece en el selector.
        self.assertNotContains(respuesta, "Etiqueta descontinuada")

    def test_cliente_inactivo_no_captura(self):
        respuesta = self.client.get(self.url, {"cliente": "churneado"})
        self.assertEqual(respuesta.status_code, 404)

    def test_alta_feliz_por_post(self):
        from apps.pedidos.models import Pedido

        with patch("apps.inventario.services.reservar", return_value=True):
            respuesta = self.client.post(
                f"{self.url}?cliente=colima", self._datos_post(), follow=True,
            )
        pedido = Pedido.objects.get()
        self.assertEqual(pedido.origen, "manual")
        self.assertEqual(pedido.cliente, self.colima)
        self.assertTrue(pedido.es_local)
        self.assertEqual(pedido.cp, "01780")
        self.assertEqual(pedido.direccion["address1"], "Av. Constitución 380")
        linea = pedido.lineas.get()
        self.assertEqual(linea.cantidad, 3)
        self.assertTrue(linea.reservada)
        # PRG: redirect a mesa:pedidos filtrado por el folio nuevo.
        self.assertRedirects(respuesta, f"{reverse('mesa:pedidos')}?q={pedido.folio}")
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "creado")
        self.assertContains(respuesta, "entrega local")

    def test_alta_sin_stock_avisa_en_el_flash(self):
        from apps.incidencias.models import Incidencia
        from apps.pedidos.models import Pedido

        # Sin stock real: la reserva falla por la puerta oficial de inventario.
        respuesta = self.client.post(
            f"{self.url}?cliente=colima", self._datos_post(), follow=True,
        )
        pedido = Pedido.objects.get()
        self.assertFalse(pedido.lineas.get().reservada)
        self.assertTrue(pedido.incidencia_activa)
        self.assertTrue(
            Incidencia.objects.filter(pedido=pedido, tipo="FAL").exists()
        )
        self.assertContains(respuesta, "sin stock")
        self.assertContains(respuesta, "FAL")

    def test_form_incompleto_no_crea_pedido(self):
        from apps.pedidos.models import Pedido

        datos = self._datos_post(cp="178")  # CP inválido: van 5 dígitos
        respuesta = self.client.post(f"{self.url}?cliente=colima", datos)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "5 dígitos")
        self.assertEqual(Pedido.objects.count(), 0)

    def test_portal_y_piso_no_entran(self):
        for usuario in (self.usuario_portal, self.usuario_piso):
            self.client.force_login(usuario)
            respuesta = self.client.get(self.url)
            self.assertEqual(respuesta.status_code, 403, f"{usuario.username} entró a pedido_nuevo")
            respuesta = self.client.post(f"{self.url}?cliente=colima", self._datos_post())
            self.assertEqual(respuesta.status_code, 403)

    def test_anonimo_va_a_login(self):
        self.client.logout()
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 302)


class DireccionPedidoManualTests(BasePedidoManualVista):
    """direccion() emite province_code (shape Shopify) derivado del CP: es la
    clave que el adapter de envia.com lee para el estado destino."""

    def _form_valido(self, cp, estado="Jalisco"):
        from apps.mesa.forms import FormPedidoManual

        form = FormPedidoManual(self.colima, {
            "comprador_nombre": "Lupita Ramírez",
            "comprador_tel": "",
            "comprador_email": "",
            "calle": "Av. Vallarta 1500",
            "colonia": "",
            "ciudad": "Guadalajara",
            "estado": estado,
            "cp": cp,
            "valor_declarado": "",
            "nota_regalo": "",
            "sku_1": self.sku.pk,
            "cantidad_1": 1,
        })
        self.assertTrue(form.is_valid(), form.errors)
        return form

    def test_cp_de_guadalajara_trae_province_code_jal(self):
        direccion = self._form_valido("44100").direccion()
        # Vocabulario code_shopify de envia (JAL, no el JA de la tabla vieja).
        self.assertEqual(direccion["province_code"], "JAL")
        self.assertEqual(direccion["province"], "Jalisco")  # el texto capturado se conserva

    def test_cp_sin_mapa_deja_province_code_vacio(self):
        direccion = self._form_valido("17000").direccion()
        self.assertEqual(direccion["province_code"], "")


class ReintentarReservasDesdeMesaTests(BasePedidoManualVista):
    def setUp(self):
        self.url = reverse("mesa:pedidos")
        self.client.force_login(self.usuario_mesa)

    def test_boton_reintenta_reservas_del_pedido(self):
        from unittest.mock import patch

        pedido = crear_pedido(self.colima)
        with patch(
            "apps.pedidos.services.reintentar_reservas_pedido",
            return_value="PED-PRUEBA: reservas al día",
        ) as reintento:
            respuesta = self.client.post(
                self.url, {"accion": "reintentar_reservas", "folio": pedido.folio}, follow=True,
            )
        reintento.assert_called_once()
        self.assertContains(respuesta, "reservas al día")

    def test_folio_desconocido_es_404(self):
        respuesta = self.client.post(
            self.url, {"accion": "reintentar_reservas", "folio": "PED-99999"},
        )
        self.assertEqual(respuesta.status_code, 404)


class CancelarDesdeMesaTests(BasePedidoManualVista):
    def setUp(self):
        self.url = reverse("mesa:pedidos")
        self.client.force_login(self.usuario_mesa)

    def _cancelar(self, pedido, motivo="El cliente ya no lo quiere", **extra):
        datos = {"accion": "cancelar", "folio": pedido.folio, "motivo": motivo}
        datos.update(extra)
        return self.client.post(self.url, datos, follow=True)

    def test_pendiente_cancela_directo(self):
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.colima)
        respuesta = self._cancelar(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertContains(respuesta, "cancelado y stock liberado")
        evento = EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=pedido.folio, accion="cancelacion_solicitada_mesa",
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.motivo, "El cliente ya no lo quiere")

    def test_empacado_queda_pendiente_y_es_restock_en_piso(self):
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(
            self.colima, estado=Pedido.EMPACADO, ts_empacado=timezone.now(),
        )
        respuesta = self._cancelar(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELACION_PENDIENTE)
        self.assertContains(respuesta, "confirmar el restock")
        # La tarea aparece en el tablero del piso (mesa también lo puede ver).
        home_piso = self.client.get(reverse("piso:home"))
        self.assertContains(home_piso, pedido.folio)

    def test_despachado_abre_incidencia_can(self):
        from apps.incidencias.models import Incidencia
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.colima, estado=Pedido.EN_TRANSITO)
        respuesta = self._cancelar(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EN_TRANSITO)  # el estado no cambia
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo="CAN").exists())
        self.assertContains(respuesta, "incidencia CAN")

    def test_entregado_error_claro(self):
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.colima, estado=Pedido.ENTREGADO)
        respuesta = self._cancelar(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.ENTREGADO)
        self.assertContains(respuesta, "No se puede cancelar")
        self.assertFalse(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id=pedido.folio,
                accion="cancelacion_solicitada_mesa",
            ).exists()
        )

    def test_motivo_requerido(self):
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.colima)
        respuesta = self._cancelar(pedido, motivo="")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertContains(respuesta, "motivo")

    def test_boton_cancelar_solo_en_estados_cancelables(self):
        from apps.pedidos.models import Pedido

        crear_pedido(self.colima, estado=Pedido.ENTREGADO)
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, 'value="cancelar"')
        crear_pedido(self.colima)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'value="cancelar"')

    def test_cancelar_conserva_los_filtros_activos(self):
        # El form inline postea a la URL filtrada: el PRG debe regresar ahí,
        # no a la tabla completa (tanda de cancelaciones sin rearmar filtros).
        pedido = crear_pedido(self.colima)
        url_filtrada = f"{self.url}?estado=PENDIENTE&q={pedido.folio}"
        respuesta = self.client.post(url_filtrada, {
            "accion": "cancelar", "folio": pedido.folio, "motivo": "Duplicado",
        })
        self.assertRedirects(respuesta, url_filtrada)

    def test_piso_no_cancela_desde_mesa(self):
        from apps.pedidos.models import Pedido

        pedido = crear_pedido(self.colima)
        self.client.force_login(self.usuario_piso)
        respuesta = self.client.post(
            self.url, {"accion": "cancelar", "folio": pedido.folio, "motivo": "intruso"},
        )
        self.assertEqual(respuesta.status_code, 403)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
