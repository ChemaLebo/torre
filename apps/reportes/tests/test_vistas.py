"""Vistas genéricas de reportes en Mesa y portal: índice, página, CSV,
rango de fechas, cliente en Mesa (uno o todos) y acceso por rol."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import Lote
from apps.inventario.models import Saldo
from apps.reportes.base import rango_desde_get

from .base import ReportesTestCase


class VistasReportesTests(ReportesTestCase):
    def setUp(self):
        lote = Lote.objects.create(sku=self.six, codigo="LC-01", fecha_caducidad=timezone.localdate() + timedelta(days=5))
        Saldo.objects.create(sku=self.six, ubicacion=self.picking, lote=lote, estado=Saldo.UBICADO_VENDIBLE, cantidad=12)
        Saldo.objects.create(sku=self.ajeno, ubicacion=self.picking, estado=Saldo.UBICADO_VENDIBLE, cantidad=4)

    def test_indice_y_reporte_en_mesa_para_todos_los_clientes(self):
        self.client.force_login(self.mesa)
        indice = self.client.get(reverse("mesa:reportes"))
        self.assertContains(indice, "Existencias por SKU y lote")
        self.assertContains(indice, reverse("mesa:reporte", args=["lotes"]))
        respuesta = self.client.get(reverse("mesa:reporte", args=["existencias"]))
        self.assertContains(respuesta, "<th>Cliente</th>")
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertContains(respuesta, "MEZ-750")
        self.assertContains(respuesta, "Todos los clientes")
        # Con cliente: sin la columna Cliente y solo lo suyo.
        respuesta = self.client.get(reverse("mesa:reporte", args=["existencias"]), {"cliente": "colima"})
        self.assertNotContains(respuesta, "<th>Cliente</th>")
        self.assertNotContains(respuesta, "MEZ-750")
        self.assertContains(respuesta, "COLIMITA-SIX")

    def test_portal_solo_ve_lo_suyo_y_sin_selector(self):
        self.client.force_login(self.portal)
        indice = self.client.get(reverse("portal:reportes"))
        self.assertContains(indice, "Lotes y caducidad")
        respuesta = self.client.get(reverse("portal:reporte", args=["lotes"]))
        self.assertContains(respuesta, "LC-01")
        self.assertNotContains(respuesta, "MEZ-750")
        self.assertNotContains(respuesta, "Todos los clientes")

    def test_csv_con_bom_y_encabezados(self):
        self.client.force_login(self.portal)
        respuesta = self.client.get(reverse("portal:reporte_csv", args=["existencias"]), {"desde": "2026-09-01", "hasta": "2026-09-15"})
        self.assertEqual(respuesta["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("existencias-colima-2026-09-01-2026-09-15.csv", respuesta["Content-Disposition"])
        lineas = respuesta.content.decode("utf-8-sig").splitlines()
        self.assertTrue(lineas[0].startswith("SKU,Producto,Lote,Caducidad"))
        self.assertIn("COLIMITA-SIX", lineas[1])

    def test_fecha_invalida_avisa_y_clave_desconocida_es_404(self):
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:reporte", args=["existencias"]), {"desde": "ayer"}, follow=True)
        self.assertContains(respuesta, "no se entiende")
        self.assertEqual(self.client.get(reverse("mesa:reporte", args=["nada"])).status_code, 404)

    def test_rango_default_y_orden(self):
        hoy = timezone.localdate()
        self.assertEqual(rango_desde_get({}), (hoy.replace(day=1), hoy, True))
        desde, hasta, valido = rango_desde_get({"desde": "2026-09-10", "hasta": "2026-09-01"})
        self.assertEqual((desde.isoformat(), hasta.isoformat(), valido), ("2026-09-01", "2026-09-10", True))

    def test_piso_y_anonimo_no_entran(self):
        self.client.force_login(self.piso)
        self.assertEqual(self.client.get(reverse("mesa:reporte", args=["lotes"])).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("portal:reporte", args=["lotes"])).status_code, 302)
