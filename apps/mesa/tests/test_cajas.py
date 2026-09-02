"""CRUD de cajas de empaque en Mesa (B1)."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import Caja
from apps.core.models import Cliente, PerfilUsuario


class CajasMesaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(
            nombre="Cajas Demo", slug="cajas-demo", integracion_envios="envia",
        )
        cls.usuario = User.objects.create_user("mesacajas", password="x12345")
        PerfilUsuario.objects.create(usuario=cls.usuario, rol="mesa")

    def setUp(self):
        self.client.force_login(self.usuario)
        self.url = reverse("mesa:cliente_cajas", args=[self.cliente.pk])

    def _datos(self, **extra):
        base = {"nombre": "Mediana", "largo_cm": 30, "ancho_cm": 20,
                "alto_cm": 15, "peso_gr": 180, "activo": "on"}
        base.update(extra)
        return base

    def test_alta_lista_edicion_y_nombre_duplicado(self):
        r = self.client.post(self.url, self._datos(), follow=True)
        self.assertContains(r, "Mediana")
        caja = Caja.objects.get(cliente=self.cliente, nombre="Mediana")
        self.assertEqual(caja.peso_gr, 180)

        # Edición conserva identidad y actualiza medidas.
        r = self.client.post(self.url, self._datos(caja_id=caja.pk, largo_cm=32), follow=True)
        caja.refresh_from_db()
        self.assertEqual(caja.largo_cm, 32)

        # Nombre duplicado en alta nueva → error, sin crear.
        r = self.client.post(self.url, self._datos(), follow=True)
        self.assertEqual(Caja.objects.filter(cliente=self.cliente).count(), 1)
        self.assertContains(r, "Ya hay una caja")

    def test_piso_no_entra(self):
        piso = User.objects.create_user("pisocajas", password="x12345")
        PerfilUsuario.objects.create(usuario=piso, rol="piso")
        self.client.force_login(piso)
        r = self.client.get(self.url)
        self.assertNotEqual(r.status_code, 200)
