"""Etiquetas de recepción 51×51 (Chema 2026-10-13): impresión por tandas en
orden inverso al escaneo (primero la tarima, luego las cajas), "Continuar
impresión" tras recargar rollo, "Reimprimir desde N", la etiqueta al momento
de las cajas que faltaron, y el PDF (caja, dañada, tarima, nombres largos)."""
from datetime import date

from django.test import override_settings
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.configuracion.models import TandaEtiquetas
from apps.core.models import EventoAuditoria
from apps.envios.models import TrabajoImpresion
from apps.inventario.models import EscaneoRecepcion, LineaASN, OrdenEntrada
from apps.inventario.services import confirmar_tarima, contar_caja, crear_tarima, escaneos_lifo, tanda_etiquetas
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

    def imprimir(self, desde=""):
        return self.client.post(self.detalle, {"accion": "imprimir", "tarima_id": self.tarima.pk, "desde": desde}, follow=True)

    def test_primera_tanda_lleva_la_tarima_y_las_cajas_en_orden_inverso(self):
        self.assertEqual([e.pk for e in escaneos_lifo(self.orden, self.tarima)], [e.pk for e in reversed(self.escaneos)])
        r = self.imprimir()
        self.assertContains(r, "TAR-01: etiquetas 1 a 5 de 5 van a la impresora (con la etiqueta de la tarima)")
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual((trabajo.tipo, trabajo.impresora, trabajo.media, trabajo.paginas, trabajo.cliente, trabajo.guia), ("producto", "etiquetas", "Custom.51x51mm", 6, self.cliente, None))
        self.assertEqual(trabajo.referencia, f"{self.orden.folio} TAR-01 etiquetas 1-5")
        self.assertEqual(trabajo.folio, trabajo.referencia)
        with trabajo.pdf.open("rb") as archivo:
            pdf = archivo.read()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(paginas_pdf(pdf), 6)
        self.assertEqual(EscaneoRecepcion.objects.filter(orden=self.orden, impresa_ts__isnull=False).count(), 5)
        evento = EventoAuditoria.objects.get(entidad="asn", entidad_id=self.orden.folio, accion="etiquetas_impresas")
        self.assertEqual((evento.delta["desde"], evento.delta["hasta"], evento.delta["con_tarima"], evento.delta["trabajo"]), (1, 5, True, trabajo.pk))
        r = self.imprimir()
        self.assertContains(r, "Ya salieron las 5 etiquetas de TAR-01")
        r = self.client.get(self.detalle)
        self.assertContains(r, "Etiquetas: 5 de 5 impresas")
        self.assertContains(r, "etiquetas impresas")

    def test_tandas_cortas_continuar_y_reimprimir_desde(self):
        TandaEtiquetas.objects.create(cajas=2)
        r = self.imprimir()
        self.assertContains(r, "etiquetas 1 a 2 de 5")
        self.assertContains(r, "pulsa Continuar impresión: faltan 3")
        self.assertEqual(TrabajoImpresion.objects.latest("pk").paginas, 3)  # tarima + 2 cajas
        r = self.client.get(self.detalle)
        self.assertContains(r, "Continuar impresión")
        self.assertContains(r, "sigue la 3")
        self.imprimir()
        self.assertEqual(TrabajoImpresion.objects.latest("pk").paginas, 2)  # sin tarima
        r = self.imprimir()
        self.assertContains(r, "etiquetas 5 a 5 de 5")
        self.assertEqual(TrabajoImpresion.objects.latest("pk").paginas, 1)
        r = self.imprimir(desde="4")
        self.assertContains(r, "etiquetas 4 a 5 de 5")
        self.assertEqual(TrabajoImpresion.objects.latest("pk").referencia, f"{self.orden.folio} TAR-01 etiquetas 4-5")
        self.assertEqual(TrabajoImpresion.objects.latest("pk").paginas, 2)
        r = self.imprimir(desde="1")
        self.assertEqual(TrabajoImpresion.objects.latest("pk").paginas, 3)  # vuelve a llevar la tarima
        r = self.imprimir(desde="9")
        self.assertContains(r, "hay 5 caja(s): pide desde 1 hasta 5")
        tanda = tanda_etiquetas(self.orden, self.tarima, desde=2, tamano=10)
        self.assertEqual((tanda["desde"], tanda["hasta"], tanda["incluye_tarima"], len(tanda["escaneos"])), (2, 5, False, 4))

    def test_faltaron_imprime_la_etiqueta_al_momento(self):
        self.imprimir()
        confirmar_tarima(self.orden, self.tarima, self.operador)
        confirmar_url = reverse("piso:recepcion_tarima_confirmar", args=[self.orden.pk, self.tarima.pk])
        self.client.post(confirmar_url, {"accion": "faltaron"})
        r = self.client.get(self.detalle)
        self.assertContains(r, "imprime su etiqueta al momento")
        antes = TrabajoImpresion.objects.count()
        r = self.client.post(reverse("piso:recepcion_lote", args=[self.orden.pk]), {"sku_id": self.sku.pk, "linea_id": self.linea.pk}, follow=True)
        self.assertContains(r, "Su etiqueta va a la impresora")
        self.assertEqual(TrabajoImpresion.objects.count(), antes + 1)
        trabajo = TrabajoImpresion.objects.latest("pk")
        nuevo = EscaneoRecepcion.objects.filter(orden=self.orden).latest("pk")
        self.assertEqual((trabajo.paginas, trabajo.referencia), (1, f"{self.orden.folio} TAR-01 caja {nuevo.pk}"))
        self.assertIsNotNone(nuevo.impresa_ts)
        # Cambiar de tarima apaga el modo.
        otra = crear_tarima(self.operador)
        self.client.post(reverse("piso:recepcion_tarima", args=[self.orden.pk]), {"accion": "elegir", "tarima_id": otra.pk})
        r = self.client.get(self.detalle)
        self.assertNotContains(r, "imprime su etiqueta al momento")

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
    def test_modo_local_sin_impresora_avisa_sin_marcar_impresas(self):
        with self.settings():
            r = self.imprimir()
        self.assertContains(r, "No hay impresora configurada")
        self.assertEqual(TrabajoImpresion.objects.count(), 0)
        self.assertEqual(EscaneoRecepcion.objects.filter(orden=self.orden, impresa_ts__isnull=False).count(), 0)
