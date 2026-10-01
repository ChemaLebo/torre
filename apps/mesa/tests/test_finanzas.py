"""Mesa → Finanzas: la vista del estado de resultados por cliente (el motor
se prueba en apps.finanzas.tests.test_motor)."""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Cliente
from apps.finanzas.tests.base import asn, crear_pedido, crear_usuario, guia, paquete


class VistaFinanzasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.cliente)
        pedido = crear_pedido(cls.cliente, "PED-F0100")
        p1 = paquete(pedido, 1, "12.00")
        guia(pedido, "local", "100", p1)

    def test_mesa_ve_finanzas_con_numeros(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Finanzas")
        self.assertContains(respuesta, "Cervecería Colima")
        # Tabla estado × volumen alimentada por las guías del mes
        self.assertContains(respuesta, "Estado × volumen")
        self.assertContains(respuesta, "Ciudad de México")

    def test_portal_no_entra(self):
        self.client.login(username="karina", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertNotEqual(respuesta.status_code, 200)

    def test_mes_invalido_cae_al_mes_actual(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"mes": "chorizo"})
        self.assertEqual(respuesta.status_code, 200)

    def test_mes_sin_actividad_no_truena(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"mes": "2020-01"})
        self.assertEqual(respuesta.status_code, 200)


class VistaFinanzasRecepcionTests(TestCase):
    """Un mes de SOLO recepción (sin guías) también aparece en la vista."""

    @classmethod
    def setUpTestData(cls):
        cls.mesa = crear_usuario("mesa2", "mesa")
        # Cliente inactivo (sin pedidos ni guías): solo recibió tarimas este mes.
        cls.cliente = Cliente.objects.create(
            nombre="Mayorista Tarimas", slug="mayorista-tarimas", activo=False,
            tarifario={"recepcion_tarima": 190, "minimo_mes": 12000},
        )
        asn(cls.cliente, tarimas_recibidas=16, descarga=timezone.now())

    def test_mes_solo_recepcion_aparece_con_su_facturacion(self):
        self.client.login(username="mesa2", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Mayorista Tarimas")
        self.assertContains(respuesta, "3,040")  # 16 tarimas × $190
        self.assertContains(respuesta, "La recepción se factura por tarima recibida")
        # Facturó 3,040 < mínimo 12,000 → la línea de ajuste sale con su pill.
        self.assertContains(respuesta, "Ajuste a mínimo mensual")
        self.assertContains(respuesta, "8,960")  # 12,000 − 3,040
