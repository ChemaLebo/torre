"""Reconciliación de inventario desde Mesa: exportación del conteo, previa por
CSV con modal de confirmación, aplicar firmado por Mesa y accesos por rol."""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.inventario.models import Ajuste, LineaASN, OrdenEntrada, Saldo


def crear_usuario(username, rol, cliente=None, pin=""):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente, pin=pin)
    return user


class BaseReconciliacionMesa(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.usuario_mesa = crear_usuario("mesa1", "mesa", pin="3333")
        cls.usuario_piso = crear_usuario("piso1", "piso", pin="1111")
        cls.usuario_portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.ubic_recepcion = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        cls.ubic_picking = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        cls.sku = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six pack", requiere_lote=False,
        )
        cls.url = reverse("mesa:inventario_reconciliar") + "?cliente=colima"
        cls.url_export = reverse("mesa:inventario_exportar_conteo") + "?cliente=colima"

    def setUp(self):
        from apps.inventario.services import recibir, ubicar

        orden = OrdenEntrada.objects.create(cliente=self.colima)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=20)
        recibir(linea, 20, 0, self.usuario_piso)
        ubicar(self.sku, 20, self.ubic_picking, None, self.usuario_piso)
        self.client.force_login(self.usuario_mesa)

    def vendible(self):
        return Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).aggregate(
            t=Sum("cantidad")
        )["t"] or 0

    def archivo(self, texto, nombre="conteo.csv"):
        return SimpleUploadedFile(nombre, texto.encode("utf-8"), content_type="text/csv")


class AccesoTests(BaseReconciliacionMesa):
    def test_portal_y_piso_no_entran(self):
        for usuario in (self.usuario_portal, self.usuario_piso):
            self.client.force_login(usuario)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.get(self.url_export).status_code, 403)

    def test_anonimo_va_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_botones_en_el_resumen_de_inventario(self):
        respuesta = self.client.get(reverse("mesa:inventario") + "?cliente=colima")
        self.assertContains(respuesta, "Exportar conteo")
        self.assertContains(respuesta, "Reconciliar por CSV")


class ExportarTests(BaseReconciliacionMesa):
    def test_csv_con_bom_encabezado_y_fila(self):
        respuesta = self.client.get(self.url_export)
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("conteo-colima-", respuesta["Content-Disposition"])
        cuerpo = respuesta.content.decode("utf-8")
        self.assertTrue(cuerpo.startswith("﻿codigo,descripcion,lote,caducidad,ubicacion,vendible_actual,contado"))
        self.assertIn("COLIMITA-SIX,Colimita six pack,,,A-01-1,20,20", cuerpo)


class PreviaYAplicarTests(BaseReconciliacionMesa):
    CSV = "codigo,descripcion,lote,caducidad,ubicacion,vendible_actual,contado\nCOLIMITA-SIX,Colimita six pack,,,A-01-1,20,17\n"

    def test_get_muestra_los_dos_pasos(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Exportar conteo (CSV)")
        self.assertContains(respuesta, "Ver previa")

    def test_previa_muestra_delta_y_hidden_con_el_csv(self):
        respuesta = self.client.post(self.url, {
            "accion": "previa", "cliente": "colima", "archivo": self.archivo(self.CSV),
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "<b>-3</b>", html=False)
        self.assertContains(respuesta, 'name="csv_conteo"')
        self.assertContains(respuesta, "Aplicar reconciliación")
        self.assertEqual(self.vendible(), 20)

    def test_previa_con_errores_no_ofrece_aplicar(self):
        csv = self.CSV + "NOEXISTE,,,,,,3\n"
        respuesta = self.client.post(self.url, {
            "accion": "previa", "cliente": "colima", "archivo": self.archivo(csv),
        })
        self.assertContains(respuesta, "SKU desconocido")
        self.assertNotContains(respuesta, "Aplicar reconciliación")

    def test_sin_archivo_o_ilegible_avisa(self):
        respuesta = self.client.post(self.url, {"accion": "previa", "cliente": "colima"}, follow=True)
        self.assertContains(respuesta, "Adjunta el CSV")
        respuesta = self.client.post(self.url, {
            "accion": "previa", "cliente": "colima", "archivo": self.archivo("sku,piezas\nA,1\n"),
        }, follow=True)
        self.assertContains(respuesta, "codigo y contado")

    def test_previa_trae_el_modal_de_confirmacion_con_el_resumen(self):
        respuesta = self.client.post(self.url, {
            "accion": "previa", "cliente": "colima", "archivo": self.archivo(self.CSV),
        })
        self.assertContains(respuesta, "Confirmar y aplicar")
        self.assertContains(respuesta, "<b>1</b> ajuste(s)")
        self.assertContains(respuesta, "−3</span> bajan")
        self.assertNotContains(respuesta, "pin_1")

    def test_aplicar_firma_el_usuario_de_mesa_y_regresa_al_inventario(self):
        respuesta = self.client.post(self.url, {
            "accion": "aplicar", "cliente": "colima", "csv_conteo": self.CSV,
            "archivo_nombre": "conteo.csv", "motivo": Ajuste.MOTIVO_RECONCILIACION_INV,
            "nota": "Prueba",
        }, follow=True)
        self.assertRedirects(respuesta, reverse("mesa:inventario") + "?cliente=colima")
        self.assertContains(respuesta, "Reconciliación aplicada: 1 ajuste(s)")
        self.assertEqual(self.vendible(), 17)
        ajuste = Ajuste.objects.get()
        self.assertEqual((ajuste.delta, ajuste.motivo), (-3, Ajuste.MOTIVO_RECONCILIACION_INV))
        self.assertEqual((ajuste.autorizo_1, ajuste.autorizo_2), ("mesa1", "mesa1"))
        self.assertTrue(EventoAuditoria.objects.filter(accion="reconciliacion_csv").exists())

    def test_aplicar_con_motivo_invalido_no_mueve_y_vuelve_a_la_previa(self):
        respuesta = self.client.post(self.url, {
            "accion": "aplicar", "cliente": "colima", "csv_conteo": self.CSV, "motivo": "inventado",
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "fuera del catálogo")
        self.assertContains(respuesta, "Confirmar y aplicar")
        self.assertEqual(self.vendible(), 20)
        self.assertEqual(Ajuste.objects.count(), 0)

    def test_aplicar_sin_csv_redirige(self):
        respuesta = self.client.post(self.url, {"accion": "aplicar", "cliente": "colima"}, follow=True)
        self.assertContains(respuesta, "vuelve a subirlo")
