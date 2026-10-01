"""Mesa → Finanzas: la vista por corte (quincena) con los dos estados de
cuenta por cliente (el motor se prueba en apps.finanzas.tests.test_motor)."""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente
from apps.finanzas.cortes import Corte, corte_actual, corte_de
from apps.finanzas.services import registrar_reembolso
from apps.finanzas.tests.base import asn, crear_pedido, crear_usuario, guia, paquete


class VistaFinanzasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", facturacion_desde=date(2026, 1, 1))
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.cliente)
        pedido = crear_pedido(cls.cliente, "PED-F0100")
        p1 = paquete(pedido, 1, "12.00")
        cls.guia = guia(pedido, "local", "100", p1)

    def test_mesa_ve_el_corte_actual_con_los_dos_estados_de_cuenta(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertEqual(respuesta.status_code, 200)
        html = respuesta.content.decode()
        corte = corte_actual()
        for esperado in (
            "Finanzas", "Cervecería Colima", corte.etiqueta, f"?corte={corte.anterior().clave}", f"?corte={corte.siguiente().clave}",
            "Estado de cuenta 1 · Fulfillment", "Estado de cuenta 2 · Guías", "Almacenaje", "½ de $18,000 al mes",
            "Picking (alistamiento)", "IVA 16%", "G-PED-F0100-local", "$129.00",
            "Estado × volumen", "Ciudad de México",
        ):
            self.assertIn(esperado, html)

    def test_reembolso_de_corte_anterior_aparece_en_el_corte_en_que_llega(self):
        from datetime import timedelta

        from apps.envios.models import Guia

        corte = corte_actual()
        inicio, _fin = corte.limites()
        Guia.objects.filter(pk=self.guia.pk).update(creado=inicio - timedelta(days=20))
        self.guia.refresh_from_db()
        registrar_reembolso(self.guia, "mesa1", origen="reclamacion", nota="pagó 99minutos")
        self.client.login(username="mesa1", password="x12345678")
        html = self.client.get(reverse("mesa:finanzas")).content.decode()
        self.assertIn("Reembolsos de cortes anteriores", html)
        self.assertIn(f"cobrada el {corte_de(self.guia.creado).etiqueta_corta}", html)
        self.assertIn("−$129.00", html)
        self.assertIn("pagó 99minutos", html)
        # En el corte anterior la guía sigue cobrada, sin el reembolso.
        html = self.client.get(reverse("mesa:finanzas"), {"corte": corte_de(self.guia.creado).clave}).content.decode()
        self.assertNotIn("Reembolsos de cortes anteriores", html)
        self.assertIn("G-PED-F0100-local", html)

    def test_portal_no_entra(self):
        self.client.login(username="karina", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"))
        self.assertNotEqual(respuesta.status_code, 200)

    def test_corte_invalido_cae_al_actual(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"corte": "chorizo"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, corte_actual().etiqueta)

    def test_corte_sin_actividad_no_truena(self):
        self.client.login(username="mesa1", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"corte": "2020-01-2"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "2ª quincena de enero 2020")


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
        # Descargada en un 2º corte fijo: el mínimo mensual se evalúa ahí con el mes completo.
        cls.corte = Corte(2026, 9, 2)
        asn(cls.cliente, tarimas_recibidas=16, descarga=cls.corte.limites()[0] + timedelta(days=2))

    def test_corte_solo_recepcion_aparece_con_su_facturacion(self):
        self.client.login(username="mesa2", password="x12345678")
        respuesta = self.client.get(reverse("mesa:finanzas"), {"corte": self.corte.clave})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Mayorista Tarimas")
        self.assertContains(respuesta, "3,040")  # 16 tarimas × $190
        self.assertContains(respuesta, "La recepción se factura por tarima recibida")
        # El mes facturó 0 + 3,040 < mínimo 12,000 → la línea de ajuste sale en el 2º corte.
        self.assertContains(respuesta, "Ajuste a mínimo mensual")
        self.assertContains(respuesta, "8,960")  # 12,000 − 3,040
