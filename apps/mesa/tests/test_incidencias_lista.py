"""Lista de incidencias en Mesa: columna y filtro de origen (Chema
2026-09-24), para separar las automáticas del poller de las que levantan el
cliente o el comprador."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia


class BusquedaYColumnasTests(TestCase):
    """Chema 2026-09-28: el pedido en la segunda columna y una caja de búsqueda
    por folio, pedido, orden de Shopify, comprador o dueño; las tablas de Torre
    cargan el script que las ordena y les pone buscador."""

    def setUp(self):
        from .test_vistas import crear_pedido

        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.pedido = crear_pedido(self.colima, folio="PED-00047", shopify_order_name="#33713", comprador_nombre="Dulce Espinoza")
        self.con_pedido = abrir_incidencia(self.colima, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=self.pedido, texto="x")
        self.suelta = abrir_incidencia(self.colima, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO, texto="descuadre")
        self.suelta.dueno = "karina"
        self.suelta.save(update_fields=["dueno"])
        self.url = reverse("mesa:incidencias")

    def test_el_pedido_va_en_la_segunda_columna_con_su_orden_y_comprador(self):
        html = self.client.get(self.url).content.decode()
        self.assertLess(html.index("<th>Pedido</th>"), html.index("<th>Tipo</th>"))
        self.assertIn("#33713 · Dulce Espinoza", html)
        self.assertIn("js/tablas.js", html)

    def test_busqueda(self):
        casos = {
            "33713": (self.con_pedido, self.suelta), "#33713": (self.con_pedido, self.suelta),
            "ped-00047": (self.con_pedido, self.suelta), "dulce": (self.con_pedido, self.suelta),
            self.suelta.folio: (self.suelta, self.con_pedido), "karina": (self.suelta, self.con_pedido),
        }
        for q, (si, no) in casos.items():
            html = self.client.get(self.url, {"q": q}).content.decode()
            self.assertIn(si.folio, html, q)
            self.assertNotIn(no.folio, html, q)


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
