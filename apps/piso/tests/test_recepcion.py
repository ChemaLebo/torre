"""Recepción en piso: recibir con dañadas + foto de llegada, ubicar y cerrar."""
from django.db.models import Sum
from django.urls import reverse

from apps.core.models import EvidenciaFoto
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo

from .base import PisoTestCase


def _suma(sku, estado):
    return Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"] or 0


class RecepcionPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=10,
        )
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])

    def test_recibir_con_danadas_y_foto_de_llegada(self):
        respuesta = self.client.post(self.url, {
            "accion": "recibir",
            "linea_id": self.linea.pk,
            "cantidad_ok": "8",
            "cantidad_danada": "2",
            "foto_llegada": self.foto("llegada.jpg"),
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        self.linea.refresh_from_db()
        self.orden.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 8)
        self.assertEqual(self.linea.cantidad_danada, 2)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 8)
        self.assertEqual(_suma(self.sku, Saldo.CUARENTENA), 2)
        # Todas las líneas quedaron completas → la orden avanza sola a RECIBIDA.
        self.assertEqual(self.orden.estado, OrdenEntrada.RECIBIDA)
        self.assertTrue(
            EvidenciaFoto.objects.filter(
                entidad="asn", entidad_id=self.orden.folio, tipo="llegada",
            ).exists()
        )

    def test_recibir_sin_cantidades_muestra_error(self):
        respuesta = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "0", "cantidad_danada": "0",
        }, follow=True)
        self.assertContains(respuesta, "Nada que recibir")
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)

    def test_ubicar_a_ubicacion_escaneada(self):
        from apps.inventario.services import recibir
        recibir(self.linea, 10, 0, self.operador)

        respuesta = self.client.post(self.url, {
            "accion": "ubicar",
            "sku_id": self.sku.pk,
            "cantidad": "10",
            "ubicacion": "a-01-1",  # escaneo tolerante a mayúsculas/minúsculas
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)
        self.assertEqual(_suma(self.sku, Saldo.UBICADO_VENDIBLE), 10)

    def test_ubicar_a_ubicacion_inexistente_da_error_claro(self):
        from apps.inventario.services import recibir
        recibir(self.linea, 10, 0, self.operador)

        respuesta = self.client.post(self.url, {
            "accion": "ubicar", "sku_id": self.sku.pk,
            "cantidad": "10", "ubicacion": "Z-99-9",
        }, follow=True)
        self.assertContains(respuesta, "No existe la ubicación")
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 10)

    def test_cerrar_exige_todo_ubicado(self):
        from apps.inventario.services import recibir, ubicar
        recibir(self.linea, 10, 0, self.operador)

        respuesta = self.client.post(self.url, {"accion": "cerrar"}, follow=True)
        self.orden.refresh_from_db()
        self.assertContains(respuesta, "sin ubicar")
        self.assertNotEqual(self.orden.estado, OrdenEntrada.CERRADA)

        ubicar(self.sku, 10, self.ubic_picking, None, self.operador)
        self.client.post(self.url, {"accion": "cerrar"}, follow=True)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertIsNotNone(self.orden.ts_vendible)


class RecepcionCiegaYFotoObligatoriaTests(PisoTestCase):
    """B4: el que cuenta no ve lo anunciado, y la primera línea exige foto."""

    def setUp(self):
        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=3)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=8887,
        )
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])

    def test_captura_no_muestra_cantidad_anunciada(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "8887")       # la cifra anunciada no aparece
        self.assertNotContains(respuesta, "Anunciado")  # ni la columna
        self.assertContains(respuesta, "COLIMITA-SIX")  # el SKU sí: es lo que se cuenta

    def test_linea_completa_muestra_pill_sin_cifras(self):
        from apps.inventario.services import recibir
        self.linea.cantidad_anunciada = 10
        self.linea.save(update_fields=["cantidad_anunciada"])
        EvidenciaFoto.objects.create(
            entidad="asn", entidad_id=self.orden.folio, tipo="llegada",
            archivo=self.foto(), tomada_por="piso1",
        )
        recibir(self.linea, 10, 0, self.operador)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Completa")

    def test_primer_recibir_sin_foto_se_rechaza(self):
        respuesta = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "5", "cantidad_danada": "0",
        }, follow=True)
        self.assertContains(respuesta, "Tómale foto al camión/tarimas")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)

    def test_primer_recibir_con_foto_pasa_y_el_segundo_ya_no_la_exige(self):
        con_foto = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "5", "cantidad_danada": "0",
            "foto_llegada": self.foto("llegada.jpg"),
        }, follow=True)
        self.assertEqual(con_foto.status_code, 200)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 5)

        sin_foto = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "4", "cantidad_danada": "0",
        }, follow=True)
        self.assertNotContains(sin_foto, "Tómale foto")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 9)


class CerrarConTarimasTests(PisoTestCase):
    """B1: al cerrar se capturan las tarimas recibidas (default: las anunciadas)."""

    def setUp(self):
        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=3)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=10,
        )
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])

    def _recibir_y_ubicar_todo(self):
        from apps.inventario.services import recibir, ubicar
        recibir(self.linea, 10, 0, self.operador)
        ubicar(self.sku, 10, self.ubic_picking, None, self.operador)

    def test_cerrar_guarda_tarimas_recibidas(self):
        self._recibir_y_ubicar_todo()
        respuesta = self.client.post(self.url, {
            "accion": "cerrar", "tarimas_recibidas": "2",
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(self.orden.tarimas_recibidas, 2)

    def test_cerrar_sin_capturar_usa_las_anunciadas(self):
        self._recibir_y_ubicar_todo()
        self.client.post(self.url, {"accion": "cerrar"}, follow=True)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(self.orden.tarimas_recibidas, 3)

    def test_replay_de_cierre_sobre_cerrada_no_pisa_tarimas(self):
        # Botón atrás + reenviar POST con otro valor: la orden cerrada no debe
        # mutar el dato que factura finanzas ni duplicar transiciones.
        self._recibir_y_ubicar_todo()
        self.client.post(self.url, {"accion": "cerrar", "tarimas_recibidas": "16"}, follow=True)
        respuesta = self.client.post(
            self.url, {"accion": "cerrar", "tarimas_recibidas": "2"}, follow=True,
        )
        self.assertContains(respuesta, "ya está cerrada")
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(self.orden.tarimas_recibidas, 16)
