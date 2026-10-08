"""Quitar de salida (Chema 2026-10-05, PED-00319): una caja subió al
manifiesto y firmó, pero nunca se fue — Mesa canceló su guía y el carrier no
la recogió — y regresa a bodega para salir por otra paquetería o con la flota
propia. Aquí el servicio: la regla de cuándo se puede, el reverso del kardex
y del contador de lo despachado, la línea del manifiesto marcada, el estado
del pedido (parcial o de vuelta a empacado) y que la siguiente salida vuelve
a despachar sin duplicar nada."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, LineaManifiesto, Paquete, PaqueteLinea
from apps.envios.services import cancelar_guia, registrar_manifiesto
from apps.inventario.models import Movimiento, Saldo
from apps.pedidos import services
from apps.pedidos.linea_tiempo import construir
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


def suma(sku, estado):
    return Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"] or 0


@override_settings(ENVIA_API_KEY="")
class QuitarDeSalidaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def dos_cajas_fuera(self):
        """Pedido de 4 piezas en dos cajas con guía, cerradas, que subieron al
        mismo manifiesto (RECOLECTADO): el kardex ya despachó las 4."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        Paquete.objects.filter(pedido=pedido).update(ts_cierre=timezone.now())  # cierre con etiqueta pegada
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        hoja = registrar_manifiesto("estafeta", "SAL-OTRO", self.operador, [(pedido, [c1, c2])], chofer="Juan Chofer")
        pedido.refresh_from_db()
        c1.refresh_from_db()
        c2.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, c1.estado, c2.estado), (Pedido.RECOLECTADO, Paquete.DESPACHADO, Paquete.DESPACHADO))
        self.assertEqual((linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE)), (4, 0))
        return pedido, c1, c2, linea, hoja

    def test_solo_con_guia_cancelada_y_sin_recoleccion_del_carrier(self):
        pedido, c1, c2, linea, hoja = self.dos_cajas_fuera()
        g1 = c1.guia_activa
        # Con la guía viva no: primero se cancela (el carrier no la recogió).
        self.assertIn("guía viva", services.motivo_no_quitable_de_salida(pedido, c1))
        with self.assertRaisesMessage(ValueError, "cancela la guía primero"):
            services.quitar_de_salida(pedido, c1, self.mesa)
        cancelar_guia(g1, self.mesa, motivo="el carrier nunca la recogió")
        c1 = Paquete.objects.get(pk=c1.pk)
        self.assertEqual(services.motivo_no_quitable_de_salida(pedido, c1), "")
        # Si el carrier reportó la recolección, la caja sí se fue: no se quita.
        Guia.objects.filter(pk=g1.pk).update(ts_recolectado_carrier=pedido.ts_recolectado)
        c1 = Paquete.objects.get(pk=c1.pk)
        self.assertIn("El carrier reportó que recogió la caja 1", services.motivo_no_quitable_de_salida(pedido, c1))
        Guia.objects.filter(pk=g1.pk).update(ts_recolectado_carrier=None)
        # Una caja que no ha salido, o un pedido ya en tránsito, tampoco.
        c3 = Paquete.objects.create(pedido=pedido, numero=3, peso_kg=Decimal("1"), carrier="estafeta", estado=Paquete.EMPACADO)
        self.assertIn("no ha salido", services.motivo_no_quitable_de_salida(pedido, c3))
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EN_TRANSITO)
        pedido.refresh_from_db()
        c1 = Paquete.objects.get(pk=c1.pk)
        self.assertIn("en tránsito", services.motivo_no_quitable_de_salida(pedido, c1))

    def test_una_caja_regresa_y_las_demas_siguen_fuera(self):
        pedido, c1, c2, linea, hoja = self.dos_cajas_fuera()
        g1 = c1.guia_activa
        cancelar_guia(g1, self.mesa)
        c1 = Paquete.objects.get(pk=c1.pk)
        with patch("apps.integraciones.services.cancelar_fulfillment_caja") as shopify, self.captureOnCommitCallbacks(execute=True):
            services.quitar_de_salida(pedido, c1, self.mesa, motivo="sale con la flota propia")
        shopify.assert_not_called()  # fulfillment al llegar a Salida (2026-10-08): la caja lo conserva
        pedido.refresh_from_db()
        c1.refresh_from_db()
        c2.refresh_from_db()
        linea.refresh_from_db()
        # La caja vuelve a empacada con su cierre; el pedido queda parcial porque la 2 sigue fuera.
        self.assertEqual((c1.estado, c2.estado, pedido.estado), (Paquete.EMPACADO, Paquete.DESPACHADO, Pedido.PARCIALMENTE_DESPACHADO))
        self.assertIsNotNone(c1.ts_cierre)
        self.assertIsNotNone(pedido.ts_recolectado)
        # Kardex: sus 2 piezas regresan a en_empaque en el corral del manifiesto; el contador baja a lo que sigue fuera.
        self.assertEqual((linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE)), (2, 2))
        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.EN_EMPAQUE)
        self.assertEqual(saldo.ubicacion.codigo, "SAL-OTRO")
        mov = Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RETORNO).latest("ts")
        self.assertEqual((mov.delta, mov.estado_origen, mov.estado_destino, mov.referencia), (2, "despachado", "en_empaque", pedido.folio))
        # La hoja firmada se conserva; solo la línea de la caja 1 queda "no salió".
        l1 = LineaManifiesto.objects.get(manifiesto=hoja, paquete=c1)
        l2 = LineaManifiesto.objects.get(manifiesto=hoja, paquete=c2)
        self.assertTrue(l1.no_salio and l1.ts_no_salio is not None)
        self.assertFalse(l2.no_salio)
        # Línea de tiempo: la caja 1 ya no muestra salida; la 2 sí.
        filas = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=pedido.pk))}
        self.assertIsNone(filas[1]["manifiesto"])
        self.assertIsNone(filas[1]["ts"]["salida"])
        self.assertEqual(filas[2]["manifiesto"].folio, hoja.folio)
        # Auditoría con el manifiesto, el chofer y lo devuelto.
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="caja_quitada_de_salida")
        self.assertEqual(evento.delta["manifiestos"], [hoja.folio])
        self.assertEqual((evento.delta["chofer"], evento.delta["devuelto"], evento.delta["quedan_fuera"]), ("Juan Chofer", [[self.sku.codigo, 2]], [2]))
        self.assertEqual(evento.motivo, "sale con la flota propia")
        # Dos veces no: ya no está despachada.
        with self.assertRaisesMessage(ValueError, "no ha salido"):
            services.quitar_de_salida(pedido, c1, self.mesa)

    def test_la_ultima_caja_regresa_el_pedido_a_empacado_y_vuelve_a_salir_sin_duplicar(self):
        pedido, c1, c2, linea, hoja = self.dos_cajas_fuera()
        for caja in (c1, c2):
            cancelar_guia(caja.guia_activa, self.mesa)
        Pedido.objects.filter(pk=pedido.pk).update(asignado_a=self.operador)
        pedido.refresh_from_db()
        with patch("apps.integraciones.services.cancelar_fulfillment_caja"), self.captureOnCommitCallbacks(execute=True):
            services.quitar_de_salida(pedido, Paquete.objects.get(pk=c1.pk), self.mesa)
            services.quitar_de_salida(pedido, Paquete.objects.get(pk=c2.pk), self.mesa)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.asignado_a, pedido.ts_recolectado), (Pedido.EMPACADO, None, None))
        self.assertEqual((linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE)), (0, 4))
        self.assertFalse(LineaManifiesto.objects.filter(manifiesto=hoja, no_salio=False).exists())
        self.assertFalse(pedido.tiene_despachadas)
        # En el piso el pedido vuelve a "Completar empaquetado" y el wizard ofrece "Reintentar guía"
        # (no la pantalla de listo: las cajas tienen cierre pero no guía).
        self.login_piso()
        html = self.client.get(reverse("piso:home")).content.decode()
        self.assertIn("sin guía: caja 1, 2", html)
        html = self.client.get(reverse("piso:empaque_pedido", args=[pedido.pk])).content.decode()
        self.assertIn('value="generar_guia"', html)
        self.assertNotIn("listo</h1>", html)
        # Mesa cambia la paquetería de cada caja a entrega propia y el piso recompra la guía.
        for caja in pedido.paquetes.all():
            services.cambiar_paqueteria_caja(pedido, caja, "local", self.mesa)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        self.assertTrue(all(c.guia_activa.numero.startswith("LOCAL-") for c in pedido.paquetes.all()))
        # La segunda salida despacha las 4 piezas otra vez (ni más ni menos).
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE)), (Pedido.RECOLECTADO, 4, 0))
        self.assertIsNotNone(pedido.ts_recolectado)
        self.assertEqual(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.SALIDA).aggregate(t=Sum("delta"))["t"], -8)


@override_settings(ENVIA_API_KEY="")
class CasoMixtoTests(PisoTestCase):
    """Una caja regresa y la otra sigue fuera (PARCIALMENTE_DESPACHADO): el
    piso le recompra la guía SOLO a la que está en bodega (2026-10-05) y la
    caja vuelve a salir con su propio manifiesto."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def test_la_caja_de_vuelta_recompra_su_guia_y_vuelve_a_salir(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        Paquete.objects.filter(pedido=pedido).update(ts_cierre=timezone.now())
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        registrar_manifiesto("estafeta", "SAL-OTRO", self.operador, [(pedido, [c1, c2])], chofer="Juan")
        pedido.refresh_from_db()
        c1 = Paquete.objects.get(pk=c1.pk)
        c2 = Paquete.objects.get(pk=c2.pk)
        g2 = c2.guia_activa
        cancelar_guia(c1.guia_activa, self.mesa)
        c1 = Paquete.objects.get(pk=c1.pk)
        with patch("apps.integraciones.services.cancelar_fulfillment_caja"), self.captureOnCommitCallbacks(execute=True):
            services.quitar_de_salida(pedido, c1, self.mesa)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)
        services.cambiar_paqueteria_caja(pedido, Paquete.objects.get(pk=c1.pk), "local", self.mesa)
        # Sin guía de carrier no hay nada que comprar (2026-10-07): la guía interna de la caja 1
        # sale ahí mismo; Mi turno ya no la lista "sin guía" ni el piso tiene que reintentar.
        self.login_piso()
        self.assertNotIn("sin guía: caja 1", self.client.get(reverse("piso:home")).content.decode())
        pedido.refresh_from_db()
        c1 = Paquete.objects.get(pk=c1.pk)
        c2 = Paquete.objects.get(pk=c2.pk)
        # Solo la caja 1 compró guía (interna); la 2 conserva la suya y el pedido sigue parcial.
        self.assertTrue(c1.guia_activa.numero.startswith("LOCAL-"))
        self.assertEqual((c2.guia_activa.pk, pedido.estado), (g2.pk, Pedido.PARCIALMENTE_DESPACHADO))
        self.assertTrue(pedido.empaque_completo)
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 3)
        # Segunda salida: solo la caja 1; el kardex despacha sus 2 piezas y el pedido queda recolectado.
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador, paquetes=[c1])
        pedido.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, linea.cantidad_despachada, suma(self.sku, Saldo.EN_EMPAQUE)), (Pedido.RECOLECTADO, 4, 0))
