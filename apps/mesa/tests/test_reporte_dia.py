"""Reporte del día en Mesa: toda la bodega, filtro por cliente y fecha, operador
por etapa, CSV y accesos por rol."""
from datetime import datetime, time, timedelta

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Cliente, EvidenciaFoto
from apps.pedidos.models import Pedido

from .test_vistas import crear_usuario


@override_settings(MEDIA_ROOT="/tmp/torre-test-reporte-mesa")
class ReporteDiaMesaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.piso = crear_usuario("piso1", "piso")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.url = reverse("mesa:reporte_dia")
        cls.url_csv = reverse("mesa:reporte_dia_csv")
        cls.hoy = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.mesa)
        self.p_colima = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp="28017")
        self.p_colima.transicionar(Pedido.EN_PICKING, actor=self.piso)
        self.foto = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(self.p_colima.pk), tipo="contenido",
            archivo=ContentFile(b"\x89PNG", name="c.png"), tomada_por="piso1",
        )
        self.p_nocturno = Pedido.objects.create(cliente=self.nocturno, comprador_nombre="Luis", cp="06600")
        ayer = timezone.make_aware(datetime.combine(self.hoy - timedelta(days=1), time(9)))
        self.p_ayer = Pedido.objects.create(cliente=self.colima, comprador_nombre="Eva", cp="28017")
        Pedido.objects.filter(pk=self.p_ayer.pk).update(creado=ayer, actualizado=ayer)

    def test_toda_la_bodega_con_operador_y_fotos(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, self.p_colima.folio)
        self.assertContains(respuesta, self.p_nocturno.folio)
        self.assertNotContains(respuesta, self.p_ayer.folio)
        self.assertContains(respuesta, "· piso1")  # quién hizo el picking
        self.assertContains(respuesta, reverse("core:evidencia", args=[self.foto.pk]))
        self.assertContains(respuesta, "Toda la bodega")
        self.assertContains(respuesta, 'class="nav-grupo" data-grupo="mesa-reportes"')

    def test_filtro_por_cliente_y_fecha(self):
        respuesta = self.client.get(self.url, {"cliente": "colima"})
        self.assertContains(respuesta, self.p_colima.folio)
        self.assertNotContains(respuesta, self.p_nocturno.folio)
        ayer = (self.hoy - timedelta(days=1)).isoformat()
        respuesta = self.client.get(self.url, {"fecha": ayer, "cliente": "colima"})
        self.assertContains(respuesta, self.p_ayer.folio)
        self.assertNotContains(respuesta, self.p_colima.folio)
        self.assertContains(respuesta, "Día siguiente")
        respuesta = self.client.get(self.url, {"fecha": "ayer"}, follow=True)
        self.assertContains(respuesta, "no se entiende")

    def test_csv(self):
        respuesta = self.client.get(self.url_csv, {"cliente": "colima"})
        self.assertEqual(respuesta["Content-Type"], "text/csv; charset=utf-8")
        lineas = respuesta.content.decode("utf-8-sig").splitlines()
        self.assertEqual(lineas[0].split(",")[0], "folio")
        self.assertEqual(len(lineas), 2)
        self.assertIn(self.p_colima.folio, lineas[1])
        self.assertIn(f"/evidencia/{self.foto.pk}/", lineas[1])

    def test_portal_y_piso_no_entran(self):
        for usuario in (self.portal, self.piso):
            self.client.force_login(usuario)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.get(self.url_csv).status_code, 403)
