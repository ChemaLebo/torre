"""Conteos cíclicos en piso: conteo ciego, discrepancia y folio DES si excede umbral."""
from django.urls import reverse

from apps.incidencias.models import Incidencia
from apps.inventario.models import Conteo, TareaConteo

from .base import PisoTestCase


class ConteosPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)  # esperado del sistema: 50 vendibles
        self.tarea = TareaConteo.objects.create(sku=self.sku)
        self.url = reverse("piso:conteos")

    def test_conteo_que_cuadra_completa_la_tarea(self):
        respuesta = self.client.post(self.url, {
            "tarea_id": self.tarea.pk, "contado": "50",
        }, follow=True)
        self.assertContains(respuesta, "cuadra")

        self.tarea.refresh_from_db()
        self.assertEqual(self.tarea.estado, TareaConteo.COMPLETADA)
        conteo = Conteo.objects.get(sku=self.sku)
        self.assertEqual(conteo.esperado, 50)
        self.assertEqual(conteo.contado, 50)
        self.assertEqual(conteo.diferencia, 0)
        self.assertFalse(Incidencia.objects.filter(sku=self.sku, tipo="DES").exists())

    def test_descuadre_grande_abre_folio_des_y_lo_muestra(self):
        # Diferencia de -40: excede el umbral de unidades → incidencia DES.
        respuesta = self.client.post(self.url, {
            "tarea_id": self.tarea.pk, "contado": "10",
        }, follow=True)

        conteo = Conteo.objects.get(sku=self.sku)
        self.assertEqual(conteo.diferencia, -40)
        incidencia = Incidencia.objects.get(sku=self.sku, tipo="DES")
        self.assertContains(respuesta, incidencia.folio)
        self.assertContains(respuesta, "doble firma")

    def test_tarea_ya_contada_no_se_repite(self):
        self.client.post(self.url, {"tarea_id": self.tarea.pk, "contado": "50"})
        respuesta = self.client.post(self.url, {
            "tarea_id": self.tarea.pk, "contado": "49",
        }, follow=True)
        self.assertContains(respuesta, "ya se contó hoy")
        self.assertEqual(Conteo.objects.filter(sku=self.sku).count(), 1)

    def test_contado_invalido_da_error_claro(self):
        respuesta = self.client.post(self.url, {
            "tarea_id": self.tarea.pk, "contado": "muchas",
        }, follow=True)
        self.assertContains(respuesta, "número entero")
        self.tarea.refresh_from_db()
        self.assertEqual(self.tarea.estado, TareaConteo.PENDIENTE)

    def test_card_muestra_donde_contar_sin_cantidades(self):
        # C2: la card dice DÓNDE está el SKU (ubicaciones con saldo), pero el
        # conteo sigue CIEGO: jamás muestra el esperado del sistema.
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Dónde contar")
        self.assertContains(respuesta, "A-01-1")
        tarea = respuesta.context["pendientes"][0]
        self.assertEqual(list(tarea.ubicaciones), ["A-01-1"])
        self.assertNotContains(respuesta, "Esperado")  # ciego: sin cifras del sistema
