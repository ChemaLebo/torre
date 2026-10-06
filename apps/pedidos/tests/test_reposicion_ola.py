"""La ola de reposición de punta a punta (Chema 2026-10-05): reponer 2 de
las 4 piezas de una línea entregada → el piso pickea solo 2 (tope = lo que
tiene stock), empaca la caja nueva, compra su guía, sale en su manifiesto (el
kardex despacha 2, ni más ni menos) y al entregarse el pedido vuelve a
ENTREGADO. Sin stock, la reposición espera inventario y arranca sola cuando
entra. Un orders/updated de Shopify ya no ve "piezas de más" (no hay líneas
de reposición), y la cancelación en bodega libera lo repuesto."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.inventario.models import Movimiento, Saldo
from apps.inventario.services import disponible
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


def suma(sku, estado):
    return Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"] or 0


@override_settings(ENVIA_API_KEY="")
class OlaDeReposicionTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=10)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def pedido_entregado_en_dos_cajas(self):
        """4 piezas en dos cajas de 2, con guía, manifiesto y entrega (RECOLECTADO → ENTREGADO)."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        cajas = []
        for n in (1, 2):
            caja = Paquete.objects.create(pedido=pedido, numero=n, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO, ts_cierre=timezone.now())
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
            cajas.append(caja)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        for caja in cajas:
            caja.refresh_from_db()
            caja.guia_activa.transicionar(Guia.ENTREGADO)
        pedido.refresh_from_db()
        pedido.transicionar(Pedido.ENTREGADO)
        linea.refresh_from_db()
        self.assertEqual((linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE), disponible(self.sku)), (4, 0, 6))
        return pedido, linea, cajas

    def test_reponer_dos_piezas_de_la_caja_dos_y_sacarlas(self):
        pedido, linea, (c1, c2) = self.pedido_entregado_en_dos_cajas()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 2, c2)], self.mesa)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.lineas.count()), (Pedido.PENDIENTE, 1))
        self.assertEqual((linea.cantidad, linea.cantidad_repuesta, linea.con_stock, linea.pendiente, linea.por_pickear), (4, 2, 6, 2, 2))
        self.assertEqual(disponible(self.sku), 4)  # las 2 repuestas quedaron apartadas
        nueva = Paquete.objects.get(pedido=pedido, estado=Paquete.PLANEADO)
        self.assertEqual([(pl.cantidad, pl.repone_a_id) for pl in nueva.lineas.all()], [(2, c2.pk)])
        self.assertEqual((nueva.cajas_que_repone, c2.cajas_de_reposicion, c1.cajas_de_reposicion), ([2], [nueva.numero], []))
        # Picking: el tope es 6 (4 pedidas + 2 repuestas); lleva 4, faltan 2; una pieza de más se rechaza.
        services.iniciar_picking(pedido, self.operador)
        with self.assertRaisesMessage(ValueError, "Te pasas"):
            services.confirmar_linea_pick(linea, 3, self.operador)
        services.confirmar_linea_pick(linea, 2, self.operador)
        linea.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((linea.cantidad_pickeada, pedido.lineas_completas), (6, True))
        # Empaque de la caja nueva (confirma el pick de las 2 en el kardex), guía, cierre, salida: despacha 2.
        services.empacar_caja(nueva, self.operador, 4000, self.foto())
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, suma(self.sku, Saldo.EN_EMPAQUE)), (Pedido.EMPACADO, 2))
        services.generar_guia(pedido)
        Paquete.objects.filter(pk=nueva.pk).update(ts_cierre=timezone.now())
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, Guia.objects.filter(pedido=pedido).count()), (Pedido.GUIA_GENERADA, 3))  # las 2 viejas siguen
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE)), (Pedido.RECOLECTADO, 6, 0))
        self.assertEqual(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.SALIDA).aggregate(t=Sum("delta"))["t"], -6)
        # Entregada la caja nueva, el pedido vuelve a ENTREGADO (las viejas ya estaban entregadas).
        from apps.envios.services import _estado_por_guias
        nueva.refresh_from_db()
        nueva.guia_activa.transicionar(Guia.ENTREGADO)
        self.assertEqual(_estado_por_guias(pedido), "ENTREGADO")
        self.assertFalse(pedido.pendiente_de_completar)

    def test_sin_stock_la_reposicion_espera_y_arranca_cuando_entra_inventario(self):
        pedido, linea, (c1, c2) = self.pedido_entregado_en_dos_cajas()
        # Sin stock: solo quedan 6 piezas... las usa otro pedido para dejar 0 disponibles.
        otro = self.crear_pedido(cantidad=6)
        self.assertEqual(disponible(self.sku), 0)
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 1, c1)], self.mesa)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, linea.cantidad_repuesta, linea.cantidad_repuesta_reservada), (Pedido.PENDIENTE, 1, 0))
        self.assertTrue(linea.faltante)
        self.assertEqual((linea.pendiente, linea.pendiente_sin_stock, pedido.tiene_faltantes), (0, 1, True))
        self.assertFalse(Paquete.objects.filter(pedido=pedido, estado=Paquete.PLANEADO).exists())  # nada que planear aún
        self.assertEqual(EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="reposicion").delta["sin_stock"], [self.sku.codigo])
        # Entra stock: la reserva se completa sola, con su caja planeada apuntando a la caja 1.
        with self.captureOnCommitCallbacks(execute=True):
            self.crear_stock(cantidad=5)
        linea.refresh_from_db()
        self.assertEqual((linea.cantidad_repuesta_reservada, linea.faltante, linea.pendiente), (1, False, 1))
        nueva = Paquete.objects.get(pedido=pedido, estado=Paquete.PLANEADO)
        self.assertEqual([(pl.cantidad, pl.repone_a_id) for pl in nueva.lineas.all()], [(1, c1.pk)])
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="reserva_reintentada").exists())
        del otro

    def test_un_orders_updated_de_shopify_ya_no_ve_piezas_de_mas(self):
        pedido, linea, (c1, c2) = self.pedido_entregado_en_dos_cajas()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 2, c2)], self.mesa)
        pedido.refresh_from_db()
        # Shopify sigue diciendo 4 piezas: la reposición no es una pieza de más.
        payload = {"line_items": [{"sku": self.sku.codigo, "quantity": 4, "current_quantity": 4}]}
        services._aplicar_cambios_cantidades(pedido, payload, "webhook")
        linea.refresh_from_db()
        self.assertEqual((linea.cantidad, linea.cantidad_repuesta, pedido.lineas.count()), (4, 2, 1))
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="edicion_orden").exists())
        self.assertFalse(pedido.incidencia_activa)

    def test_cancelar_en_bodega_libera_lo_repuesto(self):
        pedido, linea, (c1, c2) = self.pedido_entregado_en_dos_cajas()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 2, c2)], self.mesa)
        pedido.refresh_from_db()
        self.assertEqual(disponible(self.sku), 4)
        with self.captureOnCommitCallbacks(execute=True):
            services.cancelar(pedido, self.mesa, motivo="ya no la quiere")
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertEqual((linea.cantidad_repuesta, linea.cantidad_repuesta_reservada, disponible(self.sku)), (2, 0, 6))

    def test_el_piso_ve_la_reposicion_en_la_linea_y_el_json_del_escaneo_usa_el_tope_con_stock(self):
        pedido, linea, (c1, c2) = self.pedido_entregado_en_dos_cajas()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 2, c2)], self.mesa)
        services.iniciar_picking(Pedido.objects.get(pk=pedido.pk), self.operador)
        self.login_piso()
        html = self.client.get(reverse("piso:picking_pedido", args=[pedido.pk])).content.decode()
        self.assertIn("Reposición · 2 pzas", html)
        self.assertIn("4 de 6", html)
        self.assertIn('data-cantidad="6"', html)


@override_settings(ENVIA_API_KEY="")
class PantallasReposicionTests(PisoTestCase):
    """Portal, Mesa y la página pública de rastreo muestran solo los line items
    (con "repuestas: N") y en los paquetes "reposición del paquete N"."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=10)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def test_articulos_sin_renglones_extra_y_paquete_de_reposicion_senalado(self):
        from apps.rastreo.services import obtener_o_crear_token

        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        Guia.objects.create(pedido=pedido, paquete=c1, carrier="estafeta", numero="EST-1", estado=Guia.ENTREGADO, proveedor="mock")
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.ENTREGADO)
        pedido.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 1, c1)], self.mesa)
        # Portal: un solo artículo con "repuestas: 1"; el paquete 2 dice de qué paquete repone.
        self.client.force_login(self.usuario_portal)
        html = self.client.get(reverse("portal:pedido_detalle", args=[pedido.pk])).content.decode()
        self.assertIn("COLIMITA-SIX", html)
        self.assertIn("repuestas: 1", html)
        self.assertNotIn(">Reposición<", html)
        self.assertIn("reposición del paquete 1", html)
        lista = self.client.get(reverse("portal:pedidos"), {"ver": "todos"}).content.decode()
        self.assertIn(pedido.folio, lista)
        # Mesa: lo mismo en el detalle, con las piezas con stock.
        self.client.force_login(self.mesa)
        html = self.client.get(reverse("mesa:pedido_detalle", args=[pedido.pk])).content.decode()
        self.assertIn("repuestas: 1", html)
        self.assertIn("reposición de la caja 1", html)
        # Rastreo público: el contenido del paquete nuevo lleva la nota.
        self.client.logout()
        html = self.client.get(f"/r/{obtener_o_crear_token(pedido)}/").content.decode()
        self.assertIn("reposición del paquete 1", html)
