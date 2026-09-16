"""Incidencias con su solución y daños en la entrega por paquetería."""
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EvidenciaFoto
from apps.envios.models import Guia
from apps.incidencias.models import Compensacion, Incidencia, ReclamacionCarrier
from apps.pedidos.models import Pedido
from apps.reportes import danos, incidencias
from apps.reportes.base import limites

from .base import ReportesTestCase


@override_settings(MEDIA_ROOT="/tmp/torre-test-reportes-inc")
class IncidenciasTests(ReportesTestCase):
    def setUp(self):
        self.ahora = timezone.now()
        hoy = timezone.localdate()
        self.inicio, self.fin = limites(hoy - timedelta(days=7), hoy)
        self.entregados = []
        for i, carrier in enumerate(("estafeta", "estafeta", "noventa9Minutos", "noventa9Minutos")):
            pedido = Pedido.objects.create(
                cliente=self.colima, comprador_nombre=f"C{i}", cp="44100", estado=Pedido.ENTREGADO,
                ts_entregado=self.ahora - timedelta(days=2),
            )
            Guia.objects.create(pedido=pedido, carrier=carrier, numero=f"G-{i}", proveedor="mock", estado=Guia.ENTREGADO)
            self.entregados.append(pedido)
        self.dan = Incidencia.objects.create(
            cliente=self.colima, pedido=self.entregados[0], sku=self.six, tipo=Incidencia.TIPO_DAN,
            origen=Incidencia.ORIGEN_COMPRADOR, ts_apertura=self.ahora - timedelta(days=1, hours=20),
            dueno="mesa1",
        )
        Compensacion.objects.create(incidencia=self.dan, tipo=Compensacion.TIPO_REPOSICION, monto=Decimal(300), estado=Compensacion.APROBADA)
        Compensacion.objects.create(incidencia=self.dan, tipo=Compensacion.TIPO_CUPON, monto=Decimal(50), estado=Compensacion.PAGADA)
        ReclamacionCarrier.objects.create(incidencia=self.dan, carrier="estafeta", monto_reclamado=Decimal(430), estado=ReclamacionCarrier.PRESENTADA)
        EvidenciaFoto.objects.create(entidad="incidencia", entidad_id=str(self.dan.pk), tipo="dano", archivo=ContentFile(b"\x89PNG", name="d.png"))
        self.ret = Incidencia.objects.create(
            cliente=self.colima, pedido=self.entregados[2], tipo=Incidencia.TIPO_RET, ts_apertura=self.ahora - timedelta(days=1),
        )
        Incidencia.objects.create(cliente=self.otro, tipo=Incidencia.TIPO_DES, ts_apertura=self.ahora - timedelta(days=1))
        Incidencia.objects.create(cliente=self.colima, tipo=Incidencia.TIPO_FAL, ts_apertura=self.ahora - timedelta(days=30))

    def test_incidencias_con_solucion_reclamacion_y_fotos(self):
        r = incidencias.generar(self.colima, self.inicio, self.fin, {"tipo": "", "abiertas": False}, es_mesa=True)
        self.assertEqual([f[0] for f in r["filas"]], [self.dan.folio, self.ret.folio])
        fila = r["filas"][0]
        self.assertEqual((fila[2], fila[3], fila[4]), (self.entregados[0].folio, "COLIMITA-SIX", "Daño / rotura"))
        self.assertEqual(fila[11], "reposición física $300.00 (aprobada); cupón $50.00 (pagada)")
        self.assertEqual(fila[12], Decimal("350.00"))
        self.assertEqual(fila[13], "estafeta $430.00 (presentada)")
        self.assertEqual((fila[14], fila[15]), (1, "mesa1"))
        self.assertEqual(r["filas"][1][11], "sin compensación")
        self.assertIsNone(r["filas"][1][12])
        self.assertIn(("incidencias", 2), r["resumen"])
        self.assertIn(("MXN compensados", Decimal("350.00")), r["resumen"])
        self.assertIn(("daño / rotura", 1), r["resumen"])
        # El portal no ve al dueño.
        portal = incidencias.generar(self.colima, self.inicio, self.fin, {"tipo": "", "abiertas": False}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(incidencias.COLUMNAS))

    def test_filtros_por_tipo_y_abiertas(self):
        solo_dan = incidencias.generar(self.colima, self.inicio, self.fin, {"tipo": "DAN", "abiertas": False}, es_mesa=False)
        self.assertEqual([f[0] for f in solo_dan["filas"]], [self.dan.folio])
        Incidencia.objects.filter(pk=self.ret.pk).update(estado=Incidencia.CERRADA)
        abiertas = incidencias.generar(self.colima, self.inicio, self.fin, {"tipo": "", "abiertas": True}, es_mesa=False)
        self.assertEqual([f[0] for f in abiertas["filas"]], [self.dan.folio])

    def test_danos_por_paqueteria_con_porcentaje_y_detalle(self):
        r = danos.generar(self.colima, self.inicio, self.fin, {}, es_mesa=False)
        filas = {f[0]: f for f in r["filas"]}
        self.assertEqual(filas["estafeta"][1:4], [2, 1, 50.0])
        self.assertEqual(filas["estafeta"][4], Decimal("350.00"))
        self.assertEqual(filas["noventa9Minutos"][1:4], [2, 0, 0.0])
        self.assertEqual(filas["Total"][1:4], [4, 1, 25.0])
        [g] = r["grupos"]
        [detalle] = g["filas"]
        self.assertEqual((detalle[0], detalle[2], detalle[3]), (self.entregados[0].folio, "estafeta", self.dan.folio))
        self.assertAlmostEqual(detalle[5], 4.0, places=1)  # reportado 4 h después de la entrega
        self.assertEqual((detalle[6], detalle[8], detalle[9]), ("Comprador final", 1, Decimal("350.00")))
        self.assertIn(("con daño", 1), r["resumen"])

    def test_danos_vacio_sin_entregas(self):
        inicio, fin = limites(timezone.localdate() - timedelta(days=60), timezone.localdate() - timedelta(days=50))
        r = danos.generar(self.colima, inicio, fin, {}, es_mesa=False)
        self.assertEqual(r["filas"], [])
        self.assertEqual(r["grupos"][0]["filas"], [])

    def test_vistas_mesa_y_portal(self):
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:reporte", args=["danos"]))
        self.assertContains(respuesta, "Pedidos con daño")
        self.assertContains(respuesta, "50.0%")
        self.client.force_login(self.portal)
        respuesta = self.client.get(reverse("portal:reporte", args=["incidencias"]), {"tipo": "DAN"})
        self.assertContains(respuesta, self.dan.folio)
        self.assertNotContains(respuesta, self.ret.folio)
        self.assertNotContains(respuesta, "<th>Dueño</th>")
