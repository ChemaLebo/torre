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


class MarcaAnaquelEnConteoTests(PisoTestCase):
    """Al contar, el piso marca el anaquel lleno o con espacio (Ubicacion.lleno_manual)."""

    def test_marca_lleno_y_luego_con_espacio(self):
        from django.urls import reverse

        from apps.catalogo.models import Ubicacion
        from apps.inventario.models import TareaConteo

        self.login_piso()
        self.crear_stock(cantidad=10)  # deja saldo vendible en self.ubic_picking
        anaquel = Ubicacion.objects.get(pk=self.ubic_picking.pk)
        anaquel.largo_cm, anaquel.ancho_cm, anaquel.alto_cm, anaquel.prioridad = 180, 58, 52, 1
        anaquel.save()
        tarea = TareaConteo.objects.create(sku=self.sku)
        respuesta = self.client.get(reverse("piso:conteos"))
        self.assertContains(respuesta, f'value="{anaquel.codigo}|lleno"')
        respuesta = self.client.post(reverse("piso:conteos"), {
            "tarea_id": tarea.pk, "contado": "10", "anaquel_estado": f"{anaquel.codigo}|lleno",
        }, follow=True)
        self.assertContains(respuesta, f"{anaquel.codigo} marcado lleno")
        anaquel.refresh_from_db()
        self.assertTrue(anaquel.lleno_manual)
        # Otra tarea del día (la de un SKU es única por fecha): con otro SKU en el mismo anaquel.
        from decimal import Decimal

        from apps.catalogo.models import SKU

        otro = SKU.objects.create(cliente=self.cliente, codigo="OTRO", descripcion="Otro", peso_gr=100, precio_declarado=Decimal("1"), requiere_lote=False)
        self.crear_stock(sku=otro, cantidad=5)
        tarea2 = TareaConteo.objects.create(sku=otro)
        self.client.post(reverse("piso:conteos"), {"tarea_id": tarea2.pk, "contado": "5", "anaquel_estado": f"{anaquel.codigo}|espacio"})
        anaquel.refresh_from_db()
        self.assertFalse(anaquel.lleno_manual)
