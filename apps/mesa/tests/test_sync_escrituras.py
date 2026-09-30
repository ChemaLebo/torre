"""Mesa → Sync: las escrituras a Shopify pendientes se ven y, vencidas, se
reactivan o se descartan (2026-09-30)."""
from datetime import timedelta

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
