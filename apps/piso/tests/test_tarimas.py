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
