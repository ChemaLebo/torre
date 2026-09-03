"""Asignación de pedidos en piso: dueño, ocultamiento y transferencia aceptada."""
from django.contrib.auth.models import User
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.pedidos.models import Pedido
from apps.pedidos import services

from .base import PisoTestCase


class AsignacionTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=2)
        self.otro = User.objects.create_user("piso2", password="x12345")
        PerfilUsuario.objects.create(usuario=self.otro, rol="piso")

    def _iniciar(self):
        services.iniciar_picking(self.pedido, self.operador)
        self.pedido.refresh_from_db()

    def test_iniciar_asigna_al_operador(self):
        self._iniciar()
        self.assertEqual(self.pedido.asignado_a, self.operador)

    def test_ajeno_no_ve_ni_abre_el_pedido(self):
        self._iniciar()
        self.client.force_login(self.otro)
        # Oculto en la lista de picking:
        lista = self.client.get(reverse("piso:picking"))
        self.assertNotContains(lista, self.pedido.folio)
        # El detalle lo rechaza con el nombre del dueño:
        detalle = self.client.get(
            reverse("piso:picking_pedido", args=[self.pedido.pk]), follow=True,
        )
        self.assertContains(detalle, "lo tiene")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)

    def test_transferencia_requiere_aceptacion_y_conserva_avance(self):
        self._iniciar()
        from apps.pedidos.services import confirmar_linea_pick
        confirmar_linea_pick(self.pedido.lineas.get(), 1, self.operador)

        services.transferir_pedido(self.pedido, self.operador, self.otro)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)  # aún del dueño
        self.assertEqual(self.pedido.transferencia_a, self.otro)

        # El destinatario la ve en su home y acepta:
        self.client.force_login(self.otro)
        home = self.client.get(reverse("piso:home"))
        self.assertContains(home, "Te mandaron pedidos")
        self.assertContains(home, self.pedido.folio)
        self.client.post(reverse("piso:home"), {
            "accion": "aceptar_transferencia", "pedido_id": self.pedido.pk,
        })
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.otro)
        self.assertIsNone(self.pedido.transferencia_a)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 1)  # avance intacto

    def test_solo_el_dueno_puede_enviar(self):
        self._iniciar()
        with self.assertRaises(ValueError):
            services.transferir_pedido(self.pedido, self.otro, self.operador)

    def test_rechazo_lo_regresa_limpio(self):
        self._iniciar()
        services.transferir_pedido(self.pedido, self.operador, self.otro)
        services.rechazar_transferencia(self.pedido, self.otro)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)
        self.assertIsNone(self.pedido.transferencia_a)

    def test_pedido_libre_se_adopta_al_trabajarlo(self):
        # Compat con lo vivo: un EN_PICKING sin dueño (pre-deploy) lo adopta
        # el primer operador que le haga un POST.
        self._iniciar()
        Pedido.objects.filter(pk=self.pedido.pk).update(asignado_a=None)
        self.client.force_login(self.otro)
        self.client.post(
            reverse("piso:picking_pedido", args=[self.pedido.pk]),
            {"codigo": self.sku.codigo_barras or self.sku.codigo, "cantidad": 1},
        )
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.otro)
