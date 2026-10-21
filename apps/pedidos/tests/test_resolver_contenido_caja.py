"""Resolver el contenido de una caja pieza por pieza (Chema 2026-10-21): una
caja despachada que regresó reingresa como retorno a la posición y lote
elegidos o se da por no recuperada; una caja cuyo contenido el manifiesto
descontó de más (PED-00281) no vuelve a descontar nada, y los contadores de
la línea se recalculan desde las cajas que de verdad salieron."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import recibir, reservar, confirmar_pick, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def suma(sku, estado):
    return sum(Saldo.objects.filter(sku=sku, estado=estado).values_list("cantidad", flat=True))


@override_settings(ENVIA_API_KEY="")
class ResolverContenidoCajaTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="C24OROL", descripcion="Caja 24 Oro", peso_gr=9000,
                                      requiere_lote=True, precio_declarado=Decimal(500))
        self.lote = Lote.objects.create(sku=self.sku, codigo="CCYL75ABCD", fecha_caducidad="2027-05-25")
        self.rec = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="D-6", tipo=Ubicacion.PICKING)
        self.mesa = get_user_model().objects.create_user(username="mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=10, lote_codigo=self.lote.codigo)
        recibir(linea, 10, 0, self.mesa)
        ubicar(self.sku, 10, self.pic, self.lote, self.mesa)

    def _pedido(self, estado=Pedido.PARCIALMENTE_DESPACHADO):
        return Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana", cp="01780", estado=estado)

    def test_caja_despachada_que_regreso(self):
        pedido = self._pedido(Pedido.RETORNADO)
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=2, reservada=True, cantidad_pickeada=2, cantidad_despachada=2)
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("18"), carrier="estafeta", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero="G-1", proveedor="envia", estado=Guia.RETORNO)
        self.assertEqual([c.pk for c in services.cajas_por_reingresar(pedido)], [caja.pk])
        with self.assertRaisesMessage(ValueError, "lleva 2 pieza(s) y decidiste 1"):
            services.resolver_contenido_caja(pedido, caja, [{"sku": self.sku, "cantidad": 1, "destino": "bodega", "ubicacion": self.pic, "lote": self.lote}], self.mesa)
        services.resolver_contenido_caja(pedido, caja, [
            {"sku": self.sku, "cantidad": 1, "destino": "bodega", "ubicacion": self.pic, "lote": self.lote},
            {"sku": self.sku, "cantidad": 1, "destino": "merma", "motivo": "se perdió"},
        ], self.mesa, motivo="una volvió, la otra no")
        caja.refresh_from_db(); pedido.refresh_from_db(); linea.refresh_from_db()
        self.assertEqual((caja.reingreso_estado, pedido.reingreso_estado), (Paquete.REINGRESADO, Pedido.REINGRESADO))
        self.assertEqual(suma(self.sku, Saldo.UBICADO_VENDIBLE), 11)
        retorno = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.RETORNO)
        self.assertEqual((retorno.delta, retorno.lote.codigo, retorno.estado_destino), (1, "CCYL75ABCD", "ubicado_vendible"))
        self.assertEqual((linea.cantidad_pickeada, linea.cantidad_despachada), (2, 2))  # la caja sí salió: nada cambia
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="reingreso_resuelto")
        self.assertEqual(evento.delta["detalle"][0]["fuera_del_kardex"], 2)
        self.assertEqual(services.cajas_por_reingresar(pedido), [])

    def test_caja_cancelada_cuyo_contenido_el_manifiesto_desconto_de_mas(self):
        # Como PED-00281: 3 piezas pickeadas, el manifiesto descontó 3 aunque solo salió la caja 1 (2 piezas).
        pedido = self._pedido()
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=3, reservada=True, cantidad_pickeada=3, cantidad_despachada=3)
        self.assertTrue(reservar(self.sku, 3, pedido.folio))
        confirmar_pick(self.sku, 3, pedido.folio)
        from apps.inventario.services import despachar
        despachar(self.sku, 3, pedido.folio)
        fuera = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("18"), carrier="estafeta", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=fuera, linea_pedido=linea, cantidad=2)
        bodega = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("9"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=bodega, linea_pedido=linea, cantidad=1)
        bodega.transicionar(Paquete.CANCELADO, actor=self.mesa)
        self.assertEqual(services.sobre_descontadas(linea), 1)
        self.assertEqual(suma(self.sku, Saldo.EN_EMPAQUE), 0)
        # "No regresa": no hay nada que descontar (ya lo descontó el manifiesto); los contadores quedan en lo que salió.
        antes = Movimiento.objects.count()
        services.resolver_contenido_caja(pedido, bodega, [{"sku": self.sku, "cantidad": 1, "destino": "merma", "descontar": True}], self.mesa)
        self.assertEqual(Movimiento.objects.count(), antes)
        linea.refresh_from_db(); bodega.refresh_from_db()
        self.assertEqual((linea.cantidad_pickeada, linea.cantidad_despachada, bodega.reingreso_estado), (2, 2, Paquete.NO_RECUPERADO))

    def test_caja_cancelada_cuyo_contenido_si_esta_en_empaque(self):
        pedido = self._pedido(Pedido.EMPACADO)
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=1, reservada=True, cantidad_pickeada=1)
        self.assertTrue(reservar(self.sku, 1, pedido.folio))
        services.confirmar_linea_pick(linea, 0 or 1, self.mesa, ubicacion=self.pic, lote=self.lote) if False else None
        from apps.pedidos.models import PickLinea
        PickLinea.objects.create(linea=linea, ubicacion=self.pic, lote=self.lote, cantidad=1, estado=PickLinea.EN_EMPAQUE)
        confirmar_pick(self.sku, 1, pedido.folio, preferir=[(self.pic.pk, self.lote.pk, 1)])
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("9"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1)
        caja.transicionar(Paquete.CANCELADO, actor=self.mesa)
        services.resolver_contenido_caja(pedido, caja, [{"sku": self.sku, "cantidad": 1, "destino": "cuarentena", "lote": self.lote}], self.mesa)
        self.assertEqual((suma(self.sku, Saldo.EN_EMPAQUE), suma(self.sku, Saldo.CUARENTENA)), (0, 1))
        self.assertEqual(PickLinea.objects.get(linea=linea).estado, PickLinea.REGRESADO)
        linea.refresh_from_db()
        self.assertEqual((linea.cantidad_pickeada, linea.cantidad_despachada), (0, 0))
