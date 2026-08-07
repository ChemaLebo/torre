"""Flujo punta a punta: ASN → recibir → ubicar → reservar → pick → despachar → retorno."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.inventario import services
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo

from .base import InventarioTestCase


class RecepcionTests(InventarioTestCase):
    def _orden_con_linea(self, anunciada=24):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=anunciada)
        return orden, linea

    def test_folios_consecutivos(self):
        orden1, _ = self._orden_con_linea()
        orden2, _ = self._orden_con_linea()
        self.assertEqual(orden1.folio, "ASN-0001")
        self.assertEqual(orden2.folio, "ASN-0002")

    def test_recibir_separa_bueno_y_danado(self):
        orden, linea = self._orden_con_linea(24)
        services.recibir(linea, 20, 4, actor="piso1")

        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 20)
        self.assertEqual(self.suma(Saldo.CUARENTENA), 4)
        # Lo recibido sin ubicar NO es vendible
        self.assertEqual(services.disponible(self.sku), 0)

        linea.refresh_from_db()
        self.assertEqual(linea.cantidad_recibida, 20)
        self.assertEqual(linea.cantidad_danada, 4)

        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.RECIBIDA)  # línea completa → auto
        self.assertIsNotNone(orden.ts_descarga_fin)

    def test_recibir_parcial_deja_orden_en_recepcion(self):
        orden, linea = self._orden_con_linea(24)
        services.recibir(linea, 10, 0, actor="piso1")
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.EN_RECEPCION)
        self.assertIsNone(orden.ts_descarga_fin)

    def test_transicion_invalida_truena(self):
        orden, _ = self._orden_con_linea()
        with self.assertRaisesMessage(ValueError, "Transición inválida"):
            orden.transicionar(OrdenEntrada.CERRADA)

    def test_reloj_sla_recepcion(self):
        orden, linea = self._orden_con_linea(24)
        services.recibir(linea, 24, 0, actor="piso1")
        orden.refresh_from_db()

        horas = settings.TORRE["SLA_RECEPCION_HORAS_CONTRACTUAL"]
        self.assertEqual(orden.sla_limite, orden.ts_descarga_fin + timedelta(hours=horas))
        self.assertFalse(orden.sla_vencido)

        # Simula que la descarga terminó hace más de las horas contractuales
        orden.ts_descarga_fin = timezone.now() - timedelta(hours=horas + 1)
        orden.save()
        self.assertTrue(orden.sla_vencido)

        # Al cerrar (todo vendible) el reloj se detiene en ts_vendible
        orden.transicionar(OrdenEntrada.CERRADA, actor="jefe")
        self.assertIsNotNone(orden.ts_vendible)

    def test_no_se_recibe_en_orden_cerrada(self):
        orden, linea = self._orden_con_linea(24)
        services.recibir(linea, 24, 0, actor="piso1")
        orden.refresh_from_db()
        orden.transicionar(OrdenEntrada.CERRADA)
        with self.assertRaisesMessage(ValueError, "cerrada"):
            services.recibir(linea, 1, 0, actor="piso1")


class UbicarTests(InventarioTestCase):
    def _recibido(self, cantidad=20):
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=cantidad)
        services.recibir(linea, cantidad, 0, actor="piso1")

    def test_ubicar_vuelve_vendible(self):
        self._recibido(20)
        services.ubicar(self.sku, 20, self.ubic_picking, None, actor="piso1")

        self.assertEqual(self.suma(Saldo.EN_PUTAWAY), 0)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 20)
        self.assertEqual(services.disponible(self.sku), 20)
        self.assertTrue(
            Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.PUTAWAY, delta=20).exists()
        )

    def test_ubicar_exige_lote_si_el_sku_lo_requiere(self):
        self.sku.requiere_lote = True
        self.sku.save()
        self._recibido(20)
        with self.assertRaisesMessage(ValueError, "requiere lote"):
            services.ubicar(self.sku, 20, self.ubic_picking, None, actor="piso1")

        lote = self.crear_lote("L-001")
        services.ubicar(self.sku, 20, self.ubic_picking, lote, actor="piso1")
        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual(saldo.lote, lote)

    def test_ubicar_mas_de_lo_recibido_truena(self):
        self._recibido(10)
        with self.assertRaises(ValueError):
            services.ubicar(self.sku, 11, self.ubic_picking, None, actor="piso1")

    def test_no_se_ubica_a_una_ubicacion_de_salida(self):
        from apps.catalogo.models import Ubicacion
        corral = Ubicacion.objects.create(codigo="SAL-PQX", tipo=Ubicacion.SALIDA)
        self._recibido(10)
        with self.assertRaises(ValueError):
            services.ubicar(self.sku, 10, corral, None, actor="piso1")


class PickYDespachoTests(InventarioTestCase):
    def test_flujo_completo_pick_despacho(self):
        self.poner_vendible(20)
        self.assertTrue(services.reservar(self.sku, 5, "PED-00001"))

        services.confirmar_pick(self.sku, 5, "PED-00001")
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 15)
        self.assertEqual(self.suma(Saldo.RESERVADO), 0)
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 5)
        # El disponible no cambia con el pick (ya estaba descontado por la reserva)
        self.assertEqual(services.disponible(self.sku), 15)

        services.despachar(self.sku, 5, "PED-00001")
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 0)
        salida = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.SALIDA)
        self.assertEqual(salida.delta, -5)
        self.assertEqual(salida.estado_destino, "despachado")

    def test_pick_sin_reserva_truena(self):
        self.poner_vendible(20)
        with self.assertRaisesMessage(ValueError, "reservadas"):
            services.confirmar_pick(self.sku, 5, "PED-00002")

    def test_despachar_sin_empaque_truena(self):
        self.poner_vendible(20)
        services.reservar(self.sku, 5, "PED-00003")
        with self.assertRaisesMessage(ValueError, "en empaque"):
            services.despachar(self.sku, 5, "PED-00003")

    def test_restock_de_empaque_regresa_a_vendible(self):
        self.poner_vendible(20)
        services.reservar(self.sku, 5, "PED-00004")
        services.confirmar_pick(self.sku, 5, "PED-00004")

        services.restock_empaque(self.sku, 5, "PED-00004", actor="jefe")
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 20)
        self.assertEqual(self.suma(Saldo.EN_EMPAQUE), 0)
        self.assertEqual(services.disponible(self.sku), 20)


class RetornoTests(InventarioTestCase):
    def test_retorno_entra_a_cuarentena(self):
        services.retornar(self.sku, 2, "PED-00005", actor="piso1")

        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA)
        self.assertEqual(saldo.cantidad, 2)
        self.assertEqual(saldo.ubicacion, self.ubic_retorno)
        # La cuarentena no es vendible
        self.assertEqual(services.disponible(self.sku), 0)

        mov = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.RETORNO)
        self.assertEqual(mov.delta, 2)
        self.assertEqual(mov.estado_destino, Saldo.CUARENTENA)


class ResumenTests(InventarioTestCase):
    def test_resumen_sku_cuadra(self):
        self.poner_vendible(10)
        services.reservar(self.sku, 3, "PED-00006")
        services.retornar(self.sku, 2, "PED-00007", actor="piso1")

        resumen = services.resumen_sku(self.sku)
        self.assertEqual(resumen["vendible"], 10)
        self.assertEqual(resumen["apartado"], 3)
        self.assertEqual(resumen["cuarentena"], 2)
        self.assertEqual(resumen["disponible"], 7)
        self.assertEqual(resumen["fisico"], 12)  # la reserva es capa, no suma físico
