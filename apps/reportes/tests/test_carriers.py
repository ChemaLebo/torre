"""Incidencias por paquetería y zona (Chema 2026-09-30)."""
from datetime import timedelta

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.services import registrar_evento
from apps.envios.models import Guia
from apps.incidencias.models import Incidencia
from apps.pedidos.models import Pedido
from apps.reportes import carriers
from apps.reportes.base import limites

from .base import ReportesTestCase


class CarriersTests(ReportesTestCase):
    def setUp(self):
        self.ahora = timezone.now()
        hoy = timezone.localdate()
        self.inicio, self.fin = limites(hoy - timedelta(days=7), hoy)
        self.pedidos = []
        for i, (carrier, cp, estado) in enumerate((
            ("estafeta", "44100", Guia.ENTREGADO), ("estafeta", "44100", Guia.EN_TRANSITO),
            ("noventa9Minutos", "06600", Guia.ENTREGADO), ("noventa9Minutos", "64460", Guia.ENTREGADO),
        )):
            pedido = Pedido.objects.create(cliente=self.colima, comprador_nombre=f"C{i}", cp=cp, estado=Pedido.ENTREGADO)
            Guia.objects.create(pedido=pedido, carrier=carrier, numero=f"G-{i}", proveedor="mock", estado=estado)
            self.pedidos.append(pedido)
        # Estafeta: un daño (guía sustituida y luego entregada = duplicada) y un retraso.
        Incidencia.objects.create(cliente=self.colima, pedido=self.pedidos[0], tipo=Incidencia.TIPO_DAN,
                                  origen=Incidencia.ORIGEN_COMPRADOR, ts_apertura=self.ahora - timedelta(days=1))
        Incidencia.objects.create(cliente=self.colima, pedido=self.pedidos[1], tipo=Incidencia.TIPO_RET,
                                  origen=Incidencia.ORIGEN_AUTO, ts_apertura=self.ahora - timedelta(days=1))
        g0 = Guia.objects.get(numero="G-0")
        g0.sustituida_motivo = "danada"
        g0.save(update_fields=["sustituida_motivo"])
        registrar_evento("guia", g0.pk, "entrega_duplicada", cliente=self.colima)
        # 99minutos a Monterrey (nacional): una incidencia interna, que el portal no ve.
        Incidencia.objects.create(cliente=self.colima, pedido=self.pedidos[3], tipo=Incidencia.TIPO_PAQ,
                                  origen=Incidencia.ORIGEN_AUTO, interna=True, ts_apertura=self.ahora - timedelta(days=1))

    def test_tabla_por_paqueteria_y_zona(self):
        r = carriers.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=True)
        filas = {(f[0], f[1]): f for f in r["filas"]}
        estafeta = filas[("estafeta", "metro")]
        self.assertEqual(estafeta[2:], [2, 1, 2, 100.0, 1, 1, 0, 0, 1, 1])
        self.assertEqual(filas[("noventa9Minutos", "local")][2:6], [1, 1, 0, 0.0])
        nacional = filas[("noventa9Minutos", "nacional")]
        self.assertEqual((nacional[4], nacional[9]), (1, 1))  # la interna cuenta como "otras" en Mesa
        self.assertEqual(filas[("Total", "")][2:5], [4, 3, 3])
        self.assertEqual(len(r["grupos"][0]["filas"]), 3)
        self.assertIn("paquete dañado", [f[9] for f in r["grupos"][0]["filas"]])
        # Portal: sin la interna.
        portal = carriers.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=False)
        self.assertEqual(len(portal["grupos"][0]["filas"]), 2)
        # Filtro de zona.
        solo_metro = carriers.generar(self.colima, self.inicio, self.fin, {"zona": "metro"}, es_mesa=True)
        self.assertEqual([f[0] for f in solo_metro["filas"]], ["estafeta", "Total"])

    def test_se_ve_en_el_indice_de_mesa_y_portal(self):
        self.client.force_login(self.mesa)
        self.assertContains(self.client.get(reverse("mesa:reportes")), "Incidencias por paquetería y zona")
        respuesta = self.client.get(reverse("mesa:reporte", args=["carriers"]))
        self.assertContains(respuesta, "estafeta")
        self.client.force_login(self.portal)
        self.assertContains(self.client.get(reverse("portal:reporte", args=["carriers"])), "noventa9Minutos")
