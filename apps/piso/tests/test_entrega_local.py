"""Entrega local (POD): foto + receptor + mayoría de edad OBLIGATORIA (es alcohol).

El flujo entero vive tras TORRE["FLOTA_PROPIA"] (default False = sin flota):
apagado, las URLs responden 404 amable. Estas pruebas prenden el flag para
ejercitar el POD; Flota404Tests cubre el default apagado.
"""
from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria, EvidenciaFoto
from apps.pedidos.models import Pedido

from .base import PisoTestCase

TORRE_CON_FLOTA = {**settings.TORRE, "FLOTA_PROPIA": True}


class Flota404Tests(PisoTestCase):
    """Sin flota propia (default) las pantallas de entrega local no existen."""

    def setUp(self):
        self.login_piso()
        self.pedido = self.crear_pedido(
            cantidad=1, es_local=True, estado=Pedido.RECOLECTADO, reservar_stock=False,
        )

    def test_lista_regresa_404_amable(self):
        respuesta = self.client.get(reverse("piso:entrega_local"))
        self.assertEqual(respuesta.status_code, 404)
        self.assertContains(respuesta, "no tenemos flota propia", status_code=404)

    def test_detalle_regresa_404_amable(self):
        respuesta = self.client.get(
            reverse("piso:entrega_local_pedido", args=[self.pedido.pk])
        )
        self.assertEqual(respuesta.status_code, 404)

    def test_post_de_pod_tambien_404(self):
        respuesta = self.client.post(
            reverse("piso:entrega_local_pedido", args=[self.pedido.pk]),
            {"receptor": "Juan", "mayoria_edad": "si", "foto_pod": self.foto()},
        )
        self.assertEqual(respuesta.status_code, 404)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.RECOLECTADO)


@override_settings(TORRE=TORRE_CON_FLOTA)
class EntregaLocalPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        # En reparto: ya salió con manifiesto (RECOLECTADO), sin stock que mover aquí.
        self.pedido = self.crear_pedido(
            cantidad=1, es_local=True, estado=Pedido.RECOLECTADO, reservar_stock=False,
        )
        self.url = reverse("piso:entrega_local_pedido", args=[self.pedido.pk])

    def test_lista_muestra_el_pedido_en_reparto(self):
        respuesta = self.client.get(reverse("piso:entrega_local"))
        self.assertContains(respuesta, self.pedido.folio)
        self.assertContains(respuesta, "mayoría de edad")

    def test_sin_verificar_mayoria_de_edad_no_se_entrega(self):
        respuesta = self.client.post(self.url, {
            "receptor": "Juan Receptor",
            "foto_pod": self.foto("pod.jpg"),
            # sin checkbox mayoria_edad
        }, follow=True)
        self.assertContains(respuesta, "mayoría de edad")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.RECOLECTADO)
        self.assertFalse(
            EvidenciaFoto.objects.filter(entidad="entrega_local", entidad_id=str(self.pedido.pk)).exists()
        )

    def test_sin_foto_o_sin_receptor_no_se_entrega(self):
        respuesta = self.client.post(self.url, {
            "receptor": "", "mayoria_edad": "si",
        }, follow=True)
        self.assertContains(respuesta, "foto de entrega")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.RECOLECTADO)

    def test_pod_completo_entrega_y_guarda_evidencia(self):
        respuesta = self.client.post(self.url, {
            "receptor": "Juan Receptor",
            "mayoria_edad": "si",
            "foto_pod": self.foto("pod.jpg"),
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.ENTREGADO)
        self.assertIsNotNone(self.pedido.ts_entregado)

        evidencia = EvidenciaFoto.objects.get(
            entidad="entrega_local", entidad_id=str(self.pedido.pk), tipo="pod",
        )
        self.assertEqual(evidencia.tomada_por, "piso1")

        evento = EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk), accion="pod_entrega_local",
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.delta.get("receptor"), "Juan Receptor")
        self.assertTrue(evento.delta.get("mayoria_edad_verificada"))

    def test_pedido_no_local_no_pasa_por_pod(self):
        foraneo = self.crear_pedido(
            cantidad=1, es_local=False, estado=Pedido.RECOLECTADO, reservar_stock=False,
        )
        respuesta = self.client.get(reverse("piso:entrega_local_pedido", args=[foraneo.pk]))
        self.assertRedirects(
            respuesta, reverse("piso:entrega_local"), fetch_redirect_response=False,
        )
