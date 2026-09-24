"""Lista de incidencias en Mesa: columna y filtro de origen (Chema
2026-09-24), para separar las automáticas del poller de las que levantan el
cliente o el comprador."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia


class OrigenEnListaTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.auto = abrir_incidencia(self.colima, Incidencia.TIPO_RET, Incidencia.ORIGEN_AUTO, texto="Sin movimiento")
        self.cliente = abrir_incidencia(self.colima, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, texto="Llegó roto")
        self.url = reverse("mesa:incidencias")

    def test_columna_de_origen(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("<th>Origen</th>", html)
        self.assertIn("Automática (sistema)", html)
        self.assertIn("Cliente (portal)", html)

    def test_filtro_por_origen(self):
        html = self.client.get(self.url, {"origen": "auto"}).content.decode()
        self.assertIn(self.auto.folio, html)
        self.assertNotIn(self.cliente.folio, html)
        html = self.client.get(self.url, {"origen": "cliente"}).content.decode()
        self.assertIn(self.cliente.folio, html)
        self.assertNotIn(self.auto.folio, html)
