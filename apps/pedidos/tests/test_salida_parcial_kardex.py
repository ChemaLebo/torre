"""Fix del manifiesto por caja (Chema 2026-10-21; PED-00281, PED-00507): con
varias cajas enteras del mismo producto, del kardex sale solo lo que viaja en
las cajas que suben al manifiesto; lo de la caja que se queda sale cuando
salga. Una media caja que se queda no retiene la unidad: sale entera con la
primera mitad, como siempre. Y el seed local siembra ese escenario."""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.envios.adapters import MockAdapter
from apps.envios.models import Paquete, PaqueteLinea
from apps.inventario.models import Movimiento, Saldo
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import FIRMA_PNG, PisoTestCase


def suma(sku, estado):
    return sum(Saldo.objects.filter(sku=sku, estado=estado).values_list("cantidad", flat=True))


# Pool pinneado como en apps.piso.tests.test_salida: puntopost gana el lane y su corral es SAL-OTRO.
TORRE_POOL_LEGADO = {**settings.TORRE, "CARRIERS_COTIZAR": ["puntopost", "estafeta", "paquetexpress", "fedex"]}


@override_settings(ENVIA_API_KEY="", TORRE=TORRE_POOL_LEGADO)
class ManifiestoPorCajaRestaSoloLoQueSaleTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        MockAdapter.reiniciar()
        self.login_piso()
        self.crear_stock(cantidad=20)
        self.url = reverse("piso:salida")

    def _cajas(self, pedido, linea, reparto):
        cajas = []
        for numero, (cantidad, fraccion) in enumerate(reparto, start=1):
            caja = Paquete.objects.create(pedido=pedido, numero=numero, peso_kg=Decimal("4"), carrier="puntopost", estado=Paquete.EMPACADO)
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=cantidad, fraccion_de=fraccion)
            cajas.append(caja)
        services.generar_guia(pedido)
        ahora = timezone.now()
        Paquete.objects.filter(pedido=pedido).update(ts_etiquetas=ahora, ts_cierre=ahora, ts_listo_salida=ahora)
        pedido.refresh_from_db()
        return [Paquete.objects.get(pk=c.pk) for c in cajas]

    def _manifestar(self, pedido, cajas):
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {
                "accion": "manifiesto", "firma_png": FIRMA_PNG, "chofer": "Chofer Prueba", "corral": "SAL-OTRO", "carrier": "puntopost",
                "paquete_id": [c.pk for c in cajas],
            }, follow=True)
        pedido.refresh_from_db()

    def test_tres_cajas_del_mismo_producto_salen_de_dos_en_dos(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=3))
        linea = pedido.lineas.get()
        c1, c2, c3 = self._cajas(pedido, linea, [(1, 1), (1, 1), (1, 1)])
        self.assertEqual(suma(self.sku, Saldo.EN_EMPAQUE), 3)
        self._manifestar(pedido, [c1, c2])
        linea.refresh_from_db()
        # Antes: salida -3 y despachada 3 aunque la caja 3 siguiera en bodega.
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), linea.cantidad_despachada, pedido.estado), (1, 2, Pedido.PARCIALMENTE_DESPACHADO))
        self.assertEqual(Movimiento.objects.get(sku=self.sku, tipo=Movimiento.SALIDA, referencia=pedido.folio).delta, -2)
        self._manifestar(pedido, [c3])
        linea.refresh_from_db()
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), linea.cantidad_despachada, pedido.estado), (0, 3, Pedido.RECOLECTADO))
        self.assertEqual(sorted(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.SALIDA, referencia=pedido.folio).values_list("delta", flat=True)), [-2, -1])

    def test_media_caja_sigue_saliendo_entera_con_la_primera_mitad(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        linea = pedido.lineas.get()
        c1, c2 = self._cajas(pedido, linea, [(1, 2), (1, 2)])
        self._manifestar(pedido, [c1])
        linea.refresh_from_db()
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), linea.cantidad_despachada, pedido.estado), (0, 1, Pedido.PARCIALMENTE_DESPACHADO))
        self._manifestar(pedido, [c2])
        linea.refresh_from_db()
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), linea.cantidad_despachada, pedido.estado), (0, 1, Pedido.RECOLECTADO))
        self.assertEqual(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.SALIDA, referencia=pedido.folio).count(), 1)


@override_settings(ENVIA_API_KEY="", DEBUG=True)
class SeedEscenarioCajasTests(PisoTestCase):
    def test_siembra_los_dos_pedidos_listos_en_salida(self):
        call_command("seed_escenario_cajas", verbosity=0)
        a = Pedido.objects.get(folio="SEED-CAJAS-A")
        b = Pedido.objects.get(folio="SEED-CAJAS-B")
        c = Pedido.objects.get(folio="SEED-CAJAS-C")
        d = Pedido.objects.get(folio="SEED-CAJAS-D")
        self.assertEqual((a.estado, a.paquetes.count(), b.paquetes.count()), (Pedido.GUIA_GENERADA, 3, 2))
        self.assertEqual(a.lineas.get().picks.filter(ubicacion__codigo="SEED-A3", lote__codigo="SEEDLOTE").count(), 1)
        sku = a.lineas.get().sku
        self.assertEqual((suma(sku, Saldo.EN_EMPAQUE), suma(sku, Saldo.UBICADO_VENDIBLE), suma(sku, Saldo.RESERVADO)), (4, 16, 3))
        # C y D: en picking, libres y al frente de la cola (C primero).
        self.assertEqual((c.estado, c.asignado_a, d.estado, d.asignado_a), (Pedido.EN_PICKING, None, Pedido.EN_PICKING, None))
        self.assertLess(c.ts_picking, d.ts_picking)
        from apps.incidencias.models import Compensacion
        comp = Compensacion.objects.get(incidencia__pedido=d)
        self.assertEqual((comp.estado, d.lineas.get().cantidad_repuesta, d.paquetes.get(numero=2).lineas.get().repone_a.numero), ("APROBADA", 1, 1))
        self.login_piso()
        self.assertContains(self.client.get(reverse("piso:salida")), "SEED-CAJAS-A")
        home = self.client.post(reverse("piso:home"), {"accion": "siguiente"}, follow=True)
        self.assertRedirects(home, reverse("piso:picking_pedido", args=[c.pk]), fetch_redirect_response=False)
        # Se vuelve a sembrar aunque la prueba haya firmado un manifiesto y cancelado una guía (reembolso).
        from unittest.mock import patch

        from apps.envios.services import cancelar_guia, registrar_manifiesto
        cajas = list(a.paquetes.order_by("numero")[:2])
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(a, self.operador, paquetes=cajas, kardex_por_caja=True)
        registrar_manifiesto("estafeta", "SAL-OTRO", self.operador, [(a, cajas)], chofer="Chofer Seed")
        cancelar_guia(a.paquetes.get(numero=3).guia_activa, self.operador, motivo="prueba")
        call_command("seed_escenario_cajas", verbosity=0)  # idempotente
        self.assertEqual(Pedido.objects.filter(folio__startswith="SEED-CAJAS-").count(), 4)
        self.assertEqual(Pedido.objects.get(folio="SEED-CAJAS-A").paquetes.filter(estado="EMPACADO").count(), 3)
        with override_settings(DEBUG=False), self.assertRaises(Exception):
            call_command("seed_escenario_cajas", verbosity=0)
