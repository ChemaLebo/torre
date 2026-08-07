"""Tests de mesa:bodega — el plano vivo del Local 380 E.

El stock SIEMPRE se siembra por la puerta oficial (ASN → recibir → ubicar de
inventario.services), como en los tests de piso: así los conteos del plano
prueban el flujo real del contrato y no un fixture inventado.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, PerfilUsuario
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.pedidos.models import LineaPedido, Pedido

ZONAS_CON_PANEL = (
    "descarga", "almacen", "packing", "paquetes_listos", "cuarentena", "oficina",
)


class BaseBodegaMesa(TestCase):
    """Bodega mínima del Local 380 E + usuarios de los tres roles."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.colima = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", buffer_stock=0,
        )
        cls.usuario_mesa = User.objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=cls.usuario_mesa, rol=PerfilUsuario.ROL_MESA)
        cls.usuario_portal = User.objects.create_user("karina", password="x12345678")
        PerfilUsuario.objects.create(
            usuario=cls.usuario_portal, rol=PerfilUsuario.ROL_PORTAL, cliente=cls.colima,
        )
        cls.usuario_piso = User.objects.create_user("piso1", password="x12345678")
        PerfilUsuario.objects.create(usuario=cls.usuario_piso, rol=PerfilUsuario.ROL_PISO)

        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        cls.ubic_picking = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        Ubicacion.objects.create(codigo="A-01-2", tipo=Ubicacion.PICKING)
        Ubicacion.objects.create(codigo="RES-01", tipo=Ubicacion.RESERVA)
        Ubicacion.objects.create(codigo="MER-01", tipo=Ubicacion.MERMA)
        for codigo in ("SAL-PQX", "SAL-LOCAL", "SAL-OTRO"):
            Ubicacion.objects.create(codigo=codigo, tipo=Ubicacion.SALIDA)

        cls.sku = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six pack",
            peso_gr=2000, requiere_lote=False, precio_declarado=Decimal("180.00"),
        )

    # ── Helpers ──

    def get_bodega(self):
        self.client.force_login(self.usuario_mesa)
        return self.client.get(reverse("mesa:bodega"))

    def crear_stock(self, sku=None, cantidad=40, ubicacion=None):
        """Stock vendible por la puerta oficial: ASN → recibir → ubicar."""
        from apps.inventario.services import recibir, ubicar

        sku = sku or self.sku
        orden = OrdenEntrada.objects.create(cliente=sku.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=cantidad)
        recibir(linea, cantidad, 0, self.usuario_mesa)
        ubicar(sku, cantidad, ubicacion or self.ubic_picking, None, self.usuario_mesa)
        orden.refresh_from_db()
        return orden

    def crear_pedido_en_picking(self, cantidad=2, pickeada=0, es_local=True):
        pedido = Pedido.objects.create(
            cliente=self.colima, origen="manual", estado=Pedido.EN_PICKING,
            comprador_nombre="Ana Prueba", cp="28017" if es_local else "44100",
            es_local=es_local, direccion={},
        )
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=cantidad, cantidad_pickeada=pickeada,
        )
        return pedido


class PlanoBodegaTests(BaseBodegaMesa):
    def test_carga_con_zonas_y_codigos_de_ubicacion_reales(self):
        respuesta = self.get_bodega()
        self.assertEqual(respuesta.status_code, 200)
        for zona in ZONAS_CON_PANEL:
            self.assertContains(respuesta, f'data-zona="{zona}"')
            self.assertContains(respuesta, f'data-panel="{zona}"')
        # Racks del almacén con las ubicaciones reales + corrales y cuarentena.
        for codigo in ("A-01-1", "A-01-2", "RES-01", "SAL-PQX", "SAL-LOCAL", "SAL-OTRO", "MER-01"):
            self.assertContains(respuesta, codigo)

    def test_zona_inicial_es_packing_sin_descarga_activa(self):
        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["zona_activa"], "packing")

    def test_almacen_cuenta_el_stock_sembrado_por_la_puerta_oficial(self):
        from apps.inventario.services import reservar

        self.crear_stock(cantidad=40)
        self.assertTrue(reservar(self.sku, 5, "PED-TEST"))

        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["badges"]["almacen"], 40)
        almacen = respuesta.context["almacen"]
        self.assertEqual(almacen["total_vendible"], 40)
        self.assertEqual(almacen["total_apartado"], 5)
        self.assertEqual(almacen["n_skus"], 1)
        fila = next(u for u in almacen["ubicaciones"] if u["codigo"] == "A-01-1")
        self.assertEqual(fila["vendible"], 40)
        self.assertEqual(fila["apartado"], 5)
        por_cliente = {f["sku__cliente__nombre"]: f["piezas"] for f in almacen["por_cliente"]}
        self.assertEqual(por_cliente["Cervecería Colima"], 40)

    def test_descarga_muestra_asn_en_recepcion_con_avance_y_putaway(self):
        from apps.inventario.services import recibir

        orden = OrdenEntrada.objects.create(cliente=self.colima, tarimas=24)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=24)
        recibir(linea, 7, 0, self.usuario_mesa)  # parcial: queda EN_RECEPCION
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.EN_RECEPCION)

        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["badges"]["descarga"], 1)
        self.assertEqual(respuesta.context["zona_activa"], "descarga")
        self.assertContains(respuesta, orden.folio)
        self.assertContains(respuesta, "7/24 pzas")
        putaway = respuesta.context["putaway"]
        self.assertEqual(len(putaway), 1)
        self.assertEqual(putaway[0]["sku__codigo"], "COLIMITA-SIX")
        self.assertEqual(putaway[0]["piezas"], 7)

    def test_packing_separa_surtido_de_listos_para_empacar(self):
        surtiendo = self.crear_pedido_en_picking(cantidad=2, pickeada=0)
        listo = self.crear_pedido_en_picking(cantidad=2, pickeada=2)

        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["badges"]["packing"], 2)
        self.assertEqual(
            [p.folio for p in respuesta.context["surtiendo"]], [surtiendo.folio]
        )
        self.assertEqual(
            [p.folio for p in respuesta.context["listos_empaque"]], [listo.folio]
        )
        self.assertContains(respuesta, surtiendo.folio)
        self.assertContains(respuesta, listo.folio)

    def test_paquetes_listos_agrupa_por_corral(self):
        # Sin flota propia (TORRE["FLOTA_PROPIA"]=False, default) el pedido
        # es_local cae al corral de su carrier real (paquetexpress → SAL-PQX);
        # SAL-LOCAL queda solo para guías "local" viejas.
        pedido = Pedido.objects.create(
            cliente=self.colima, origen="manual", estado=Pedido.EMPACADO,
            comprador_nombre="Ana Prueba", cp="28017", es_local=True, direccion={},
        )
        LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=1)

        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["badges"]["paquetes_listos"], 1)
        grupo = next(
            g for g in respuesta.context["corrales"] if g["codigo"] == "SAL-PQX"
        )
        self.assertEqual([p.folio for p in grupo["pedidos"]], [pedido.folio])
        self.assertEqual(grupo["paquetes"], 1)
        grupo_local = next(
            g for g in respuesta.context["corrales"] if g["codigo"] == "SAL-LOCAL"
        )
        self.assertEqual(grupo_local["pedidos"], [])
        self.assertContains(respuesta, pedido.folio)

    def test_cuarentena_y_oficina_con_conteos_vivos(self):
        from apps.incidencias.services import abrir_incidencia
        from apps.inventario.services import recibir

        orden = OrdenEntrada.objects.create(cliente=self.colima)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=10)
        recibir(linea, 7, 3, self.usuario_mesa)  # 3 dañadas → cuarentena
        abrir_incidencia(self.colima, "DAN", "manual", texto="Caja estrellada en descarga.")

        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["badges"]["cuarentena"], 3)
        self.assertEqual(respuesta.context["badges"]["oficina"], 1)
        self.assertEqual(respuesta.context["cuarentena"][0]["sku__codigo"], "COLIMITA-SIX")
        self.assertEqual(respuesta.context["cuarentena"][0]["piezas"], 3)


class AccesoBodegaMesaTests(BaseBodegaMesa):
    def test_portal_no_entra(self):
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.get(reverse("mesa:bodega"))
        self.assertEqual(respuesta.status_code, 403)

    def test_piso_no_entra(self):
        self.client.force_login(self.usuario_piso)
        respuesta = self.client.get(reverse("mesa:bodega"))
        self.assertEqual(respuesta.status_code, 403)

    def test_anonimo_va_a_login(self):
        respuesta = self.client.get(reverse("mesa:bodega"))
        self.assertEqual(respuesta.status_code, 302)
