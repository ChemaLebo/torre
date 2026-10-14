"""Miniaturas y modal de fotos (Chema 2026-10-14): al guardar una evidencia
nace su miniatura JPEG de 640 px (el original y su hash no se tocan), la vista
las sirve con caché privada, y el comando genera las que falten."""
from io import BytesIO, StringIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.core.models import EvidenciaFoto, PerfilUsuario


def png(ancho=1600, alto=1200):
    imagen = Image.new("RGB", (ancho, alto), (200, 40, 40))
    salida = BytesIO()
    imagen.save(salida, format="PNG")
    return salida.getvalue()


@override_settings(MEDIA_ROOT="/tmp/torre-test-media-miniaturas")
class MiniaturasTests(TestCase):
    def setUp(self):
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol=PerfilUsuario.ROL_MESA)
        self.client.force_login(self.mesa)

    def test_al_guardar_nace_la_miniatura_y_el_original_no_cambia(self):
        contenido = png()
        foto = EvidenciaFoto.objects.create(entidad="pedido", entidad_id="1", tipo="contenido", archivo=ContentFile(contenido, name="caja.png"))
        self.assertTrue(foto.miniatura)
        self.assertIn("/mini/", foto.miniatura.name)
        with foto.miniatura.open("rb") as f:
            mini = Image.open(f)
            self.assertEqual((mini.format, max(mini.size)), ("JPEG", 640))
        with foto.archivo.open("rb") as f:
            self.assertEqual(f.read(), contenido)
        import hashlib
        self.assertEqual(foto.hash_sha256, hashlib.sha256(contenido).hexdigest())

    def test_un_archivo_que_no_es_imagen_no_tiene_miniatura_y_se_sirve_el_original(self):
        foto = EvidenciaFoto.objects.create(entidad="pedido", entidad_id="2", archivo=ContentFile(b"%PDF-1.4 no es imagen", name="doc.pdf"))
        self.assertFalse(foto.miniatura)
        r = self.client.get(reverse("core:evidencia_mini", args=[foto.pk]))
        self.assertEqual((r.status_code, r["Cache-Control"]), (200, "private, max-age=604800"))
        self.assertTrue(b"".join(r.streaming_content).startswith(b"%PDF"))

    def test_la_vista_mini_sirve_la_miniatura_y_la_normal_el_original(self):
        foto = EvidenciaFoto.objects.create(entidad="asn", entidad_id="ASN-0001", tipo="llegada", archivo=ContentFile(png(), name="llegada.png"))
        r = self.client.get(reverse("core:evidencia_mini", args=[foto.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(b"".join(r.streaming_content).startswith(b"\xff\xd8"))  # JPEG
        r = self.client.get(reverse("core:evidencia", args=[foto.pk]))
        self.assertTrue(b"".join(r.streaming_content).startswith(b"\x89PNG"))

    def test_el_comando_genera_las_que_faltan(self):
        foto = EvidenciaFoto.objects.create(entidad="pedido", entidad_id="3", archivo=ContentFile(png(300, 300), name="chica.png"))
        EvidenciaFoto.objects.filter(pk=foto.pk).update(miniatura="")
        salida = StringIO()
        call_command("generar_miniaturas", stdout=salida)
        self.assertIn("1 miniatura(s) generada(s)", salida.getvalue())
        foto.refresh_from_db()
        self.assertTrue(foto.miniatura)
        salida = StringIO()
        call_command("generar_miniaturas", stdout=salida)
        self.assertIn("0 miniatura(s)", salida.getvalue())

    def test_el_modal_global_esta_en_las_pantallas(self):
        r = self.client.get(reverse("mesa:bodega"))
        self.assertContains(r, 'id="modal-foto"')
        self.assertContains(r, "a.foto-ampliable")
