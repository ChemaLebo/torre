"""Mesa → Pedidos → detalle de un pedido (Chema 2026-09-30): toda la orden en
una página (cajas con guía, líneas, línea de tiempo, incidencias, fotos,
auditoría), Anterior / Siguiente con los filtros de la lista y las acciones
POR CAJA: cambiar paquetería (incluida la salida sin guía), cancelar guía y
reimprimir etiqueta."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class PedidoDetalleTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def pedido_con_dos_cajas_y_guias(self):
        """Empacado en dos cajas físicas con guía comprada (GUIA_GENERADA)."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        return pedido, c1, c2

    def test_muestra_todo_el_pedido_y_navega_con_los_filtros(self):
        pedido, c1, c2 = self.pedido_con_dos_cajas_y_guias()
        otro = self.crear_pedido(cantidad=1)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        respuesta = self.client.get(url, {"q": "PED-"})
        html = respuesta.content.decode()
        for esperado in (
            pedido.folio, "Ana Prueba", "Cajas", c1.guia_activa.numero, c2.guia_activa.numero,
            "Línea de tiempo por caja", "Incidencias", "Auditoría", "Fotos de evidencia",
            'value="cambiar_paqueteria_caja"', 'value="cancelar_guia_caja"', 'value="reimprimir_caja"',
            'value="cancelar_guias"', "Sin guía: entrega propia o la recoge el cliente",
        ):
            self.assertIn(esperado, html)
        self.assertIn('value="cambiar_paqueteria"', html)  # la del pedido entero replanea la ola en bodega
        # Anterior = el más reciente (otro), con el filtro conservado; Siguiente no hay.
        self.assertIn(f"{reverse('mesa:pedido_detalle', args=[otro.pk])}?q=PED-", html)
        self.assertIn(f"{reverse('mesa:pedidos')}?q=PED-", html)
        self.assertNotIn("Siguiente →", html)
        # Desde la lista se llega con el link del folio y el resumen de cajas.
        lista = self.client.get(reverse("mesa:pedidos")).content.decode()
        self.assertIn(f'href="{url}"', lista)
        self.assertIn("2 cajas · 2 guía creada", lista)

    def test_cambiar_paqueteria_de_una_caja_a_sin_guia_no_toca_la_otra(self):
        pedido, c1, c2 = self.pedido_con_dos_cajas_y_guias()
        vieja = c1.guia_activa
        intacta = c2.guia_activa
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        respuesta = self.client.post(url, {
            "accion": "cambiar_paqueteria_caja", "folio": pedido.folio, "caja": c1.pk, "carrier": "local",
        }, follow=True)
        self.assertContains(respuesta, "Caja 1")
        self.assertContains(respuesta, "guía cancelada, el piso la recompra")
        pedido.refresh_from_db()
        c1.refresh_from_db()
        c2.refresh_from_db()
        vieja.refresh_from_db()
        intacta.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.asignado_a), (Pedido.EMPACADO, None))
        self.assertEqual((c1.carrier, c1.carrier_forzado, c1.servicio), ("local", "local", "entrega_local"))
        self.assertEqual((vieja.estado, intacta.estado), (Guia.CANCELADA, Guia.GUIA_CREADA))
        self.assertEqual((c2.carrier, c2.carrier_forzado), ("estafeta", ""))
        self.assertEqual(pedido.carrier_forzado, "")  # el pedido no cambia, solo la caja
        self.assertTrue(EventoAuditoria.objects.filter(entidad="paquete", entidad_id=str(c1.pk), accion="cambio_paqueteria_caja").exists())
        # Al recomprar, la caja 1 sale con guía interna y la 2 conserva la suya.
        services.generar_guia(pedido)
        c1.refresh_from_db()
        self.assertTrue(c1.guia_activa.numero.startswith("LOCAL-"))
        self.assertEqual(c2.guia_activa.pk, intacta.pk)

    def test_cancelar_guia_de_una_caja_y_reimprimir(self):
        pedido, c1, c2 = self.pedido_con_dos_cajas_y_guias()
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="impresa") as imprimir:
            respuesta = self.client.post(url, {"accion": "reimprimir_caja", "folio": pedido.folio, "caja": c2.pk}, follow=True)
        self.assertEqual(imprimir.call_count, 2)  # carrier + interna
        self.assertContains(respuesta, "Caja 2")
        vieja = c1.guia_activa
        respuesta = self.client.post(url, {"accion": "cancelar_guia_caja", "folio": pedido.folio, "caja": c1.pk}, follow=True)
        self.assertContains(respuesta, f"Guía {vieja.numero} de la caja 1 cancelada")
        pedido.refresh_from_db()
        c1.refresh_from_db()
        vieja.refresh_from_db()
        self.assertEqual((pedido.estado, vieja.estado, c1.carrier_forzado), (Pedido.EMPACADO, Guia.CANCELADA, ""))
        self.assertEqual(c2.guia_activa.estado, Guia.GUIA_CREADA)

    def test_caja_que_ya_salio_no_tiene_acciones_y_la_accion_avisa(self):
        pedido, c1, c2 = self.pedido_con_dos_cajas_y_guias()
        Paquete.objects.filter(pk=c1.pk).update(estado=Paquete.DESPACHADO)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertEqual(html.count('value="cambiar_paqueteria_caja"'), 1)  # solo la caja 2
        respuesta = self.client.post(url, {
            "accion": "cambiar_paqueteria_caja", "folio": pedido.folio, "caja": c1.pk, "carrier": "local",
        }, follow=True)
        self.assertContains(respuesta, "ya salió")
        self.assertEqual(c1.guia_activa.estado, Guia.GUIA_CREADA)


@override_settings(ENVIA_API_KEY="")
class ReplanearYGuiaPerdidaTests(PisoTestCase):
    """PED-00031 (Chema 2026-09-30): iMile nunca registró dos cajas; se cancela
    su guía sin tocar la caja despachada, y la ola de reposición se replanea
    o cambia de paquetería completa desde el detalle."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_guia_de_caja_despachada_que_el_carrier_nunca_registro_se_cancela(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        services.generar_guia(pedido)
        Paquete.objects.filter(pk=caja.pk).update(estado=Paquete.DESPACHADO)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.RECOLECTADO)
        caja.refresh_from_db()
        guia = caja.guia_activa
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn('value="cancelar_guia_caja"', html)
        self.assertIn("nunca registró", html)
        respuesta = self.client.post(url, {"accion": "cancelar_guia_caja", "folio": pedido.folio, "caja": caja.pk}, follow=True)
        self.assertContains(respuesta, "la caja sigue como despachada")
        guia.refresh_from_db()
        caja.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((guia.estado, caja.estado, pedido.estado), (Guia.CANCELADA, Paquete.DESPACHADO, Pedido.RECOLECTADO))

    def test_replanear_cajas_y_cambiar_paqueteria_del_pedido_con_cajas(self):
        from apps.envios.cotizador import planificar_envio

        pedido = self.crear_pedido(cantidad=2)
        planificar_envio(pedido)
        viejas = {c.pk for c in pedido.paquetes.all()}
        self.assertTrue(viejas)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn('value="replanear_cajas"', html)
        self.assertIn('value="cambiar_paqueteria"', html)  # también con cajas: replanea la ola en bodega
        respuesta = self.client.post(url, {"accion": "replanear_cajas", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "replaneado")
        self.assertFalse(viejas & {c.pk for c in pedido.paquetes.all()})  # plan nuevo
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="replaneo_cajas").exists())
        # Una caja ya empacada frena el replaneo con mensaje.
        Paquete.objects.filter(pedido=pedido).update(estado=Paquete.EMPACADO)
        respuesta = self.client.post(url, {"accion": "replanear_cajas", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "ya está empacada")


@override_settings(ENVIA_API_KEY="")
class SinForzarTests(PisoTestCase):
    """Chema 2026-09-30: la opción "Sin forzar" quita la paquetería forzada del
    pedido (o de la caja) y replanea con las reglas normales."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_sin_forzar_quita_el_forzado_del_pedido_y_replanea(self):
        pedido = self.crear_pedido(cantidad=2, carrier_forzado="local")
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Sin forzar: reglas normales del pedido", html)
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "cambiar_paqueteria", "folio": pedido.folio, "carrier": ""}, follow=True)
        self.assertContains(respuesta, "reglas normales del pedido")
        pedido.refresh_from_db()
        self.assertEqual(pedido.carrier_forzado, "")
        self.assertTrue(pedido.paquetes.exists())
        self.assertNotIn("local", {c.carrier for c in pedido.paquetes.all()})

    def test_seguir_al_pedido_quita_el_forzado_de_la_caja(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="local", carrier_forzado="local", estado=Paquete.EMPACADO)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        self.assertIn("Seguir al pedido", self.client.get(url).content.decode())
        respuesta = self.client.post(url, {"accion": "cambiar_paqueteria_caja", "folio": pedido.folio, "caja": caja.pk, "carrier": ""}, follow=True)
        self.assertContains(respuesta, "las reglas del pedido")
        caja.refresh_from_db()
        self.assertEqual(caja.carrier_forzado, "")
        self.assertNotEqual(caja.carrier, "local")
