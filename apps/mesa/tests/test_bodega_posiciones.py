"""Mesa → Bodega → Posiciones (Chema 2026-10-13): lista de posiciones con lo
que hay, cambiar nombre, mover existencias, imprimir la etiqueta de la
posición e imprimir las etiquetas de las cajas; nombres por pasillo en el
plano."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.configuracion.models import TandaEtiquetas
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import TrabajoImpresion
from apps.inventario.models import Ajuste, Conteo, LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import recibir, reservar, ubicar


@override_settings(TORRE_MODO_IMPRESION="relay")
class PosicionesMesaTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.mesa = User.objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol=PerfilUsuario.ROL_MESA)
        self.piso = User.objects.create_user("piso1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.piso, rol=PerfilUsuario.ROL_PISO)
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="CCPL24L", codigo_barras="7500465010011", descripcion="Piedra Lisa 24", variante="24 Pack")
        self.lote = Lote.objects.create(sku=self.sku, codigo="CPL333AB", fecha_caducidad="2027-03-01")
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.a = Ubicacion.objects.create(codigo="PIC-1-I-F-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.b = Ubicacion.objects.create(codigo="PIC-1-D-B-1", tipo=Ubicacion.PICKING)
        self.tarima = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=12, lote_codigo=self.lote.codigo)
        recibir(linea, 12, 0, "mesa1")
        ubicar(self.sku, 12, self.a, self.lote, "mesa1")
        self.client.force_login(self.mesa)
        self.lista = reverse("mesa:bodega_posiciones")
        self.accion_a = reverse("mesa:bodega_posicion_accion", args=[self.a.pk])

    def saldo(self, ubicacion, estado):
        return sum(Saldo.objects.filter(sku=self.sku, ubicacion=ubicacion, estado=estado).values_list("cantidad", flat=True))

    def test_lista_muestra_posiciones_con_su_contenido_y_botones(self):
        r = self.client.get(self.lista)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PIC-1-I-F-2")
        self.assertContains(r, "TAR-01")
        self.assertContains(r, "CPL333AB")
        self.assertContains(r, f'id="pos-datos-{self.a.pk}"')
        self.assertContains(r, "Imprimir etiquetas de las cajas")
        self.assertContains(r, "Imprimir tarima")
        self.assertContains(r, 'name="destino"')
        self.assertEqual([f["ubicacion"].codigo for f in r.context["filas"]], ["PIC-1-D-B-1", "PIC-1-I-F-2", "TAR-01"])
        r = self.client.get(reverse("mesa:bodega"))
        self.assertContains(r, "Posiciones: editar e imprimir")

    def test_el_piso_no_entra(self):
        self.client.force_login(self.piso)
        self.assertEqual(self.client.get(self.lista).status_code, 403)
        self.assertEqual(self.client.post(self.accion_a, {"accion": "renombrar", "nuevo": "A-3"}).status_code, 403)

    def test_renombrar_y_sus_errores(self):
        r = self.client.post(self.accion_a, {"accion": "renombrar", "nuevo": "a-3"}, follow=True)
        self.assertRedirects(r, f"{self.lista}#pos-{self.a.pk}", fetch_redirect_response=False)
        self.assertContains(r, "PIC-1-I-F-2 ahora se llama A-3")
        self.a.refresh_from_db()
        self.assertEqual(self.a.codigo, "A-3")
        self.assertEqual(self.saldo(self.a, Saldo.UBICADO_VENDIBLE), 12)
        r = self.client.post(self.accion_a, {"accion": "renombrar", "nuevo": "TAR-01"}, follow=True)
        self.assertContains(r, "Ya existe una posición TAR-01")
        r = self.client.get(self.lista)
        self.assertEqual([f["ubicacion"].codigo for f in r.context["filas"]], ["A-3", "PIC-1-D-B-1", "TAR-01"])

    def test_mover_existencias_desde_la_lista(self):
        reservar(self.sku, 5, "PED-00001")
        datos = {"accion": "mover", "sku_id": self.sku.pk, "lote_id": self.lote.pk, "cantidad": 10, "destino": "TAR-01", "motivo": "Plan de acomodo"}
        r = self.client.post(self.accion_a, datos, follow=True)
        self.assertContains(r, "10 × CCPL24L lote CPL333AB: de PIC-1-I-F-2 a TAR-01 (con 3 apartada(s)).")
        self.assertEqual((self.saldo(self.a, Saldo.UBICADO_VENDIBLE), self.saldo(self.a, Saldo.RESERVADO)), (2, 2))
        self.assertEqual((self.saldo(self.tarima, Saldo.UBICADO_VENDIBLE), self.saldo(self.tarima, Saldo.RESERVADO)), (10, 3))
        self.assertEqual(Movimiento.objects.get(tipo=Movimiento.TRASLADO).referencia, "PIC-1-I-F-2>TAR-01")
        r = self.client.post(self.accion_a, {**datos, "cantidad": 5}, follow=True)
        self.assertContains(r, "hay 2 pieza(s) vendible(s)")
        r = self.client.post(self.accion_a, {**datos, "cantidad": 1, "destino": "PIC-1-I-F-2"}, follow=True)
        self.assertContains(r, "distinta")
        r = self.client.post(self.accion_a, {**datos, "cantidad": "x"}, follow=True)
        self.assertContains(r, "Captura cuántas piezas mover")

    def test_imprimir_etiqueta_de_la_posicion_y_de_la_tarima(self):
        r = self.client.post(self.accion_a, {"accion": "imprimir_posicion"}, follow=True)
        self.assertContains(r, "La etiqueta de PIC-1-I-F-2 va a la impresora")
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual((trabajo.tipo, trabajo.impresora, trabajo.media, trabajo.paginas, trabajo.referencia, trabajo.cliente),
                         ("tarima", "etiquetas", "Custom.51x51mm", 1, "Posición PIC-1-I-F-2", None))
        with trabajo.pdf.open("rb") as archivo:
            self.assertTrue(archivo.read().startswith(b"%PDF"))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="ubicacion", entidad_id="PIC-1-I-F-2", accion="etiqueta_posicion_impresa").exists())
        self.client.post(reverse("mesa:bodega_posicion_accion", args=[self.tarima.pk]), {"accion": "imprimir_posicion"})
        self.assertEqual(TrabajoImpresion.objects.latest("pk").referencia, "Posición TAR-01")

    def test_imprimir_etiquetas_de_las_cajas_en_tandas(self):
        TandaEtiquetas.objects.create(cajas=5)
        r = self.client.post(self.accion_a, {"accion": "imprimir_cajas", f"n_{self.sku.pk}_{self.lote.pk}": "12"}, follow=True)
        self.assertContains(r, "12 etiqueta(s) de caja de PIC-1-I-F-2 van a la impresora en 3 tanda(s)")
        trabajos = list(TrabajoImpresion.objects.order_by("pk"))
        self.assertEqual([(t.referencia, t.paginas, t.tipo, t.cliente) for t in trabajos], [
            ("PIC-1-I-F-2 etiquetas 1-5", 5, "producto", self.cliente),
            ("PIC-1-I-F-2 etiquetas 6-10", 5, "producto", self.cliente),
            ("PIC-1-I-F-2 etiquetas 11-12", 2, "producto", self.cliente),
        ])
        evento = EventoAuditoria.objects.get(entidad="ubicacion", entidad_id="PIC-1-I-F-2", accion="etiquetas_impresas")
        self.assertEqual(evento.delta["cajas"], 12)
        self.assertEqual(evento.delta["por_producto"], [{"sku": "CCPL24L", "lote": "CPL333AB", "cajas": 12}])
        r = self.client.post(self.accion_a, {"accion": "imprimir_cajas", f"n_{self.sku.pk}_{self.lote.pk}": "0"}, follow=True)
        self.assertContains(r, "Captura cuántas etiquetas")
        self.assertEqual(TrabajoImpresion.objects.count(), 3)


    def test_productos_fijos_desde_la_lista(self):
        r = self.client.post(self.accion_a, {"accion": "asignar", "sku": "ccpl24l"}, follow=True)
        self.assertContains(r, "CCPL24L queda fijo en PIC-1-I-F-2")
        self.assertContains(r, "fijo: CCPL24L")
        self.assertContains(r, 'id="lista-skus"')
        self.assertEqual(r.context["filas"][1]["fijos"][0]["capacidad"], None)  # el SKU de prueba no tiene medidas
        r = self.client.post(self.accion_a, {"accion": "asignar", "sku": "NOEXISTE"}, follow=True)
        self.assertContains(r, "No existe el producto NOEXISTE")
        r = self.client.post(self.accion_a, {"accion": "desasignar", "sku": "CCPL24L"}, follow=True)
        self.assertContains(r, "CCPL24L deja de ser fijo")
        self.assertNotContains(r, "fijo: CCPL24L")

    def test_ajustar_conteo_por_lote_agrega_y_quita_lotes(self):
        """Chema 2026-10-14: contar un producto en una posición por lote, con
        lotes nuevos (se crean con su caducidad) y contado 0 para quitar uno."""
        datos = {
            "accion": "ajustar", "sku_id": self.sku.pk, "lotes": str(self.lote.pk), f"contado_{self.lote.pk}": "10",
            "nuevo_lote": "cpl334ab", "nuevo_caducidad": "2027-04-01", "nuevo_contado": "5", "motivo": "conteo", "nota": "Reacomodo",
        }
        r = self.client.post(self.accion_a, datos, follow=True)
        self.assertRedirects(r, f"{self.lista}#pos-{self.a.pk}", fetch_redirect_response=False)
        html = r.content.decode()
        self.assertIn("CCPL24L en PIC-1-I-F-2: lote CPL333AB: 12 → 10 (-2, AJU-", html)
        self.assertIn("lote CPL334AB: 0 → 5 (+5, AJU-", html)
        self.assertIn("2 conteo(s)", html)
        nuevo = Lote.objects.get(sku=self.sku, codigo="CPL334AB")
        self.assertEqual(str(nuevo.fecha_caducidad), "2027-04-01")
        self.assertEqual(self.saldo(self.a, Saldo.UBICADO_VENDIBLE), 15)
        self.assertEqual(sum(Saldo.objects.filter(ubicacion=self.a, lote=nuevo, estado=Saldo.UBICADO_VENDIBLE).values_list("cantidad", flat=True)), 5)
        self.assertEqual(Conteo.objects.count(), 2)
        self.assertEqual(sorted(Ajuste.objects.values_list("delta", flat=True)), [-2, 5])
        self.assertTrue(all(a.lote_id and a.autorizo_1 == "mesa1" == a.autorizo_2 and a.conteo_id for a in Ajuste.objects.all()))
        self.assertEqual(Movimiento.objects.filter(tipo=Movimiento.AJUSTE).count(), 2)
        evento = EventoAuditoria.objects.get(entidad="ubicacion", entidad_id="PIC-1-I-F-2", accion="conteo_posicion")
        self.assertEqual((evento.delta["sku"], evento.delta["conteos"], evento.motivo), ("CCPL24L", 2, "Reacomodo"))
        # Contado 0 quita el lote nuevo; el que coincide solo deja conteo.
        r = self.client.post(self.accion_a, {"accion": "ajustar", "sku_id": self.sku.pk, "lotes": f"{self.lote.pk},{nuevo.pk}",
                                             f"contado_{self.lote.pk}": "10", f"contado_{nuevo.pk}": "0", "motivo": "conteo"}, follow=True)
        self.assertContains(r, "lote CPL334AB: 5 → 0 (-5, AJU-")
        self.assertFalse(Saldo.objects.filter(ubicacion=self.a, lote=nuevo).exists())
        self.assertEqual((self.saldo(self.a, Saldo.UBICADO_VENDIBLE), Conteo.objects.count(), Ajuste.objects.count()), (10, 4, 3))
        # Sin diferencias: solo conteos. Motivo fuera del catálogo: nada cambia.
        r = self.client.post(self.accion_a, {"accion": "ajustar", "sku_id": self.sku.pk, "lotes": str(self.lote.pk), f"contado_{self.lote.pk}": "10", "motivo": "conteo"}, follow=True)
        self.assertContains(r, "sin diferencias · 1 conteo(s)")
        r = self.client.post(self.accion_a, {"accion": "ajustar", "sku_id": self.sku.pk, "lotes": str(self.lote.pk), f"contado_{self.lote.pk}": "3", "motivo": "invento"}, follow=True)
        self.assertContains(r, "fuera del catálogo")
        self.assertEqual(self.saldo(self.a, Saldo.UBICADO_VENDIBLE), 10)
        self.client.force_login(self.piso)
        self.assertEqual(self.client.post(self.accion_a, datos).status_code, 403)

    def test_contar_de_menos_libera_la_reserva_del_pedido_mas_reciente(self):
        from apps.pedidos.models import LineaPedido, Pedido

        viejo = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana", cp="01780", estado=Pedido.PENDIENTE)
        lv = LineaPedido.objects.create(pedido=viejo, sku=self.sku, cantidad=4, reservada=True)
        self.assertTrue(reservar(self.sku, 4, viejo.folio))
        nuevo = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Beto", cp="01780", estado=Pedido.PENDIENTE)
        ln = LineaPedido.objects.create(pedido=nuevo, sku=self.sku, cantidad=5, reservada=True)
        self.assertTrue(reservar(self.sku, 5, nuevo.folio))
        self.assertEqual(self.saldo(self.a, Saldo.RESERVADO), 9)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.accion_a, {"accion": "ajustar", "sku_id": self.sku.pk, "lotes": str(self.lote.pk), f"contado_{self.lote.pk}": "6", "motivo": "conteo"}, follow=True)
        html = r.content.decode()
        self.assertIn("lote CPL333AB: 12 → 6 (-6, AJU-", html)
        self.assertIn(f"Reserva liberada por falta de físico: {nuevo.folio} (5)", html)  # el más reciente pierde su reserva
        lv.refresh_from_db()
        ln.refresh_from_db()
        self.assertEqual((lv.reservada, ln.reservada), (True, False))
        self.assertEqual((self.saldo(self.a, Saldo.UBICADO_VENDIBLE), self.saldo(self.a, Saldo.RESERVADO)), (6, 4))
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(nuevo.pk), accion="reserva_liberada_por_conteo")
        self.assertEqual(evento.delta, {"sku": "CCPL24L", "piezas": 5})
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(viejo.pk), accion="reserva_liberada_por_conteo").exists())


class PlanoPorPasilloTests(TestCase):
    def test_nombres_por_pasillo_se_dibujan_por_pasillo_con_cuatro_celdas_por_piso(self):
        from apps.mesa.plano import RACK_X, racks_bodega
        for codigo in ("A-1", "A-2", "A-3", "A-7", "B-1", "B-9", "B-16", "PIC-9-1"):
            Ubicacion.objects.get_or_create(codigo=codigo, defaults={"tipo": Ubicacion.PICKING})
        racks = {r["etiqueta"]: r["filas"] for r in racks_bodega()}
        self.assertEqual(list(racks), ["Rack 9", "Pasillo A", "Pasillo B"])
        celdas = {f["codigo"]: f for f in racks["Pasillo A"]}
        ancho = celdas["A-1"]["w"]
        self.assertEqual((celdas["A-1"]["x"], celdas["A-2"]["x"], celdas["A-3"]["x"]), (RACK_X, RACK_X + ancho, RACK_X))
        self.assertLess(celdas["A-7"]["y"], celdas["A-3"]["y"])  # piso 4 arriba
        self.assertLess(celdas["A-3"]["y"], celdas["A-1"]["y"])  # piso 2 arriba del piso 1
        b = {f["codigo"]: f for f in racks["Pasillo B"]}
        self.assertEqual((b["B-1"]["x"], b["B-9"]["x"], b["B-16"]["x"]), (RACK_X, RACK_X + 2 * ancho, RACK_X + 3 * ancho))
        self.assertTrue(all(f["mini"] for f in racks["Pasillo A"]))
