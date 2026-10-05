"""Tarimas al ubicar en recepción (Chema 2026-09-24): el piso abre TAR-nn
desde Ubicar, mete piezas ahí (vendibles) y en la siguiente pieza puede usar
la misma tarima o abrir otra. Y el plan de acomodo nombra la zona de
reservas para lo que no cabe, en vez de cuarentena."""
from django.urls import reverse

from apps.catalogo.models import Ubicacion
from apps.core.models import EventoAuditoria
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo

from .base import PisoTestCase


class TarimasEnUbicarTests(PisoTestCase):
    def setUp(self):
        from apps.inventario.services import recibir

        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)
        self.linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=10)
        recibir(self.linea, 10, 0, self.operador)
        self.url_ubicar = reverse("piso:recepcion_ubicar", args=[self.orden.pk])

    def _ubicar(self, ubicacion, cantidad):
        return self.client.post(
            self.url_ubicar,
            {"accion": "ubicar", "sku_id": self.sku.pk, "ubicacion": ubicacion, "cantidad": str(cantidad)},
            follow=True,
        )

    def test_crear_tarima_ubicar_en_ella_y_abrir_otra(self):
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk})
        self.assertContains(respuesta, "Nueva tarima")
        self.assertContains(respuesta, "Todavía no hay tarimas abiertas")
        respuesta = self.client.post(self.url_ubicar, {"accion": "nueva_tarima", "sku_id": self.sku.pk})
        tarima = Ubicacion.objects.get(codigo="TAR-01")
        self.assertEqual((tarima.tipo, tarima.activo, tarima.prioridad), (Ubicacion.RESERVA, True, None))
        self.assertRedirects(respuesta, f"{self.url_ubicar}?sku={self.sku.pk}&ubicacion=TAR-01", fetch_redirect_response=False)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="ubicacion", entidad_id="TAR-01", accion="tarima_creada").exists())
        # Recién creada queda elegida en el campo de ubicación.
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk, "ubicacion": "TAR-01"})
        self.assertContains(respuesta, 'value="TAR-01" placeholder="PIC-1-I-F-1"')
        respuesta = self._ubicar("TAR-01", 4)
        self.assertContains(respuesta, "4 piezas en TAR-01")
        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((saldo.ubicacion.codigo, saldo.cantidad), ("TAR-01", 4))
        # La siguiente pieza ve la tarima con su contenido (última usada) y puede abrir otra.
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk})
        self.assertContains(respuesta, 'data-tarima="TAR-01"')
        self.assertContains(respuesta, "TAR-01 · 4 pzas · última usada")
        self.assertContains(respuesta, "COLIMITA-SIX ×4")
        self.client.post(self.url_ubicar, {"accion": "nueva_tarima", "sku_id": self.sku.pk})
        self.assertTrue(Ubicacion.objects.filter(codigo="TAR-02", tipo=Ubicacion.RESERVA).exists())
        respuesta = self._ubicar("TAR-02", 6)
        self.assertContains(respuesta, "6 piezas en TAR-02")
        self.assertEqual(
            sorted(Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).values_list("ubicacion__codigo", "cantidad")),
            [("TAR-01", 4), ("TAR-02", 6)],
        )

    def test_el_plan_manda_los_sobrantes_a_la_zona_de_reservas(self):
        """Sin anaquel con espacio (aquí: SKU sin medidas), el paso del plan
        apunta a la zona de desborde y lo dice; sin zona, cuarentena como antes."""
        from apps.inventario.services import planear_acomodo

        plan = planear_acomodo(self.orden, self.operador)
        self.assertEqual([(p["ubicacion"], p["cantidad"]) for p in plan["pasos"]], [(None, 10)])
        self.assertIn("a cuarentena", plan["pasos"][0]["motivo"])
        Ubicacion.objects.create(codigo="RES-CUAR", tipo=Ubicacion.RESERVA)
        plan = planear_acomodo(self.orden, self.operador)
        self.assertEqual([(p["ubicacion"], p["cantidad"]) for p in plan["pasos"]], [("RES-CUAR", 10)])
        self.assertIn("a RES-CUAR (reservas, vendible)", plan["pasos"][0]["motivo"])
        # Ubicar sigue el paso: RES-CUAR sugerido y vendible al confirmar.
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk})
        self.assertContains(respuesta, 'id="anaquel-sugerido">RES-CUAR')
        respuesta = self._ubicar("RES-CUAR", 10)
        self.assertContains(respuesta, "10 piezas en RES-CUAR")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).ubicacion.codigo, "RES-CUAR")
        self.assertEqual(OrdenEntrada.objects.get(pk=self.orden.pk).plan_acomodo["pasos"][0]["ubicadas"], 10)


class ReplaneoPorTarimaTests(PisoTestCase):
    """Chema 2026-10-05: lo que se queda en tarima libera el anaquel que el
    plan le tenía apartado; se rehace el plan de lo que falta de la orden."""

    def setUp(self):
        from apps.catalogo.models import SKU
        from apps.inventario.services import crear_tarima, planear_acomodo, recibir

        self.login_piso()
        # Un anaquel donde caben exactamente 2 piezas de 20×20×10 paradas.
        self.anaquel = Ubicacion.objects.create(codigo="PIC-1-A", tipo=Ubicacion.PICKING, prioridad=1, largo_cm=40, ancho_cm=20, alto_cm=10)
        self.sku_a = SKU.objects.create(cliente=self.cliente, codigo="A-SIX", descripcion="A", peso_gr=1000, largo_cm=20, ancho_cm=20, alto_cm=10, requiere_lote=False)
        self.sku_b = SKU.objects.create(cliente=self.cliente, codigo="B-SIX", descripcion="B", peso_gr=1000, largo_cm=20, ancho_cm=20, alto_cm=10, requiere_lote=False)
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)
        self.linea_a = LineaASN.objects.create(orden=self.orden, sku=self.sku_a, cantidad_anunciada=2)
        self.linea_b = LineaASN.objects.create(orden=self.orden, sku=self.sku_b, cantidad_anunciada=2)
        recibir(self.linea_a, 2, 0, self.operador)
        recibir(self.linea_b, 2, 0, self.operador)
        self.tarima = crear_tarima(self.operador)
        self.plan = planear_acomodo(self.orden, self.operador)
        self.url_ubicar = reverse("piso:recepcion_ubicar", args=[self.orden.pk])

    def _pasos(self):
        return [(p["sku"], p["ubicacion"], p["cantidad"], p["ubicadas"]) for p in OrdenEntrada.objects.get(pk=self.orden.pk).plan_acomodo["pasos"]]

    def test_dejar_en_tarima_replanea_y_libera_el_anaquel(self):
        # El plan original: A toma el anaquel y B se queda sin lugar.
        self.assertEqual(self._pasos(), [("A-SIX", "PIC-1-A", 2, 0), ("B-SIX", None, 2, 0)])
        respuesta = self.client.post(
            self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku_a.pk, "ubicacion": self.tarima.codigo, "cantidad": "2"}, follow=True,
        )
        self.assertContains(respuesta, f"2 piezas en {self.tarima.codigo}")
        # Replaneado: A ya no tiene pasos (quedó en tarima) y B hereda el anaquel.
        self.assertEqual(self._pasos(), [("B-SIX", "PIC-1-A", 2, 0)])
        evento = EventoAuditoria.objects.get(entidad="asn", entidad_id=self.orden.folio, accion="acomodo_replaneado_por_tarima")
        self.assertEqual((evento.delta["sku"], evento.delta["tarima"], evento.delta["cantidad"], evento.delta["pendientes_antes"]), ("A-SIX", "TAR-01", 2, 2))  # pendientes al replanear: las 2 de B
        # Ubicar B en el anaquel sigue el plan nuevo y no vuelve a replanear.
        self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku_b.pk, "ubicacion": "PIC-1-A", "cantidad": "2"}, follow=True)
        self.assertEqual(self._pasos(), [("B-SIX", "PIC-1-A", 2, 2)])
        self.assertEqual(EventoAuditoria.objects.filter(accion="acomodo_replaneado_por_tarima").count(), 1)

    def test_ubicar_en_anaquel_no_replanea(self):
        generado = self.plan["generado"]
        self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku_a.pk, "ubicacion": "PIC-1-A", "cantidad": "1"}, follow=True)
        self.assertEqual(OrdenEntrada.objects.get(pk=self.orden.pk).plan_acomodo["generado"], generado)
        self.assertFalse(EventoAuditoria.objects.filter(accion="acomodo_replaneado_por_tarima").exists())

    def test_sin_pasos_pendientes_no_replanea(self):
        # A completo en su anaquel y B, lo último pendiente, a tarima: ya no
        # queda nada por planear, así que no se rehace el plan.
        self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku_a.pk, "ubicacion": "PIC-1-A", "cantidad": "2"}, follow=True)
        self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku_b.pk, "ubicacion": self.tarima.codigo, "cantidad": "2"}, follow=True)
        self.assertEqual(self._pasos(), [("A-SIX", "PIC-1-A", 2, 2), ("B-SIX", None, 2, 2)])
        self.assertFalse(EventoAuditoria.objects.filter(accion="acomodo_replaneado_por_tarima").exists())
