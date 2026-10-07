"""Buscador de Bodega (Chema 2026-10-06): producto, lote o posición → dónde
está cada cosa, con los racks resaltados en el plano."""
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.catalogo.models import Lote, Ubicacion
from apps.core.models import PerfilUsuario
from apps.inventario.models import Saldo
from apps.inventario.services import buscar_en_bodega
from apps.piso.tests.base import PisoTestCase


class BuscadorBodegaTests(PisoTestCase):
    def setUp(self):
        self.crear_stock(cantidad=20)  # COLIMITA-SIX vendible en A-01-1
        self.rack = Ubicacion.objects.create(codigo="PIC-2-D-B-1", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.lote = Lote.objects.create(sku=self.sku, codigo="CEX27ABC")
        Saldo.objects.create(sku=self.sku, ubicacion=self.rack, lote=self.lote, estado=Saldo.UBICADO_VENDIBLE, cantidad=38)
        Saldo.objects.create(sku=self.sku, ubicacion=self.ubic_recepcion, lote=None, estado=Saldo.CUARENTENA, cantidad=5)
        mesa = get_user_model().objects.create_user("mesa-busca", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_servicio_por_producto_lote_y_posicion(self):
        r = buscar_en_bodega("colimita")
        self.assertEqual(r["modo"], "producto")
        self.assertEqual([(f["ubicacion"], f["lote"], f["estado"], f["cantidad"]) for f in r["filas"]],
                         [("A-01-1", "", "vendible", 20), ("PIC-2-D-B-1", "CEX27ABC", "vendible", 38), ("REC-01", "", "cuarentena", 5)])
        self.assertEqual(r["racks"], {"A-01-1", "PIC-2-D-B-1", "REC-01"})
        self.assertEqual([f["ubicacion"] for f in buscar_en_bodega("cex27abc")["filas"]], ["PIC-2-D-B-1"])
        self.assertEqual([f["cantidad"] for f in buscar_en_bodega("7501234567890")["filas"]], [20, 38, 5])
        r = buscar_en_bodega("pic-2-d-b-1")
        self.assertEqual((r["modo"], r["ubicacion"].codigo, [f["cantidad"] for f in r["filas"]]), ("posicion", "PIC-2-D-B-1", [38]))
        self.assertIsNone(buscar_en_bodega("  "))
        self.assertEqual(buscar_en_bodega("nada-de-nada")["filas"], [])

    def test_pantalla_resalta_los_racks_y_enlaza(self):
        respuesta = self.client.get(reverse("mesa:bodega"), {"q": "COLIMITA-SIX"})
        self.assertContains(respuesta, "Resultado de «COLIMITA-SIX»")
        self.assertContains(respuesta, "3 posiciones resaltadas")
        self.assertContains(respuesta, 'class="plano-rack ocup-libre resaltado"')  # PIC-2-D-B-1 en el plano
        self.assertContains(respuesta, "CEX27ABC")
        self.assertNotContains(respuesta, 'http-equiv="refresh"')  # no recarga mientras buscas
        respuesta = self.client.get(reverse("mesa:bodega"), {"q": "PIC-2-D-B-1"})
        self.assertContains(respuesta, "Posición <span class=\"mono\">PIC-2-D-B-1</span>")
        self.assertContains(respuesta, "% ocupado")
        respuesta = self.client.get(reverse("mesa:bodega"))
        self.assertContains(respuesta, 'http-equiv="refresh"')
        self.assertNotContains(respuesta, "resaltado")
        respuesta = self.client.get(reverse("mesa:cliente_skus", args=[self.cliente.pk]))
        self.assertContains(respuesta, "?q=COLIMITA-SIX\" title=\"En qué racks está este producto\">¿dónde está?")


class OcupacionFisicaTests(PisoTestCase):
    """Chema 2026-10-07: la ocupación y el contenido de una posición cuentan
    solo lo que está físicamente en el anaquel: ni la capa de apartado (iba
    doble) ni lo que ya está en la mesa de empaque."""

    def test_apartado_no_cuenta_doble_ni_lo_de_empaque_cuenta_en_el_anaquel(self):
        from apps.inventario.services import confirmar_pick, contenido_ubicacion, ocupacion, reservar
        from apps.pedidos.services import _reservar_linea  # noqa: F401 — misma puerta que los pedidos

        self.sku.largo_cm, self.sku.ancho_cm, self.sku.alto_cm = 20, 20, 10
        self.sku.save()
        rack = Ubicacion.objects.create(codigo="PIC-9-A", tipo=Ubicacion.PICKING, largo_cm=100, ancho_cm=20, alto_cm=10)  # caben 5
        Saldo.objects.create(sku=self.sku, ubicacion=rack, estado=Saldo.UBICADO_VENDIBLE, cantidad=4)
        self.assertEqual(ocupacion(rack)["pct"], 80)
        self.assertTrue(reservar(self.sku, 3, "PED-X"))   # capa de 3 sobre las 4: sigue habiendo 4 en el anaquel
        self.assertEqual(ocupacion(rack)["pct"], 80)
        self.assertEqual(contenido_ubicacion(rack)[0]["piezas"], 4)
        confirmar_pick(self.sku, 3, "PED-X")               # 3 se fueron a la mesa (en_empaque conserva la posición)
        self.assertEqual(ocupacion(rack)["pct"], 20)
        self.assertEqual(contenido_ubicacion(rack)[0]["piezas"], 1)
        r = buscar_en_bodega("PIC-9-A")
        self.assertEqual(r["piezas"], 1)
        self.assertEqual([(f["estado"], f["cantidad"], f["en_anaquel"]) for f in r["filas"]], [("vendible", 1, True), ("en empaque (ya salió del anaquel)", 3, False)])
