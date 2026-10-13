"""Etiquetas de recepción 51×51 (Chema 2026-10-13): al terminar el conteo se
imprimen solas todas, tarima por tarima en orden inverso al escaneo (primero
la tarima, luego las cajas) en trabajos del tamaño de tanda; las cajas que
faltaron se imprimen al momento; Reimprimir saca la tarima completa; y el PDF
(caja, dañada, tarima, nombres largos)."""
from datetime import date

from django.test import override_settings
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.configuracion.models import TandaEtiquetas
from apps.core.models import EventoAuditoria
from apps.envios.models import TrabajoImpresion
from apps.inventario.models import EscaneoRecepcion, LineaASN, OrdenEntrada
from apps.inventario.services import contar_caja, crear_tarima, escaneos_lifo, tanda_etiquetas
from apps.piso.etiquetas import _fuente, generar_pdf_etiquetas, paginas_pdf

from .base import PisoTestCase


@override_settings(TORRE_MODO_IMPRESION="relay")
class EtiquetasRecepcionTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.sku.requiere_lote = True
        self.sku.variante = "24 Pack · Botella 355 ml"
        self.sku.save()
        self.sku_b = SKU.objects.create(cliente=self.cliente, codigo="CC24LCAR", codigo_barras="7501000000028", descripcion="Colimita 24 lata", requiere_lote=True)
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=1)
        self.linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=10, lote_codigo="L1", fecha_caducidad=date(2027, 4, 22))
        self.linea_b = LineaASN.objects.create(orden=self.orden, sku=self.sku_b, cantidad_anunciada=5, lote_codigo="B1")
        self.tarima = crear_tarima(self.operador)
        self.escaneos = [contar_caja(self.orden, self.sku, "L1", None, self.tarima, self.operador) for _ in range(3)]
        self.escaneos += [contar_caja(self.orden, self.sku_b, "B1", None, self.tarima, self.operador) for _ in range(2)]
        self.detalle = reverse("piso:recepcion_detalle", args=[self.orden.pk])
        self.client.post(self.detalle, {"accion": "foto", "foto_llegada": self.foto()})
        self.client.post(reverse("piso:recepcion_tarima", args=[self.orden.pk]), {"accion": "elegir", "tarima_id": self.tarima.pk})

    def terminar(self):
        return self.client.post(self.detalle, {"accion": "terminar_conteo"}, follow=True)

    def test_terminar_conteo_imprime_todo_tarima_por_tarima_en_orden_inverso(self):
        otra = crear_tarima(self.operador)
        self.client.post(reverse("piso:recepcion_tarima", args=[self.orden.pk]), {"accion": "elegir", "tarima_id": otra.pk})
        extra = [contar_caja(self.orden, self.sku, "L1", None, otra, self.operador) for _ in range(2)]
        self.assertEqual([e.pk for e in escaneos_lifo(self.orden, self.tarima)], [e.pk for e in reversed(self.escaneos)])
        r = self.terminar()
        self.assertContains(r, "Conteo terminado: 7 etiqueta(s) de caja más las de TAR-01, TAR-02 van a la impresora")
        trabajos = list(TrabajoImpresion.objects.order_by("pk"))
        self.assertEqual([(t.referencia, t.paginas) for t in trabajos],
                         [(f"{self.orden.folio} TAR-01 etiquetas 1-5", 6), (f"{self.orden.folio} TAR-02 etiquetas 1-2", 3)])
        self.assertEqual((trabajos[0].tipo, trabajos[0].impresora, trabajos[0].media, trabajos[0].cliente, trabajos[0].guia), ("producto", "etiquetas", "Custom.51x51mm", self.cliente, None))
        with trabajos[0].pdf.open("rb") as archivo:
            pdf = archivo.read()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(paginas_pdf(pdf), 6)
        self.assertEqual(EscaneoRecepcion.objects.filter(orden=self.orden, impresa_ts__isnull=False).count(), 7)
        eventos = EventoAuditoria.objects.filter(entidad="asn", entidad_id=self.orden.folio, accion="etiquetas_impresas").order_by("pk")
        self.assertEqual([(e.delta["tarima"], e.delta["desde"], e.delta["hasta"], e.delta["con_tarima"]) for e in eventos], [("TAR-01", 1, 5, True), ("TAR-02", 1, 2, True)])
        # Regresar a conteo y terminar otra vez: solo la caja nueva, numerada a continuación (3/3), sin etiqueta de tarima.
        self.client.post(reverse("piso:recepcion_reetiquetado", args=[self.orden.pk]), {"accion": "regresar_conteo"})
        contar_caja(self.orden, self.sku, "L1", None, otra, self.operador)
        r = self.terminar()
        self.assertContains(r, "Conteo terminado: 1 etiqueta(s) de caja")
        ultimo = TrabajoImpresion.objects.latest("pk")
        self.assertEqual((ultimo.referencia, ultimo.paginas), (f"{self.orden.folio} TAR-02 etiquetas 3-3", 1))

    def test_tandas_cortas_parten_la_tarima_en_varios_trabajos_seguidos(self):
        TandaEtiquetas.objects.create(cajas=2)
        self.terminar()
        trabajos = list(TrabajoImpresion.objects.order_by("pk"))
        self.assertEqual([(t.referencia[-13:], t.paginas) for t in trabajos], [("etiquetas 1-2", 3), ("etiquetas 3-4", 2), ("etiquetas 5-5", 1)])
        tanda = tanda_etiquetas(self.orden, self.tarima, desde=2, tamano=10)
        self.assertEqual((tanda["desde"], tanda["hasta"], tanda["incluye_tarima"], len(tanda["escaneos"])), (2, 5, False, 4))

    def test_faltaron_imprime_al_momento_y_reimprimir_saca_la_tarima_completa(self):
        self.terminar()
        reetiquetado = reverse("piso:recepcion_reetiquetado", args=[self.orden.pk])
        antes = TrabajoImpresion.objects.count()
        r = self.client.post(reetiquetado, {"accion": "faltaron", "tarima_id": self.tarima.pk, "linea_id": self.linea.pk, "cantidad": 2}, follow=True)
        self.assertContains(r, "2 etiqueta(s) van a la impresora")
        trabajo = TrabajoImpresion.objects.latest("pk")
        self.assertEqual((TrabajoImpresion.objects.count(), trabajo.paginas, trabajo.referencia), (antes + 1, 2, f"{self.orden.folio} TAR-01 faltaron 2"))
        self.assertEqual(EscaneoRecepcion.objects.filter(orden=self.orden, impresa_ts__isnull=True).count(), 0)
        r = self.client.post(reetiquetado, {"accion": "reimprimir", "tarima_id": self.tarima.pk}, follow=True)
        self.assertContains(r, "TAR-01: 7 etiqueta(s) y la de la tarima van a la impresora")
        self.assertEqual(TrabajoImpresion.objects.latest("pk").paginas, 8)

    def test_pdf_caja_danada_tarima_y_nombre_largo(self):
        self.assertEqual(_fuente("Clash-700"), "Clash-700")  # las TTF del repo se registran
        items = [
            {"tipo": "tarima", "nombre": "TAR-09", "qr": "U|TAR-09"},
            {"tipo": "caja", "marca": "Cervecería de Colima", "sku": "CPL24", "producto": "Piedra Lisa", "presentacion": "24 Pack · Botella 355 ml",
             "lote": "CPL330ABCD", "caducidad": date(2027, 4, 22), "barras": "7503026716090", "qr": "P|7503026716090|CPL330ABCD", "danada": False},
            {"tipo": "caja", "marca": "Cervecería de Colima", "sku": "TICUS", "producto": "Ticús porter de temporada edición limitada invierno",
             "presentacion": "", "lote": "", "caducidad": None, "barras": "TICUS", "qr": "P|TICUS|", "danada": True},
        ]
        pdf = generar_pdf_etiquetas(items)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(paginas_pdf(pdf), 3)

    @override_settings(TORRE_MODO_IMPRESION="local")
    def test_modo_local_sin_impresora_avisa_y_el_conteo_queda_terminado(self):
        r = self.terminar()
        self.assertContains(r, "las etiquetas no salieron")
        self.assertContains(r, "No hay impresora configurada")
        self.assertEqual(TrabajoImpresion.objects.count(), 0)
        self.assertEqual(EscaneoRecepcion.objects.filter(orden=self.orden, impresa_ts__isnull=False).count(), 0)
        self.orden.refresh_from_db()
        self.assertIsNotNone(self.orden.ts_conteo_fin)
