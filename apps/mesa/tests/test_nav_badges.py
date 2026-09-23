"""Conteos en el menú de Mesa: Incidencias muestra las no cerradas (Chema
2026-09-23: resueltas sin cerrar también cuentan), como Recepciones muestra
los reingresos por decidir."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia, cerrar, resolver


class BadgeIncidenciasTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def _menu(self):
        return self.client.get(reverse("mesa:pedidos")).content.decode()

    def test_cuenta_solo_las_abiertas(self):
        self.assertNotIn('Incidencias <span class="pill warn">', self._menu())
        primera = abrir_incidencia(self.colima, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL, texto="Retraso")
        abrir_incidencia(self.colima, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL, texto="Daño")
        self.assertIn('Incidencias <span class="pill warn">2</span>', self._menu())
        resolver(primera, "Se repuso", self.mesa)
        self.assertIn('Incidencias <span class="pill warn">2</span>', self._menu())  # resuelta sin cerrar cuenta
        cerrar(primera, self.mesa)
        self.assertIn('Incidencias <span class="pill warn">1</span>', self._menu())

    def test_el_portal_cuenta_solo_las_de_su_cliente(self):
        """Chema 2026-09-23: el portal también lleva el conteo, acotado a su cliente."""
        otro = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        abrir_incidencia(self.colima, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL, texto="Retraso")
        abrir_incidencia(otro, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL, texto="Ajena")
        karina = get_user_model().objects.create_user("karina", password="x12345678")
        PerfilUsuario.objects.create(usuario=karina, rol="portal", cliente=self.colima)
        self.client.force_login(karina)
        html = self.client.get(reverse("portal:pedidos")).content.decode()
        self.assertIn('Incidencias <span class="pill warn">1</span>', html)
        self.assertEqual(html.count('<span class="pill warn">1</span>'), 1)
