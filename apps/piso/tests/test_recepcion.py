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


class LotesSugeridosEnPutAwayTests(PisoTestCase):
    def test_datalist_trae_el_lote_anunciado_y_ubicar_completa_la_caducidad(self):
        from apps.catalogo.models import Lote
        from apps.inventario.models import LineaASN, OrdenEntrada
        from apps.inventario.services import recibir

        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(
            orden=orden, sku=self.sku, cantidad_anunciada=4, lote_codigo="L-ANUNCIADO", fecha_caducidad=None,
        )
        recibir(linea, 4, 0, self.operador)
        Lote.objects.create(sku=self.sku, codigo="L-ANUNCIADO")
        self.client.force_login(self.operador)
        respuesta = self.client.get(reverse("piso:recepcion_detalle", args=[orden.pk]))
        self.assertContains(respuesta, f'id="lotes-sku-{self.sku.pk}"')
        self.assertContains(respuesta, 'value="L-ANUNCIADO" data-caducidad="" data-origen="asn"')
        self.client.post(reverse("piso:recepcion_detalle", args=[orden.pk]), {
            "accion": "ubicar", "sku_id": self.sku.pk, "cantidad": "4", "ubicacion": "A-01-1",
            "lote": "L-ANUNCIADO", "fecha_caducidad": "2027-05-05",
        })
        lote = Lote.objects.get(sku=self.sku, codigo="L-ANUNCIADO")
        self.assertEqual(lote.fecha_caducidad.isoformat(), "2027-05-05")


class UbicacionPredictivaTests(PisoTestCase):
    """La ubicación destino se teclea con sugerencias (datalist de anaqueles
    activos de picking/reserva), ya no con un chip por anaquel."""

    def test_recepcion_lista_los_anaqueles_como_sugerencias(self):
        from django.urls import reverse

        from apps.catalogo.models import Ubicacion
        from apps.inventario.models import OrdenEntrada

        Ubicacion.objects.create(codigo="PIC-4-D-F-3", tipo=Ubicacion.PICKING)
        Ubicacion.objects.create(codigo="PIC-9-9-9", tipo=Ubicacion.PICKING, activo=False)
        self.login_piso()
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        respuesta = self.client.get(reverse("piso:recepcion_detalle", args=[orden.pk]))
        self.assertContains(respuesta, 'list="ubicaciones-destino"')
        self.assertContains(respuesta, '<option value="PIC-4-D-F-3">')
        self.assertNotContains(respuesta, 'PIC-9-9-9')
        self.assertNotContains(respuesta, 'chips-ubicacion')


class SugerenciaEnRecepcionTests(PisoTestCase):
    """La pantalla de ubicar trae el plan de acomodo por producto (data-sug-*)."""

    def test_option_trae_anaquel_y_cantidad_sugeridos(self):
        from django.urls import reverse

        from apps.catalogo.models import Ubicacion
        from apps.inventario.models import LineaASN, OrdenEntrada, Saldo
        from apps.inventario.services import recibir

        Ubicacion.objects.create(codigo="PIC-1-I-F-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=1)
        self.sku.largo_cm, self.sku.ancho_cm, self.sku.alto_cm = 36, 24, 17
        self.sku.save()
        self.login_piso()
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=40)
        recibir(linea, 40, 0, self.operador)
        self.assertEqual(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).count(), 1)
        respuesta = self.client.get(reverse("piso:recepcion_detalle", args=[orden.pk]))
        self.assertContains(respuesta, 'data-sug-ubicacion="PIC-1-I-F-2"')
        self.assertContains(respuesta, 'data-sug-cantidad="30"')
        self.assertContains(respuesta, "10 sin anaquel con espacio")
