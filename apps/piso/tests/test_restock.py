"""Restock de rack (Chema 2026-10-13): nace cuando lo apartado rebasa el rack
o desde el picking, va antes de los pedidos en Mi turno y se hace caja por
caja: escanear la caja (el lote que caduca primero fuera del rack), escanear
el rack, confirmar; las movidas son las que cupieron."""
from django.urls import reverse

from apps.catalogo.models import Lote, Ubicacion
from apps.catalogo.services import asignar_producto
from apps.core.models import EventoAuditoria
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Restock, Saldo
from apps.inventario.services import abrir_restock, recibir, reservar, sugerir_restock, ubicar
from apps.pedidos.services import iniciar_picking

from .base import PisoTestCase


class RestockTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        self.login_piso()
        self.sku.largo_cm, self.sku.ancho_cm, self.sku.alto_cm = 26, 20, 25
        self.sku.save()
        self.rack = Ubicacion.objects.create(codigo="A-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.otro_rack = Ubicacion.objects.create(codigo="B-1", tipo=Ubicacion.PICKING)
        self.viejo = Lote.objects.create(sku=self.sku, codigo="VIEJO", fecha_caducidad="2026-12-01")
        self.nuevo = Lote.objects.create(sku=self.sku, codigo="NUEVO", fecha_caducidad="2027-06-01")
        self.stock(self.tarima, self.viejo, 20)
        self.stock(self.rack, self.nuevo, 2)
        asignar_producto(self.rack, self.sku, actor="mesa1")
        self.barras = self.sku.codigo_barras or self.sku.codigo

    def stock(self, ubicacion, lote, cantidad):
        orden = OrdenEntrada.objects.create(cliente=self.sku.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=cantidad, lote_codigo=lote.codigo)
        recibir(linea, cantidad, 0, self.operador)
        ubicar(self.sku, cantidad, ubicacion, lote, self.operador)

    def saldo(self, ubicacion, estado):
        return sum(Saldo.objects.filter(sku=self.sku, ubicacion=ubicacion, estado=estado).values_list("cantidad", flat=True))

    def test_sugerencia_y_nace_solo_cuando_lo_apartado_rebasa_el_rack(self):
        s = sugerir_restock(self.sku)
        self.assertEqual((s["rack"], s["en_rack"], s["capacidad"], s["lote"], s["origen"], s["cantidad"]), (self.rack, 2, 36, self.viejo, self.tarima, 20))
        self.assertTrue(reservar(self.sku, 2, "PED-A"))  # 2 apartadas ≤ 2 en rack: nada
        self.assertFalse(Restock.objects.exists())
        self.assertTrue(reservar(self.sku, 1, "PED-B"))  # 3 > 2: nace
        tarea = Restock.objects.get()
        self.assertEqual((tarea.estado, tarea.rack, tarea.lote, tarea.origen, tarea.cantidad_sugerida), (Restock.PENDIENTE, self.rack, self.viejo, self.tarima, 20))
        self.assertTrue(reservar(self.sku, 1, "PED-C"))  # no duplica
        self.assertEqual(Restock.objects.count(), 1)
        self.assertEqual(abrir_restock(self.sku), tarea)

    def test_mi_turno_lo_pone_antes_de_los_pedidos_y_empezar_abre_la_tarea(self):
        pedido = self.crear_pedido(cantidad=1)
        tarea = abrir_restock(self.sku, actor="mesa1")
        r = self.client.get(reverse("piso:home"))
        self.assertEqual(r.context["restock_siguiente"], tarea)
        self.assertContains(r, "trae hasta 20")
        self.assertContains(r, 'value="siguiente_restock"')
        self.assertNotContains(r, pedido.folio)
        r = self.client.post(reverse("piso:home"), {"accion": "siguiente_restock", "restock_id": tarea.pk})
        self.assertRedirects(r, reverse("piso:restock", args=[tarea.pk]), fetch_redirect_response=False)
        tarea.refresh_from_db()
        self.assertEqual((tarea.estado, tarea.asignado_a), (Restock.EN_CURSO, self.operador))

    def test_caja_rack_confirmar_una_por_una_y_terminar(self):
        pedido = self.crear_pedido(cantidad=5)
        iniciar_picking(pedido, self.operador)
        picking = reverse("piso:picking_pedido", args=[pedido.pk])
        linea = pedido.lineas.get()
        r = self.client.post(picking, {"accion": "restock", "linea_id": linea.pk})
        tarea = Restock.objects.get()
        url = reverse("piso:restock", args=[tarea.pk])
        self.assertRedirects(r, url, fetch_redirect_response=False)
        self.assertEqual((tarea.pedido, tarea.estado), (pedido, Restock.EN_CURSO))
        r = self.client.get(url)
        self.assertContains(r, "Trae hasta 20")
        self.assertContains(r, "Paso 1")
        # Caja equivocada: otro producto; lote equivocado: el nuevo.
        r = self.client.post(url, {"accion": "escanear", "codigo": "P|0000000000000|VIEJO"}, follow=True)
        self.assertContains(r, f"Esa caja no es de {self.sku.codigo}")
        r = self.client.post(url, {"accion": "escanear", "codigo": f"P|{self.barras}|NUEVO"}, follow=True)
        self.assertContains(r, "primero sale el lote VIEJO")
        # Caja buena (con teclado en español: ] por |) → paso 2; rack equivocado; rack bueno → paso 3; confirmar.
        r = self.client.post(url, {"accion": "escanear", "codigo": f"P]{self.barras}]VIEJO"}, follow=True)
        self.assertContains(r, "Paso 2")
        self.assertContains(r, "Regresar (esta caja no va)")
        r = self.client.post(url, {"accion": "escanear", "codigo": "U|B-1"}, follow=True)
        self.assertContains(r, "esta caja va a A-3")
        r = self.client.post(url, {"accion": "escanear", "codigo": "U|A-3"}, follow=True)
        self.assertContains(r, "Paso 3")
        r = self.client.post(url, {"accion": "confirmar"}, follow=True)
        self.assertContains(r, "1 caja(s) en A-3; faltan 19")
        self.assertEqual((self.saldo(self.rack, Saldo.UBICADO_VENDIBLE), self.saldo(self.tarima, Saldo.UBICADO_VENDIBLE)), (3, 19))
        self.assertEqual(Movimiento.objects.filter(tipo=Movimiento.TRASLADO).count(), 1)
        tarea.refresh_from_db()
        self.assertEqual(tarea.cantidad_movida, 1)
        # Regresar a medias no mueve nada.
        self.client.post(url, {"accion": "escanear", "codigo": f"P|{self.barras}|VIEJO"})
        r = self.client.post(url, {"accion": "regresar"}, follow=True)
        self.assertContains(r, "Paso 1")
        self.assertEqual(self.saldo(self.rack, Saldo.UBICADO_VENDIBLE), 3)
        # Terminar: las movidas son las que cupieron; vuelve al pedido.
        r = self.client.post(url, {"accion": "terminar"})
        self.assertRedirects(r, picking, fetch_redirect_response=False)
        tarea.refresh_from_db()
        self.assertEqual((tarea.estado, tarea.cantidad_movida), (Restock.HECHO, 1))
        self.assertEqual(EventoAuditoria.objects.get(entidad="restock", entidad_id=tarea.folio, accion="restock_hecho").delta["movidas"], 1)
        r = self.client.get(url, follow=True)
        self.assertContains(r, "ya está hecho")
        self.assertEqual(Restock.objects.count(), 1)

    def test_otro_operador_no_abre_la_tarea_en_curso(self):
        from django.contrib.auth import get_user_model

        from apps.core.models import PerfilUsuario
        tarea = abrir_restock(self.sku)
        self.client.post(reverse("piso:home"), {"accion": "siguiente_restock", "restock_id": tarea.pk})
        otro = get_user_model().objects.create_user("piso2", password="x")
        PerfilUsuario.objects.create(usuario=otro, rol=PerfilUsuario.ROL_PISO, pin="2222")
        self.client.force_login(otro)
        r = self.client.get(reverse("piso:home"))
        self.assertIsNone(r.context["restock_siguiente"])
        r = self.client.get(reverse("piso:restock", args=[tarea.pk]), follow=True)
        self.assertContains(r, f"{tarea.folio} lo tiene piso1")
