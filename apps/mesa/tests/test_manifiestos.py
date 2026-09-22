"""Mesa → Manifiestos: la hoja que firmó el chofer por cada salida, por fecha
y carrier, con link a la versión imprimible (Chema 2026-09-22)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.envios.models import LineaManifiesto, Manifiesto
from apps.pedidos.models import Pedido


class ManifiestosMesaTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)
        self.pedido = Pedido.objects.create(
            cliente=self.colima, origen="manual", comprador_nombre="Ana", cp="44100",
            estado=Pedido.RECOLECTADO,
        )

    def _manifiesto(self, carrier):
        hoja = Manifiesto.objects.create(carrier=carrier, corral="SAL-OTRO", operador=self.mesa, chofer="Juan")
        LineaManifiesto.objects.create(manifiesto=hoja, pedido=self.pedido, numero_guia=f"G-{carrier}", caja=1)
        return hoja

    def test_lista_filtra_y_enlaza_la_hoja_imprimible(self):
        hoja = self._manifiesto("puntopost")
        otra = self._manifiesto("estafeta")
        respuesta = self.client.get(reverse("mesa:manifiestos"))
        self.assertContains(respuesta, hoja.folio)
        self.assertContains(respuesta, otra.folio)
        self.assertContains(respuesta, self.pedido.folio)
        self.assertContains(respuesta, reverse("piso:manifiesto", args=[hoja.pk]))
        filtrada = self.client.get(reverse("mesa:manifiestos") + "?carrier=estafeta")
        self.assertContains(filtrada, otra.folio)
        self.assertNotContains(filtrada, hoja.folio)
        hoja_html = self.client.get(reverse("piso:manifiesto", args=[hoja.pk]))  # Mesa también la abre
        self.assertContains(hoja_html, "G-puntopost")
        self.assertContains(hoja_html, reverse("mesa:manifiestos"))

    def test_folio_anual_consecutivo(self):
        primero = self._manifiesto("puntopost")
        segundo = self._manifiesto("puntopost")
        self.assertEqual(int(primero.folio.rsplit("-", 1)[1]) + 1, int(segundo.folio.rsplit("-", 1)[1]))
