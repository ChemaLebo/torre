"""Etiqueta de envío imprimible (10×15) con QR a la página del repartidor.

La URL destino del QR vive en el atributo data-qr del div: así se verifica
server-side (sin ejecutar JS) que apunte a la página pública /r/e/<token>/.
Los casos lat/lng vs dirección urlencodeada viven en los tests de la página
pública (apps/rastreo/tests/test_etiqueta_repartidor.py): ahí vive esa lógica.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.envios.models import Guia, Paquete
from apps.pedidos.models import Pedido
from apps.rastreo.models import AccesoEtiqueta

from .base import PisoTestCase

DIRECCION_BASE = {
    "address1": "Av. Prueba 123",
    "city": "Guadalajara",
    "province": "Jalisco",
    "zip": "44100",
}


class EtiquetaTestCase(PisoTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.usuario_mesa = User.objects.create_user("mesa1", password="mesa")
        PerfilUsuario.objects.create(usuario=cls.usuario_mesa, rol=PerfilUsuario.ROL_MESA)

    # ── Helpers ──

    def crear_guia(self, pedido=None, paquete=None, direccion=None):
        """Pedido GUIA_GENERADA + guía activa, sin tocar inventario (la etiqueta es lectura)."""
        if pedido is None:
            pedido = self.crear_pedido(
                estado=Pedido.GUIA_GENERADA, reservar_stock=False,
                direccion=dict(direccion if direccion is not None else DIRECCION_BASE),
            )
        return Guia.objects.create(
            pedido=pedido, paquete=paquete,
            carrier="paquetexpress", servicio="terrestre", numero="PQX-0001",
        )

    def get_etiqueta(self, guia):
        return self.client.get(reverse("piso:etiqueta", args=[guia.pk]))


class AccesoEtiquetaTests(EtiquetaTestCase):
    def test_rol_piso_ve_la_etiqueta(self):
        guia = self.crear_guia()
        self.login_piso()
        self.assertEqual(self.get_etiqueta(guia).status_code, 200)

    def test_rol_mesa_ve_la_etiqueta(self):
        guia = self.crear_guia()
        self.client.force_login(self.usuario_mesa)
        self.assertEqual(self.get_etiqueta(guia).status_code, 200)

    def test_rol_portal_no_ve_la_etiqueta(self):
        guia = self.crear_guia()
        self.client.force_login(self.usuario_portal)
        self.assertEqual(self.get_etiqueta(guia).status_code, 403)

    def test_anonimo_redirige_a_login(self):
        guia = self.crear_guia()
        respuesta = self.get_etiqueta(guia)
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/entrar/", respuesta["Location"])


class QREtiquetaTests(EtiquetaTestCase):
    """El QR v2 codifica la página pública del repartidor, ya no maps directo."""

    def setUp(self):
        self.login_piso()

    def test_renderizar_crea_el_token_y_el_qr_apunta_a_la_pagina_publica(self):
        from apps.rastreo.services import url_publica_etiqueta

        guia = self.crear_guia()
        self.assertFalse(AccesoEtiqueta.objects.filter(guia=guia).exists())
        respuesta = self.get_etiqueta(guia)
        acceso = AccesoEtiqueta.objects.get(guia=guia)  # la vista lo creó al renderizar
        self.assertContains(respuesta, f'data-qr="{url_publica_etiqueta(guia)}"')
        self.assertIn(f"/r/e/{acceso.token}/", respuesta.content.decode())

    def test_el_qr_ya_no_codifica_maps(self):
        guia = self.crear_guia(
            direccion=dict(DIRECCION_BASE, latitude=20.676722, longitude=-103.347222)
        )
        self.assertNotContains(self.get_etiqueta(guia), "maps.google.com")

    def test_el_token_es_estable_entre_renders(self):
        guia = self.crear_guia()
        primera = self.get_etiqueta(guia).content.decode()
        segunda = self.get_etiqueta(guia).content.decode()
        self.assertEqual(AccesoEtiqueta.objects.filter(guia=guia).count(), 1)
        token = AccesoEtiqueta.objects.get(guia=guia).token
        self.assertIn(f"/r/e/{token}/", primera)
        self.assertIn(f"/r/e/{token}/", segunda)

    def test_la_leyenda_menciona_el_boton_de_ayuda(self):
        guia = self.crear_guia()
        respuesta = self.get_etiqueta(guia)
        self.assertContains(respuesta, "botón de ayuda si no encuentras")


class ContenidoEtiquetaTests(EtiquetaTestCase):
    def setUp(self):
        self.login_piso()

    def test_con_address2_las_referencias_aparecen(self):
        direccion = dict(DIRECCION_BASE, address2="Depto 4B, portón negro")
        guia = self.crear_guia(direccion=direccion)
        respuesta = self.get_etiqueta(guia)
        self.assertContains(respuesta, "Depto 4B, portón negro")
        self.assertNotContains(respuesta, "Sin referencias")

    def test_sin_referencias_manda_al_qr(self):
        guia = self.crear_guia(direccion=DIRECCION_BASE)
        respuesta = self.get_etiqueta(guia)
        self.assertContains(respuesta, "Sin referencias — usa el QR")

    def test_paquete_1_de_2_con_plan_de_dos_bultos(self):
        pedido = self.crear_pedido(
            estado=Pedido.GUIA_GENERADA, reservar_stock=False,
            direccion=dict(DIRECCION_BASE),
        )
        paquete_1 = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("7.50"), carrier="paquetexpress",
        )
        Paquete.objects.create(
            pedido=pedido, numero=2, peso_kg=Decimal("5.00"), carrier="paquetexpress",
        )
        guia = self.crear_guia(pedido=pedido, paquete=paquete_1)
        respuesta = self.get_etiqueta(guia)
        self.assertContains(respuesta, "Paquete 1 de 2")
        self.assertContains(respuesta, "7.50 kg")

    def test_guia_legacy_sin_paquete_es_1_de_1(self):
        guia = self.crear_guia()  # paquete=None
        respuesta = self.get_etiqueta(guia)
        self.assertContains(respuesta, "Paquete 1 de 1")

    def test_no_imprime_el_valor_declarado(self):
        pedido = self.crear_pedido(
            estado=Pedido.GUIA_GENERADA, reservar_stock=False,
            direccion=dict(DIRECCION_BASE), valor_declarado=Decimal("1234.56"),
        )
        guia = self.crear_guia(pedido=pedido)
        self.assertNotContains(self.get_etiqueta(guia), "1234.56")


class BotonEtiquetaEnSalidaTests(EtiquetaTestCase):
    def test_salida_muestra_el_boton_etiqueta_por_guia_activa(self):
        guia = self.crear_guia()
        self.login_piso()
        respuesta = self.client.get(reverse("piso:salida"))
        self.assertContains(respuesta, "Etiqueta")
        self.assertContains(respuesta, reverse("piso:etiqueta", args=[guia.pk]))
