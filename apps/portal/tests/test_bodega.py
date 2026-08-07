"""Tests de portal:bodega — el plano de la bodega filtrado al cliente.

Lo crítico: TODO llega filtrado por request.cliente. Lo ajeno JAMÁS aparece,
ni en los conteos de los badges. El stock se siembra por la puerta oficial
(ASN → recibir → ubicar), como en los tests de piso.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, PerfilUsuario
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.pedidos.models import LineaPedido, Pedido


class BaseBodegaPortal(TestCase):
    """Dos clientes con bodega compartida: el escenario mínimo de aislamiento."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.colima = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", buffer_stock=0,
        )
        cls.otro = Cliente.objects.create(
            nombre="Mezcal Nocturno", slug="nocturno", buffer_stock=0,
        )
        cls.karina = User.objects.create_user("karina", password="colima2026")
        PerfilUsuario.objects.create(
            usuario=cls.karina, rol=PerfilUsuario.ROL_PORTAL, cliente=cls.colima,
        )
        cls.piso = User.objects.create_user("piso1", password="piso2026")
        PerfilUsuario.objects.create(usuario=cls.piso, rol=PerfilUsuario.ROL_PISO)

        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        cls.ubic_a1 = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        cls.ubic_a2 = Ubicacion.objects.create(codigo="A-01-2", tipo=Ubicacion.PICKING)
        Ubicacion.objects.create(codigo="RES-01", tipo=Ubicacion.RESERVA)
        for codigo in ("SAL-PQX", "SAL-LOCAL", "SAL-OTRO"):
            Ubicacion.objects.create(codigo=codigo, tipo=Ubicacion.SALIDA)

        cls.sku = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six pack",
            peso_gr=2000, requiere_lote=False, precio_declarado=Decimal("180.00"),
        )
        cls.sku_ajeno = SKU.objects.create(
            cliente=cls.otro, codigo="MEZCAL-750", descripcion="Mezcal joven 750 ml",
            requiere_lote=False,
        )

        # Stock vendible de ambos clientes, por la puerta oficial.
        cls._stock(cls.sku, 40, cls.ubic_a1)
        cls._stock(cls.sku_ajeno, 24, cls.ubic_a2)

        # Un pedido EN_PICKING por cliente.
        cls.pedido = cls._pedido_en_picking(cls.colima, cls.sku)
        cls.pedido_ajeno = cls._pedido_en_picking(cls.otro, cls.sku_ajeno)

        # Una ASN EN_RECEPCION por cliente (descarga a medias).
        cls.asn = cls._asn_en_recepcion(cls.colima, cls.sku, anunciadas=24, recibidas=7)
        cls.asn_ajena = cls._asn_en_recepcion(cls.otro, cls.sku_ajeno, anunciadas=10, recibidas=4)

    # ── Siembra por la puerta oficial ──

    @classmethod
    def _stock(cls, sku, cantidad, ubicacion):
        from apps.inventario.services import recibir, ubicar

        orden = OrdenEntrada.objects.create(cliente=sku.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=cantidad)
        recibir(linea, cantidad, 0, cls.piso)
        ubicar(sku, cantidad, ubicacion, None, cls.piso)
        return orden

    @classmethod
    def _pedido_en_picking(cls, cliente, sku):
        pedido = Pedido.objects.create(
            cliente=cliente, origen="manual", estado=Pedido.EN_PICKING,
            comprador_nombre="Ana Comprador", cp="28017", es_local=True, direccion={},
        )
        LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=2)
        return pedido

    @classmethod
    def _asn_en_recepcion(cls, cliente, sku, anunciadas, recibidas):
        from apps.inventario.services import recibir

        orden = OrdenEntrada.objects.create(cliente=cliente, tarimas=24)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=anunciadas)
        recibir(linea, recibidas, 0, cls.piso)  # parcial: queda EN_RECEPCION
        orden.refresh_from_db()
        return orden

    def entrar(self):
        self.client.login(username="karina", password="colima2026")

    def get_bodega(self):
        self.entrar()
        return self.client.get(reverse("portal:bodega"))


class BodegaPortalTests(BaseBodegaPortal):
    def test_carga_con_copy_de_karina(self):
        respuesta = self.get_bodega()
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Bodega en vivo")
        self.assertContains(respuesta, "Tu producto ahora mismo")
        self.assertNotContains(respuesta, "tiempo real")  # prohibido en el portal

    def test_solo_datos_del_cliente_hasta_en_los_conteos(self):
        respuesta = self.get_bodega()
        badges = respuesta.context["badges"]
        self.assertEqual(badges["almacen"], 40)      # las 24 del otro NO cuentan
        self.assertEqual(badges["packing"], 1)
        # Solo ASNs propias vivas: la EN_RECEPCION + la de siembra (RECIBIDA).
        self.assertEqual(badges["descarga"], 2)
        self.assertIsNone(badges["oficina"])         # incidencias: solo mesa
        # Lo propio aparece; lo ajeno JAMÁS (ni folio ni SKU).
        self.assertContains(respuesta, self.pedido.folio)
        self.assertContains(respuesta, "COLIMITA-SIX")
        self.assertNotContains(respuesta, self.pedido_ajeno.folio)
        self.assertNotContains(respuesta, self.asn_ajena.folio)
        self.assertNotContains(respuesta, "MEZCAL-750")
        self.assertNotContains(respuesta, "Mezcal Nocturno")

    def test_asn_en_recepcion_aparece_en_el_panel_de_descarga(self):
        respuesta = self.get_bodega()
        self.assertEqual(respuesta.context["zona_activa"], "descarga")
        self.assertContains(respuesta, "Estamos descargando tu entrega")
        self.assertContains(respuesta, self.asn.folio)
        self.assertContains(respuesta, "7 de 24 piezas contadas")

    def test_plano_con_zonas_y_racks(self):
        respuesta = self.get_bodega()
        for zona in ("descarga", "almacen", "packing", "paquetes_listos", "cuarentena"):
            self.assertContains(respuesta, f'data-zona="{zona}"')
        self.assertContains(respuesta, "A-01-1")
        self.assertContains(respuesta, "RES-01")


class AccesoBodegaPortalTests(BaseBodegaPortal):
    def test_anonimo_va_a_login(self):
        respuesta = self.client.get(reverse("portal:bodega"))
        self.assertEqual(respuesta.status_code, 302)

    def test_rol_piso_no_entra(self):
        self.client.login(username="piso1", password="piso2026")
        respuesta = self.client.get(reverse("portal:bodega"))
        self.assertEqual(respuesta.status_code, 403)
