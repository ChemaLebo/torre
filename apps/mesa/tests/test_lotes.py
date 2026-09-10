"""Página de lotes del cliente en Mesa: lista, alta, corrección de caducidad, accesos."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU, Lote
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario


def crear_usuario(username, rol, cliente=None):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


class LotesMesaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.sku = SKU.objects.create(cliente=cls.colima, codigo="PARAMO-SIX", descripcion="Páramo")
        cls.kit = SKU.objects.create(cliente=cls.colima, codigo="TEABOX", descripcion="TeaBox", es_kit=True)
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.piso = crear_usuario("piso1", "piso")
        cls.url = reverse("mesa:cliente_lotes", args=[cls.colima.pk])

    def setUp(self):
        self.client.force_login(self.mesa)

    def test_piso_no_entra_y_anonimo_va_a_login(self):
        self.client.force_login(self.piso)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_lista_con_piezas_y_boton_en_la_ficha(self):
        Lote.objects.create(sku=self.sku, codigo="L-1", fecha_caducidad=date(2027, 1, 1))
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "L-1")
        self.assertContains(respuesta, "01/Ene/2027")
        ficha = self.client.get(reverse("mesa:cliente_detalle", args=[self.colima.pk]))
        self.assertContains(ficha, self.url)

    def test_alta_y_duplicado(self):
        respuesta = self.client.post(self.url, {
            "accion": "nuevo", "sku_id": self.sku.pk, "codigo": "L-2", "caducidad": "2027-06-01",
        }, follow=True)
        self.assertContains(respuesta, "dado de alta")
        lote = Lote.objects.get(sku=self.sku, codigo="L-2")
        self.assertEqual(lote.fecha_caducidad, date(2027, 6, 1))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="lote", accion="alta").exists())
        respuesta = self.client.post(self.url, {"accion": "nuevo", "sku_id": self.sku.pk, "codigo": "L-2"}, follow=True)
        self.assertContains(respuesta, "ya existe")
        self.assertEqual(Lote.objects.filter(sku=self.sku).count(), 1)

    def test_kit_no_admite_lotes(self):
        respuesta = self.client.post(self.url, {"accion": "nuevo", "sku_id": self.kit.pk, "codigo": "L-K"})
        self.assertEqual(respuesta.status_code, 404)

    def test_corregir_caducidad_y_fecha_invalida(self):
        lote = Lote.objects.create(sku=self.sku, codigo="L-3")
        self.client.post(self.url, {"accion": "caducidad", "lote_id": lote.pk, "caducidad": "2027-09-09"})
        lote.refresh_from_db()
        self.assertEqual(lote.fecha_caducidad, date(2027, 9, 9))
        respuesta = self.client.post(self.url, {"accion": "caducidad", "lote_id": lote.pk, "caducidad": "9/9/27"}, follow=True)
        self.assertContains(respuesta, "no se entiende")
