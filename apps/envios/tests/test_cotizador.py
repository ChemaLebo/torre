"""Tests del motor de cotización y división (modo mock: tabla real medida).

Casos del contrato CONVENTIONS-ENVIOS.md, con los precios reales de agosto 2026:
puntopost 4kg=$86/8kg=$91 (≤10 kg, cobertura parcial), estafeta plana nacional.
"""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU
from apps.envios import cotizador
from apps.envios.models import CotizacionCache, Paquete
from apps.pedidos.models import LineaPedido

from .base import crear_cliente, crear_pedido, crear_tienda

SIX_GR = 4000       # six pack ≈ 4 kg
CAJA12_GR = 8000    # caja de 12 ≈ 8 kg

# Pool pinneado: esta suite ejercita la economía de división CON puntopost,
# independiente de qué carriers estén comercialmente activos en settings.
TORRE_POOL_LEGADO = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["puntopost", "estafeta", "paquetexpress", "fedex"],
}


@override_settings(TORRE=TORRE_POOL_LEGADO)
class BaseCotizador(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.six = SKU.objects.create(
            cliente=self.cliente, codigo="SIX", descripcion="Six Colimita",
            peso_gr=SIX_GR, precio_declarado=Decimal("300"),
        )
        self.caja12 = SKU.objects.create(
            cliente=self.cliente, codigo="C12", descripcion="Caja 12 Páramo",
            peso_gr=CAJA12_GR, precio_declarado=Decimal("600"),
        )

    def pedido_con(self, cp, lineas):
        pedido = crear_pedido(self.cliente, self.tienda, cp=cp, es_local=False)
        for sku, cantidad in lineas:
            LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=cantidad)
        return pedido


class TestCotizarLane(BaseCotizador):
    def test_mock_respeta_cobertura_y_tope_puntopost(self):
        opciones = {f["carrier"]: f for f in cotizador.cotizar_lane("06600", 8)}
        self.assertTrue(opciones["puntopost"]["ok"])
        self.assertEqual(opciones["puntopost"]["precio"], Decimal("91"))
        # Mérida: sin puntopost
        merida = {f["carrier"]: f for f in cotizador.cotizar_lane("97000", 8)}
        self.assertFalse(merida["puntopost"]["ok"])
        self.assertTrue(merida["estafeta"]["ok"])
        # 12 kg: puntopost no cotiza en ningún lado
        pesado = {f["carrier"]: f for f in cotizador.cotizar_lane("06600", 12)}
        self.assertFalse(pesado["puntopost"]["ok"])

    def test_cache_evita_segunda_consulta(self):
        cotizador.cotizar_lane("44100", 8)
        antes = CotizacionCache.objects.count()
        with patch.object(cotizador, "_cotizar_mock") as mock_tabla:
            cotizador.cotizar_lane("44100", 8)
            mock_tabla.assert_not_called()
        self.assertEqual(CotizacionCache.objects.count(), antes)

    def test_mejor_opcion_elige_la_mas_barata(self):
        mejor = cotizador.mejor_opcion("06600", 8)
        self.assertEqual(mejor["carrier"], "puntopost")
        mejor_merida = cotizador.mejor_opcion("97000", 8)
        self.assertEqual(mejor_merida["carrier"], "estafeta")


class TestPlanificarEnvio(BaseCotizador):
    def test_16kg_se_divide_en_dos_puntopost(self):
        # 2 cajas de 12 (16 kg) a CDMX: 2×8.4 kg puntopost ($182) < 16.8 kg estafeta (~$230)
        pedido = self.pedido_con("06600", [(self.caja12, 2)])
        paquetes = cotizador.planificar_envio(pedido)
        self.assertEqual(len(paquetes), 2)
        self.assertTrue(all(p.carrier == "puntopost" for p in paquetes))
        self.assertTrue(all(p.peso_kg <= 9 for p in paquetes))
        self.assertGreater(paquetes[0].ahorro_plan_mxn, 0)
        self.assertFalse(any(p.fuera_de_meta for p in paquetes))

    def test_12kg_se_divide_en_8_mas_4(self):
        pedido = self.pedido_con("44100", [(self.caja12, 1), (self.six, 1)])
        paquetes = cotizador.planificar_envio(pedido)
        self.assertEqual(len(paquetes), 2)
        pesos = sorted(float(p.peso_kg) for p in paquetes)
        self.assertLess(pesos[0], 6)
        total = sum(p.precio_cotizado for p in paquetes)
        # 91 + ~86.6 (interpolación por el +5% de empaque) — muy por debajo
        # de los $201 de mandarlo entero por estafeta.
        self.assertLess(total, Decimal("185"))
        self.assertTrue(all(p.carrier == "puntopost" for p in paquetes))

    def test_merida_sin_puntopost_va_estafeta_fuera_de_meta(self):
        pedido = self.pedido_con("97000", [(self.six, 1)])
        paquetes = cotizador.planificar_envio(pedido)
        self.assertEqual(len(paquetes), 1)
        self.assertEqual(paquetes[0].carrier, "estafeta")
        self.assertTrue(paquetes[0].fuera_de_meta)  # $149 > $115

    def test_nunca_excede_20kg_y_divide_pedidos_grandes(self):
        # 4 cajas de 12 = 32 kg brutos: obligatorio dividir aunque no ahorre
        pedido = self.pedido_con("97000", [(self.caja12, 4)])
        paquetes = cotizador.planificar_envio(pedido)
        self.assertGreaterEqual(len(paquetes), 2)
        for p in paquetes:
            self.assertLessEqual(float(p.peso_kg), 20.0)

    def test_lineas_completas_en_los_paquetes(self):
        pedido = self.pedido_con("06600", [(self.caja12, 2), (self.six, 3)])
        paquetes = cotizador.planificar_envio(pedido)
        unidades = {self.caja12.pk: 0, self.six.pk: 0}
        for p in paquetes:
            for pl in p.lineas.all():
                unidades[pl.linea_pedido.sku_id] += pl.cantidad
        self.assertEqual(unidades[self.caja12.pk], 2)
        self.assertEqual(unidades[self.six.pk], 3)

    def test_idempotente_sin_force(self):
        pedido = self.pedido_con("06600", [(self.caja12, 2)])
        primera = cotizador.planificar_envio(pedido)
        segunda = cotizador.planificar_envio(pedido)
        self.assertEqual([p.pk for p in primera], [p.pk for p in segunda])

    @override_settings(TORRE={**settings.TORRE, "FLOTA_PROPIA": True})
    def test_local_con_flota_tarifa_flat_100(self):
        # El atajo local flat solo existe con flota propia (TORRE["FLOTA_PROPIA"]=True).
        pedido = crear_pedido(self.cliente, self.tienda, cp="01780", es_local=True)
        LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=2)
        paquetes = cotizador.planificar_envio(pedido)
        self.assertEqual(len(paquetes), 1)
        self.assertEqual(paquetes[0].carrier, "local")
        self.assertEqual(paquetes[0].precio_cotizado, Decimal("100"))
        self.assertFalse(paquetes[0].fuera_de_meta)  # $100 ≤ meta $115

    @override_settings(TORRE={**settings.TORRE, "FLOTA_PROPIA": True})
    def test_local_con_flota_pesado_se_divide_a_100_por_paquete(self):
        # 3 cajas de 12 (25.2 kg con margen): 2 paquetes locales de $100
        pedido = crear_pedido(self.cliente, self.tienda, cp="06700", es_local=True)
        LineaPedido.objects.create(pedido=pedido, sku=self.caja12, cantidad=3)
        paquetes = cotizador.planificar_envio(pedido)
        self.assertEqual(len(paquetes), 2)
        for p in paquetes:
            self.assertLessEqual(float(p.peso_kg), 20.0)
            self.assertEqual(p.precio_cotizado, Decimal("100"))

    def test_local_sin_flota_cotiza_carriers_reales(self):
        # Default TORRE["FLOTA_PROPIA"]=False: el es_local se planifica como
        # cualquier pedido — carrier real cotizado, nunca "local".
        pedido = crear_pedido(self.cliente, self.tienda, cp="01780", es_local=True)
        LineaPedido.objects.create(pedido=pedido, sku=self.six, cantidad=2)
        paquetes = cotizador.planificar_envio(pedido)
        self.assertGreaterEqual(len(paquetes), 1)
        for p in paquetes:
            self.assertNotEqual(p.carrier, "local")


class TestGuiasPorPaquete(BaseCotizador):
    def test_generar_guias_una_por_paquete(self):
        from apps.envios.services import generar_guias

        pedido = self.pedido_con("06600", [(self.caja12, 2)])
        cotizador.planificar_envio(pedido)
        guias = generar_guias(pedido)
        self.assertEqual(len(guias), 2)
        self.assertEqual(len({g.numero for g in guias}), 2)
        for guia in guias:
            self.assertIsNotNone(guia.paquete)
        # idempotencia: segunda llamada regresa las mismas
        de_nuevo = generar_guias(pedido)
        self.assertEqual({g.pk for g in guias}, {g.pk for g in de_nuevo})


class TestReempaque(BaseCotizador):
    def test_caja24_divisible_se_reempaca_en_dos_medias(self):
        caja24 = SKU.objects.create(
            cliente=self.cliente, codigo="C24", descripcion="Caja 24 Colimita",
            peso_gr=11400, precio_declarado=Decimal("640"), empaques_divisibles=2,
        )
        pedido = self.pedido_con("06600", [(caja24, 1)])
        paquetes = cotizador.planificar_envio(pedido)
        # 11.4 kg indivisible costaría ~$201 estafeta; reempacada: 2×5.7 kg puntopost ≈ $177
        self.assertEqual(len(paquetes), 2)
        self.assertTrue(all(p.carrier == "puntopost" for p in paquetes))
        for p in paquetes:
            linea = p.lineas.get()
            self.assertEqual(linea.fraccion_de, 2)
            self.assertEqual(linea.cantidad, 1)
            self.assertIn("REEMPACADA", linea.texto_para_piso)

    def test_caja24_indivisible_no_se_parte(self):
        caja24 = SKU.objects.create(
            cliente=self.cliente, codigo="C24-I", descripcion="Caja 24 edición especial",
            peso_gr=11400, precio_declarado=Decimal("900"), empaques_divisibles=1,
        )
        pedido = self.pedido_con("06600", [(caja24, 1)])
        paquetes = cotizador.planificar_envio(pedido)
        self.assertEqual(len(paquetes), 1)
