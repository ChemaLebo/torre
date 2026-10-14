"""Mesa → Sync: las escrituras a Shopify pendientes se ven y, vencidas, se
reactivan o se descartan (2026-09-30)."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.integraciones.models import EscrituraShopifyPendiente


class SyncEscriturasTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente, token="shpat_prueba")
        self.pedido = crear_pedido(self.cliente, self.tienda)
        self.escritura = EscrituraShopifyPendiente.objects.create(
            tienda=self.tienda, pedido=self.pedido, clave=f"fulfillment:{self.pedido.pk}:pedido",
            accion=EscrituraShopifyPendiente.ACCION_FULFILLMENT, datos={"cajas": []},
            ultimo_error="502 Bad Gateway", vence=timezone.now() - timedelta(minutes=5),
        )
        usuario = get_user_model().objects.create_user("mesa", "mesa@example.com", "x")
        PerfilUsuario.objects.create(usuario=usuario, rol="mesa")
        self.client.force_login(usuario)

    def test_la_pantalla_lista_la_escritura_vencida_con_reintentar(self):
        pantalla = self.client.get(reverse("mesa:sync"))
        self.assertContains(pantalla, self.pedido.folio)
        self.assertContains(pantalla, "Crear fulfillment")
        self.assertContains(pantalla, "502 Bad Gateway")
        self.assertContains(pantalla, "vencida")
        self.assertContains(pantalla, 'value="reintentar"')

    def test_reintentar_reactiva_la_vigencia(self):
        respuesta = self.client.post(reverse("mesa:sync"), {"accion": "reintentar", "escritura": self.escritura.pk})
        self.assertRedirects(respuesta, reverse("mesa:sync"))
        self.escritura.refresh_from_db()
        self.assertFalse(self.escritura.vencida)
        self.assertGreater(self.escritura.vence, timezone.now() + timedelta(hours=23))

    def test_descartar_la_borra_con_auditoria(self):
        self.client.post(reverse("mesa:sync"), {"accion": "descartar", "escritura": self.escritura.pk})
        self.assertFalse(EscrituraShopifyPendiente.objects.exists())
        evento = EventoAuditoria.objects.get(accion="escritura_shopify_descartada")
        self.assertEqual(evento.entidad_id, str(self.pedido.pk))
        self.assertEqual(evento.delta["accion"], "fulfillment")

    def test_reintentar_todas_las_vencidas_las_reactiva_y_las_drena_en_segundo_plano(self):
        """Chema 2026-10-14: tras otorgar el scope que faltaba, decenas de
        eventos vencidos se reactivan de un jalón y el hilo los escribe."""
        otra = EscrituraShopifyPendiente.objects.create(
            tienda=self.tienda, pedido=self.pedido, clave="evento:fid:gid://shopify/Fulfillment/1:CARRIER_PICKED_UP",
            accion=EscrituraShopifyPendiente.ACCION_EVENTO, datos={"fid": "gid://shopify/Fulfillment/1", "status": "CARRIER_PICKED_UP"},
            ultimo_error="Access denied for fulfillmentEventCreate", vence=timezone.now() - timedelta(days=12),
        )
        vigente = EscrituraShopifyPendiente.objects.create(
            tienda=self.tienda, pedido=self.pedido, clave="evento:fid:gid://shopify/Fulfillment/2:CARRIER_PICKED_UP",
            accion=EscrituraShopifyPendiente.ACCION_EVENTO, datos={"fid": "gid://shopify/Fulfillment/2", "status": "CARRIER_PICKED_UP"},
            ultimo_error="502", vence=timezone.now() + timedelta(hours=1),
        )
        pantalla = self.client.get(reverse("mesa:sync"))
        self.assertContains(pantalla, "Reintentar todas las vencidas (2)")
        with patch("apps.integraciones.services.drenar_escrituras_en_segundo_plano") as drenar:
            respuesta = self.client.post(reverse("mesa:sync"), {"accion": "reintentar_vencidas"}, follow=True)
        self.assertRedirects(respuesta, reverse("mesa:sync"))
        self.assertContains(respuesta, "2 escritura(s) vencida(s) reactivada(s)")
        drenar.assert_called_once_with()
        for escritura in (self.escritura, otra):
            escritura.refresh_from_db()
            self.assertFalse(escritura.vencida)
        vigente.refresh_from_db()
        self.assertLess(vigente.vence, timezone.now() + timedelta(hours=2))  # la vigente no se toca
        self.assertEqual(EventoAuditoria.objects.get(accion="escrituras_reactivadas").delta["reactivadas"], 2)
        self.assertNotContains(self.client.get(reverse("mesa:sync")), "Reintentar todas las vencidas")
        respuesta = self.client.post(reverse("mesa:sync"), {"accion": "reintentar_vencidas"}, follow=True)
        self.assertContains(respuesta, "No hay escrituras vencidas")
