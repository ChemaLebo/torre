"""Mesa → Pedidos → "Cancelar guías" (Chema 2026-09-24): un pedido con guía
comprada que aún no sale se regresa a empaquetado cancelando las guías con el
carrier; con algo en la calle no hay botón ni acción."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class CancelarGuiasMesaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=20)
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        self.caja = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO,
        )
        services.generar_guia(self.pedido)
        self.pedido.refresh_from_db()
        self.url = reverse("mesa:pedidos")

    def test_boton_y_accion_regresan_a_empaque(self):
        self.assertIn('value="cancelar_guias"', self.client.get(self.url).content.decode())
        vieja = self.caja.guia_activa
        respuesta = self.client.post(self.url, {"accion": "cancelar_guias", "folio": self.pedido.folio}, follow=True)
        self.assertContains(respuesta, f"guía {vieja.numero} cancelada")
        self.pedido.refresh_from_db()
        vieja.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.pedido.asignado_a, vieja.estado), (Pedido.EMPACADO, None, Guia.CANCELADA))
        self.assertNotIn('value="cancelar_guias"', self.client.get(self.url).content.decode())

    def test_con_caja_en_la_calle_no_hay_boton_ni_accion(self):
        self.caja.estado = Paquete.DESPACHADO
        self.caja.save(update_fields=["estado"])
        self.assertNotIn('value="cancelar_guias"', self.client.get(self.url).content.decode())
        respuesta = self.client.post(self.url, {"accion": "cancelar_guias", "folio": self.pedido.folio}, follow=True)
        self.assertContains(respuesta, "ya salió")
        self.assertEqual(self.caja.guia_activa.estado, Guia.GUIA_CREADA)
