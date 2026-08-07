"""Conteos: esperado del sistema, incidencia DES por umbral, y conteo cíclico."""
from decimal import Decimal
from unittest import mock

from django.core.management import call_command

from apps.catalogo.models import SKU
from apps.inventario import services
from apps.inventario.models import Movimiento, TareaConteo

from .base import InventarioTestCase


class RegistrarConteoTests(InventarioTestCase):
    def test_conteo_calcula_esperado_del_sistema(self):
        self.poner_vendible(10)
        conteo = services.registrar_conteo(self.sku, 10, "piso1")
        self.assertEqual(conteo.esperado, 10)
        self.assertEqual(conteo.contado, 10)
        self.assertEqual(conteo.diferencia, 0)
        self.assertTrue(conteo.folio.startswith("CON-"))

    def test_conteo_deja_rastro_en_kardex_sin_mover_stock(self):
        self.poner_vendible(10)
        conteo = services.registrar_conteo(self.sku, 8, "piso1")
        mov = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.CONTEO)
        self.assertEqual(mov.delta, 0)  # contar no mueve stock; el ajuste es aparte
        self.assertEqual(mov.referencia, conteo.folio)
        self.assertEqual(self.suma("ubicado_vendible"), 10)

    @mock.patch("apps.inventario.services._abrir_incidencia_descuadre")
    def test_descuadre_grande_abre_incidencia_des(self, abrir_mock):
        """|dif| > UMBRAL_DISCREPANCIA_UNIDADES (12) → incidencia DES."""
        self.poner_vendible(40)
        conteo = services.registrar_conteo(self.sku, 10, "piso1")  # dif = -30
        abrir_mock.assert_called_once_with(conteo)

    @mock.patch("apps.inventario.services._abrir_incidencia_descuadre")
    def test_descuadre_chico_no_abre_incidencia(self, abrir_mock):
        self.poner_vendible(10)
        services.registrar_conteo(self.sku, 9, "piso1")  # dif = -1, $180 < $500
        abrir_mock.assert_not_called()

    @mock.patch("apps.inventario.services._abrir_incidencia_descuadre")
    def test_descuadre_valioso_abre_incidencia_aunque_sean_pocas_unidades(self, abrir_mock):
        """1 unidad de $600 > UMBRAL_DISCREPANCIA_MXN (500) → DES igual."""
        self.sku.precio_declarado = Decimal("600.00")
        self.sku.save()
        self.poner_vendible(10)
        conteo = services.registrar_conteo(self.sku, 9, "piso1")
        abrir_mock.assert_called_once_with(conteo)

    @mock.patch("apps.inventario.services._abrir_incidencia_descuadre")
    def test_conteo_exacto_no_abre_incidencia(self, abrir_mock):
        self.poner_vendible(10)
        services.registrar_conteo(self.sku, 10, "piso1")
        abrir_mock.assert_not_called()

    def test_conteo_completa_la_tarea_del_dia(self):
        self.poner_vendible(10)
        tareas = services.generar_conteo_ciclico()
        self.assertEqual(len(tareas), 1)  # solo hay un SKU
        conteo = services.registrar_conteo(self.sku, 10, "piso1")
        tarea = TareaConteo.objects.get(pk=tareas[0].pk)
        self.assertEqual(tarea.estado, TareaConteo.COMPLETADA)
        self.assertEqual(tarea.conteo, conteo)


class ConteoCiclicoTests(InventarioTestCase):
    def _mas_skus(self, cuantos):
        return [
            SKU.objects.create(
                cliente=self.cliente, codigo=f"SKU-{i:02d}", descripcion=f"SKU {i}",
                requiere_lote=False,
            )
            for i in range(cuantos)
        ]

    def test_elige_tres_skus_y_es_idempotente(self):
        self._mas_skus(4)  # 5 SKUs en total
        tareas = services.generar_conteo_ciclico()
        self.assertEqual(len(tareas), 3)

        # Segunda corrida el mismo día: no duplica
        de_nuevo = services.generar_conteo_ciclico()
        self.assertEqual(len(de_nuevo), 3)
        self.assertEqual(TareaConteo.objects.count(), 3)

    def test_prioriza_el_sku_con_conteo_mas_viejo(self):
        """Los SKUs nunca contados van primero; el recién contado va al final."""
        extras = self._mas_skus(3)  # 4 SKUs: self.sku + 3
        services.registrar_conteo(self.sku, 0, "piso1")  # self.sku queda "fresco"

        tareas = services.generar_conteo_ciclico()
        skus_elegidos = {t.sku_id for t in tareas}
        self.assertEqual(skus_elegidos, {s.pk for s in extras})
        self.assertNotIn(self.sku.pk, skus_elegidos)

    def test_command_conteo_ciclico(self):
        self._mas_skus(4)
        call_command("conteo_ciclico")
        self.assertEqual(TareaConteo.objects.count(), 3)
        call_command("conteo_ciclico")  # idempotente
        self.assertEqual(TareaConteo.objects.count(), 3)
