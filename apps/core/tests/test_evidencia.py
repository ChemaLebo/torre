"""La evidencia sale SOLO por la vista con autorización: MEDIA no tiene auth
(y ya no tiene ruta ni en dev). piso/mesa ven todo; portal solo lo suyo."""
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, EvidenciaFoto, PerfilUsuario
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.inventario.models import OrdenEntrada

MEDIA_TESTS = tempfile.mkdtemp(prefix="torre-media-evidencia-")
User = get_user_model()


def crear_usuario(nombre, rol, cliente=None):
    usuario = User.objects.create_user(username=nombre, password="clave-123")
    PerfilUsuario.objects.create(usuario=usuario, rol=rol, cliente=cliente)
    return usuario


@override_settings(MEDIA_ROOT=MEDIA_TESTS)
class EvidenciaAutorizadaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_TESTS, ignore_errors=True)

    def setUp(self):
        self.colima = crear_cliente()
        self.nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        self.tienda = crear_tienda(self.colima)
        self.pedido = crear_pedido(self.colima, self.tienda, cp="06600")
        self.foto = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(self.pedido.pk), tipo="contenido",
            archivo=SimpleUploadedFile("contenido.jpg", b"jpegdemo"),
        )
        self.url = reverse("core:evidencia", args=[self.foto.pk])

    def test_anonimo_va_al_login(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/entrar/", respuesta.url)

    def test_portal_ve_la_evidencia_de_su_pedido(self):
        self.client.force_login(crear_usuario("karina", "portal", self.colima))
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(b"".join(respuesta.streaming_content), b"jpegdemo")

    def test_portal_ajeno_recibe_404(self):
        self.client.force_login(crear_usuario("nocturno1", "portal", self.nocturno))
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_piso_y_mesa_ven_todo(self):
        for nombre, rol in (("piso9", "piso"), ("mesa9", "mesa")):
            self.client.force_login(crear_usuario(nombre, rol))
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_asn_por_folio_resuelve_el_tenant(self):
        """entidad_id con folio (no pk): la referencia mixta se resuelve igual."""
        orden = OrdenEntrada.objects.create(cliente=self.colima)
        foto = EvidenciaFoto.objects.create(
            entidad="asn", entidad_id=orden.folio, tipo="llegada",
            archivo=SimpleUploadedFile("llegada.jpg", b"asndemo"),
        )
        url = reverse("core:evidencia", args=[foto.pk])
        self.client.force_login(crear_usuario("karina2", "portal", self.colima))
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(crear_usuario("nocturno2", "portal", self.nocturno))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_referencia_irresoluble_es_404_para_portal(self):
        foto = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id="PED-99999", tipo="contenido",
            archivo=SimpleUploadedFile("huerfana.jpg", b"x"),
        )
        self.client.force_login(crear_usuario("karina3", "portal", self.colima))
        self.assertEqual(
            self.client.get(reverse("core:evidencia", args=[foto.pk])).status_code, 404
        )

    def test_media_ya_no_se_sirve_directo(self):
        """La ruta /media/ no existe ni en dev: dev = prod."""
        with override_settings(DEBUG=True):
            respuesta = self.client.get(f"/media/{self.foto.archivo.name}")
        self.assertEqual(respuesta.status_code, 404)
