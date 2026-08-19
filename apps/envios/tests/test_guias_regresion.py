"""Regresiones de generar_guias y del plan de envío (revisión adversarial).

- Plan viejo con carrier "local" y FLOTA_PROPIA=False → la guía sale con el
  carrier real (jamás LOCAL-* atorada en un corral muerto).
- Commit POR PAQUETE: una guía ya comprada no se revierte porque otra caja
  falle; el reintento genera solo lo que falta.
- Carrier ÚNICO por plan: el cotizador jamás mezcla carriers en un pedido
  (el manifiesto se firma por corral = por carrier).
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios import cotizador, services
from apps.envios.adapters import ErrorCarrier, MockAdapter
from apps.envios.models import Guia, Paquete
from apps.pedidos.models import LineaPedido

from .base import crear_cliente, crear_pedido, crear_tienda


# Pool pinneado: la economía de estos planes depende de puntopost en el pool.
TORRE_POOL_LEGADO = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["puntopost", "estafeta", "paquetexpress", "fedex"],
}


@override_settings(ENVIA_API_KEY="", TORRE=TORRE_POOL_LEGADO)
class PlanViejoLocalSinFlotaTests(TestCase):
    """Alto #4: paquete.carrier='local' guardado cuando había flota propia."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(
            self.cliente, self.tienda, estado="EMPACADO", es_local=True, cp="01780",
        )
        self.paquete = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("4.20"),
            carrier="local", servicio="entrega_local",
            precio_cotizado=Decimal("100"),
        )

    def test_sin_flota_el_plan_local_viejo_se_resuelve_con_carrier_real(self):
        # TORRE["FLOTA_PROPIA"]=False (default): el carrier guardado se ignora
        # y elegir_carrier resuelve el real — nada de guías LOCAL-* que jamás
        # salen (POD escondido, poller las ignora).
        guias = services.generar_guias(self.pedido)
        self.assertEqual(len(guias), 1)
        guia = guias[0]
        self.assertNotEqual(guia.carrier, "local")
        self.assertFalse(guia.numero.startswith("LOCAL-"))
        self.assertTrue(guia.numero.startswith("MOCK-"))
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, "GUIA_GENERADA")

    @override_settings(TORRE={**settings.TORRE, "FLOTA_PROPIA": True})
    def test_con_flota_el_plan_local_sigue_siendo_local(self):
        guias = services.generar_guias(self.pedido)
        self.assertEqual(guias[0].carrier, "local")
        self.assertTrue(guias[0].numero.startswith("LOCAL-"))


@override_settings(ENVIA_API_KEY="", TORRE=TORRE_POOL_LEGADO)
class CommitPorPaqueteTests(TestCase):
    """Alto #10: la guía YA COMPRADA de la caja 1 jamás se revierte."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        self.caja1 = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("8.40"),
            carrier="estafeta", servicio="mock", precio_cotizado=Decimal("177"),
        )
        self.caja2 = Paquete.objects.create(
            pedido=self.pedido, numero=2, peso_kg=Decimal("12.60"),
            carrier="estafeta", servicio="mock", precio_cotizado=Decimal("201"),
        )

    def _adapter_que_falla_en_caja_2(self):
        real = MockAdapter()

        def generar(pedido, carrier, servicio, paquete=None):
            if paquete is not None and paquete.numero == 2:
                raise ErrorCarrier("El carrier rechazó la caja 2 (simulado).")
            return real.generar(pedido, carrier, servicio, paquete=paquete)

        return SimpleNamespace(generar=generar)

    def test_falla_de_una_caja_no_revierte_la_guia_comprada_ni_recompra(self):
        with patch(
            "apps.envios.services.get_adapter",
            return_value=self._adapter_que_falla_en_caja_2(),
        ):
            with self.assertRaises(ErrorCarrier):
                services.generar_guias(self.pedido)

        # La guía de la caja 1 quedó committeada — dinero ya gastado, intacto.
        guias = list(Guia.objects.filter(pedido=self.pedido))
        self.assertEqual(len(guias), 1)
        self.assertEqual(guias[0].paquete_id, self.caja1.pk)
        primera_pk = guias[0].pk
        # El pedido sigue EMPACADO: recuperable desde Salida.
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, "EMPACADO")
        # La falla quedó auditada aunque su atomic se haya revertido.
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk),
                accion="error_generacion_guia",
            ).exists()
        )

        # Reintento con el carrier sano: SOLO se genera la caja 2 (cero recompras).
        todas = services.generar_guias(self.pedido)
        self.assertEqual(len(todas), 2)
        self.assertIn(primera_pk, [g.pk for g in todas])
        self.assertEqual(Guia.objects.filter(pedido=self.pedido).count(), 2)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, "GUIA_GENERADA")


@override_settings(ENVIA_API_KEY="", TORRE=TORRE_POOL_LEGADO)
class CarrierUnicoPorPlanTests(TestCase):
    """Alto #5: un plan jamás mezcla carriers (manifiesto por corral)."""

    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        # CP 64xxx (Monterrey): puntopost SÍ cubre el lane pero solo ≤10 kg.
        self.caja8 = SKU.objects.create(
            cliente=self.cliente, codigo="C8", descripcion="Caja 8 kg",
            peso_gr=8000, precio_declarado=Decimal("600"),
        )
        self.caja12 = SKU.objects.create(
            cliente=self.cliente, codigo="C12G", descripcion="Caja 12 kg",
            peso_gr=12000, precio_declarado=Decimal("900"),
        )

    def test_costo_particion_solo_combina_bins_del_mismo_carrier(self):
        pedido = crear_pedido(self.cliente, self.tienda, cp="64000", es_local=False)
        l8 = LineaPedido.objects.create(pedido=pedido, sku=self.caja8, cantidad=1)
        l12 = LineaPedido.objects.create(pedido=pedido, sku=self.caja12, cantidad=1)
        bins = [
            [(l8, Decimal("8"), 1)],
            [(l12, Decimal("12"), 1)],
        ]
        total, opciones = cotizador._costo_particion("64000", bins)
        self.assertIsNotNone(total)
        carriers = {o["carrier"] for o in opciones}
        # Mezclado saldría más barato (puntopost $91 en el bin chico) pero
        # partiría el pedido entre corrales: UN carrier para todo el plan.
        self.assertEqual(len(carriers), 1)
        self.assertNotIn("puntopost", carriers)  # no cubre el bin de 12 kg

    def test_planificar_envio_produce_paquetes_del_mismo_carrier(self):
        pedido = crear_pedido(self.cliente, self.tienda, cp="64000", es_local=False)
        LineaPedido.objects.create(pedido=pedido, sku=self.caja8, cantidad=1)
        LineaPedido.objects.create(pedido=pedido, sku=self.caja12, cantidad=1)
        paquetes = cotizador.planificar_envio(pedido)
        self.assertGreaterEqual(len(paquetes), 2)  # 21 kg con margen: se divide
        carriers = {p.carrier for p in paquetes}
        self.assertEqual(len(carriers), 1, f"plan multi-carrier: {carriers}")