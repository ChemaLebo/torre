"""Producto erróneo (`ERR`, Chema 2026-10-10): tipo nuevo, P1, con el producto
que llegó en su lugar en `sku`; el portal lo redacta para el personal del
cliente y los avisos lo nombran en palabras."""
from django.test import TestCase

from apps.catalogo.models import SKU
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.mensajeria.services import TIPO_INCIDENCIA_LEGIBLE
from apps.portal.forms import TIPOS_INCIDENCIA_PORTAL

from .utils import crear_cliente, crear_pedido


class TipoProductoErroneoTests(TestCase):
    def test_nace_p1_visible_al_cliente_y_con_el_producto_que_llego(self):
        cliente = crear_cliente()
        pedido = crear_pedido(cliente)
        otro = SKU.objects.create(cliente=cliente, codigo="C12", descripcion="Caja 12")
        inc = abrir_incidencia(cliente, Incidencia.TIPO_ERR, Incidencia.ORIGEN_CLIENTE, pedido=pedido, sku=otro, texto="Otro producto")
        self.assertEqual((inc.prioridad, inc.interna, inc.sku, inc.get_tipo_display()), ("P1", False, otro, "Producto erróneo"))

    def test_el_portal_y_los_avisos_lo_nombran_para_el_cliente(self):
        self.assertEqual(dict(TIPOS_INCIDENCIA_PORTAL)[Incidencia.TIPO_ERR], "Le llegó un producto distinto al comprador")
        self.assertEqual(TIPO_INCIDENCIA_LEGIBLE["ERR"], "producto erróneo")
        self.assertIn(Incidencia.TIPO_ERR, dict(Incidencia.TIPOS))


class CorregirContenidoCajaTests(TestCase):
    """Fase 2 (Chema 2026-10-10): la caja llevaba otro producto. Se corrige el
    renglón de la caja (no el line item), se ajusta inventario con doble
    firma si se pide, y la caja entra a Reingresos por decidir con lo que de
    verdad salió."""

    def setUp(self):
        from decimal import Decimal

        from django.contrib.auth import get_user_model

        from apps.catalogo.models import Ubicacion
        from apps.core.models import PerfilUsuario
        from apps.envios.models import Paquete, PaqueteLinea
        from apps.inventario.models import Saldo
        from apps.pedidos.models import LineaPedido, Pedido

        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente)
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.ENTREGADO)
        self.pedido.refresh_from_db()
        self.six = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", precio_declarado=Decimal("300"))
        self.c12 = SKU.objects.create(cliente=self.cliente, codigo="C12", descripcion="Caja 12", precio_declarado=Decimal("600"))
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=self.six, cantidad=2, reservada=True, cantidad_despachada=2)
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("8"), carrier="estafeta", estado=Paquete.DESPACHADO)
        self.renglon = PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.linea, cantidad=2)
        pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        Saldo.objects.create(sku=self.six, ubicacion=pic, estado=Saldo.UBICADO_VENDIBLE, cantidad=3)
        self.saldo_c12 = Saldo.objects.create(sku=self.c12, ubicacion=pic, estado=Saldo.UBICADO_VENDIBLE, cantidad=2)
        self.mesa1 = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa1, rol="mesa", pin="3333")
        mesa2 = get_user_model().objects.create_user("mesa2", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa2, rol="mesa", pin="4444")
        self.firmas = ("mesa1", "3333", "mesa2", "4444")
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_ERR, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, sku=self.c12, texto="Otro producto")

    def _vendible(self, sku):
        from django.db.models import Sum

        from apps.inventario.models import Saldo
        return Saldo.objects.filter(sku=sku, estado=Saldo.UBICADO_VENDIBLE).aggregate(t=Sum("cantidad"))["t"] or 0

    def test_parte_el_renglon_ajusta_y_deja_la_caja_por_decidir_con_lo_que_salio(self):
        from apps.core.models import EventoAuditoria
        from apps.inventario.models import Ajuste, Movimiento
        from apps.pedidos.services import cajas_por_reingresar, registrar_reingreso, reingresos_por_decidir
        from .. import services

        corregido, ajustes = services.corregir_contenido_caja(self.inc, self.caja, self.linea, 1, self.c12, self.mesa1, ajustar=True, firmas=self.firmas)
        self.caja.refresh_from_db()
        self.renglon.refresh_from_db()
        self.assertTrue(self.caja.contenido_erroneo)
        self.assertEqual((self.renglon.cantidad, self.renglon.sku_real), (1, None))  # lo que sí fue SIX
        self.assertEqual((corregido.cantidad, corregido.sku_real, corregido.linea_pedido), (1, self.c12, self.linea))
        self.assertEqual(self.linea.pedido.lineas.count(), 1)  # los line items no se tocan
        self.assertEqual([(a.sku.codigo, a.delta, a.motivo, a.incidencia_ref) for a in ajustes],
                         [("SIX", 1, Ajuste.MOTIVO_PRODUCTO_ERRONEO, self.inc.folio), ("C12", -1, Ajuste.MOTIVO_PRODUCTO_ERRONEO, self.inc.folio)])
        self.assertEqual((self._vendible(self.six), self._vendible(self.c12)), (4, 1))
        self.assertEqual(Movimiento.objects.filter(tipo=Movimiento.AJUSTE).count(), 2)
        self.assertIn("salió en lugar de Six", corregido.texto_para_piso)
        # La caja queda por decidir con su contenido REAL: el SIX que sí viajó y el C12 que salió en lugar del otro.
        self.assertEqual(cajas_por_reingresar(self.pedido), [self.caja])
        self.assertIn(self.pedido, list(reingresos_por_decidir()))
        orden = registrar_reingreso(self.pedido, self.mesa1)
        self.assertEqual(sorted((l.sku.codigo, l.cantidad_anunciada) for l in orden.lineas.all()), [("C12", 1), ("SIX", 1)])
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(self.pedido.pk), accion="caja_corregida")
        self.assertEqual((evento.delta["pedido_sku"], evento.delta["real_sku"], evento.delta["piezas"], len(evento.delta["ajustes"])), ("SIX", "C12", 1, 2))
        self.assertIn("salió C12 en lugar de SIX", self.inc.mensajes.order_by("-pk").first().texto)

    def test_sin_ajustar_solo_corrige_la_caja(self):
        from apps.inventario.models import Ajuste
        from .. import services

        corregido, ajustes = services.corregir_contenido_caja(self.inc, self.caja, self.linea, 2, self.c12, self.mesa1, ajustar=False)
        self.assertEqual((ajustes, Ajuste.objects.count()), ([], 0))
        self.assertEqual((corregido.pk, corregido.cantidad, corregido.sku_real), (self.renglon.pk, 2, self.c12))  # entera: no se parte
        self.assertEqual((self._vendible(self.six), self._vendible(self.c12)), (3, 2))
        self.caja.refresh_from_db()
        self.assertTrue(self.caja.contenido_erroneo)
        self.assertIn("Sin ajuste de inventario", self.inc.mensajes.order_by("-pk").first().texto)

    def test_sin_existencias_de_lo_que_salio_no_cambia_nada(self):
        from apps.inventario.models import Ajuste
        from .. import services

        self.saldo_c12.delete()
        with self.assertRaisesMessage(ValueError, "negativo"):
            services.corregir_contenido_caja(self.inc, self.caja, self.linea, 1, self.c12, self.mesa1, ajustar=True, firmas=self.firmas)
        self.caja.refresh_from_db()
        self.renglon.refresh_from_db()
        self.assertFalse(self.caja.contenido_erroneo)
        self.assertEqual((self.renglon.cantidad, self.renglon.sku_real, self.caja.lineas.count(), Ajuste.objects.count()), (2, None, 1, 0))
        self.assertEqual(self._vendible(self.six), 3)

    def test_validaciones(self):
        from .. import services

        dan = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="x")
        with self.assertRaisesMessage(ValueError, "producto erróneo"):
            services.corregir_contenido_caja(dan, self.caja, self.linea, 1, self.c12, self.mesa1, ajustar=False)
        with self.assertRaisesMessage(ValueError, "viajaron 2"):
            services.corregir_contenido_caja(self.inc, self.caja, self.linea, 3, self.c12, self.mesa1, ajustar=False)
        with self.assertRaisesMessage(ValueError, "mismo producto"):
            services.corregir_contenido_caja(self.inc, self.caja, self.linea, 1, self.six, self.mesa1, ajustar=False)
        with self.assertRaisesMessage(ValueError, "dos firmas"):
            services.corregir_contenido_caja(self.inc, self.caja, self.linea, 1, self.c12, self.mesa1, ajustar=True)
        with self.assertRaisesMessage(ValueError, "dos personas distintas"):
            services.corregir_contenido_caja(self.inc, self.caja, self.linea, 1, self.c12, self.mesa1, ajustar=True, firmas=("mesa1", "3333", "mesa1", "3333"))
