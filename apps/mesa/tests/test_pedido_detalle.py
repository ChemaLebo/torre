"""Mesa → Pedidos → detalle de un pedido (Chema 2026-09-30): toda la orden en
una página (cajas con guía, líneas, línea de tiempo, incidencias, fotos,
auditoría), Anterior / Siguiente con los filtros de la lista y las acciones
POR CAJA: cambiar paquetería (incluida la salida sin guía), cancelar guía,
reimprimir etiqueta y quitar de salida una caja que nunca se fue."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido
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
        self.assertIn('class="topbar fija"', html)  # folio y botones siguen visibles al hacer scroll
        self.assertNotIn("<th>Pedido</th>", html)   # en el detalle la columna Pedido de la línea de tiempo sobra
        # Anterior = el más reciente (otro), con el filtro conservado; Siguiente no hay.
        self.assertIn(f"{reverse('mesa:pedido_detalle', args=[otro.pk])}?q=PED-", html)
        self.assertIn(f"{reverse('mesa:pedidos')}?q=PED-", html)
        self.assertNotIn("Siguiente →", html)
        # Desde la lista se llega con el link del folio y el resumen de cajas.
        lista = self.client.get(reverse("mesa:pedidos")).content.decode()
        self.assertIn(f'href="{url}"', lista)
        self.assertIn("2 cajas · 2 guía creada", lista)

    def test_registrar_y_quitar_reembolso_de_paqueteria_por_guia(self):
        # Chema 2026-10-01: lo que la paquetería nos devuelve por una guía se
        # le descuenta al cliente (la tarifa que pagó) en el corte de la fecha.
        from apps.finanzas.models import ReembolsoGuia
        from apps.finanzas.services import tarifa_envio_de

        pedido, c1, c2 = self.pedido_con_dos_cajas_y_guias()
        g1 = c1.guia_activa
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Reembolsos de paquetería", html)
        self.assertIn('value="registrar_reembolso_guia"', html)
        self.assertIn(f'value="{tarifa_envio_de(g1):.2f}"', html)
        # Sin monto: la tarifa de la zona; sin fecha: hoy (este corte).
        respuesta = self.client.post(url, {"accion": "registrar_reembolso_guia", "folio": pedido.folio, "guia": g1.pk, "origen": "reclamacion", "nota": "pagó la reclamación"}, follow=True)
        self.assertContains(respuesta, f"Reembolso de ${tarifa_envio_de(g1)} de la guía {g1.numero} registrado")
        r = ReembolsoGuia.objects.get(guia=g1)
        self.assertEqual((r.monto, r.origen, r.nota, r.registrado_por, r.cliente), (tarifa_envio_de(g1), "reclamacion", "pagó la reclamación", "mesa1", pedido.cliente))
        self.assertContains(respuesta, 'value="quitar_reembolso_guia"')
        self.assertContains(respuesta, "pagó la reclamación")
        # Monto y fecha explícitos; una fecha anterior a la compra se rechaza.
        respuesta = self.client.post(url, {"accion": "registrar_reembolso_guia", "folio": pedido.folio, "guia": g1.pk, "monto": "50.50", "fecha": "2020-01-01"}, follow=True)
        self.assertContains(respuesta, "no puede ser anterior a la compra")
        respuesta = self.client.post(url, {"accion": "registrar_reembolso_guia", "folio": pedido.folio, "guia": g1.pk, "monto": "chorizo"}, follow=True)
        self.assertContains(respuesta, "no es un número válido")
        self.assertEqual(ReembolsoGuia.objects.filter(guia=g1).count(), 1)
        # Quitar: desaparece y queda auditado en la guía.
        respuesta = self.client.post(url, {"accion": "quitar_reembolso_guia", "folio": pedido.folio, "reembolso": r.pk}, follow=True)
        self.assertContains(respuesta, f"Reembolso de ${r.monto} de la guía {g1.numero} quitado")
        self.assertFalse(ReembolsoGuia.objects.filter(pk=r.pk).exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(g1.pk), accion="reembolso_quitado").exists())
        # Un reembolso de otro pedido no se puede quitar desde aquí.
        otro = self.crear_pedido(cantidad=1)
        respuesta = self.client.post(url, {"accion": "quitar_reembolso_guia", "folio": otro.folio, "reembolso": 999999})
        self.assertEqual(respuesta.status_code, 404)

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
        # La guía interna de la caja 1 sale ahí mismo (2026-10-07) y, como la 2 conserva la
        # suya, el pedido vuelve a GUIA_GENERADA sin dueño (el cierre borrado lo pide el wizard).
        self.assertEqual((pedido.estado, pedido.asignado_a), (Pedido.GUIA_GENERADA, None))
        self.assertEqual((c1.carrier, c1.carrier_forzado, c1.servicio), ("local", "local", "entrega_local"))
        self.assertEqual((vieja.estado, intacta.estado), (Guia.CANCELADA, Guia.GUIA_CREADA))
        self.assertEqual((c2.carrier, c2.carrier_forzado), ("estafeta", ""))
        self.assertEqual(pedido.carrier_forzado, "")  # el pedido no cambia, solo la caja
        self.assertTrue(EventoAuditoria.objects.filter(entidad="paquete", entidad_id=str(c1.pk), accion="cambio_paqueteria_caja").exists())
        self.assertTrue(c1.guia_activa.numero.startswith("LOCAL-"))
        self.assertEqual(c2.guia_activa.pk, intacta.pk)
        services.generar_guia(pedido)  # reintentar no duplica nada
        self.assertEqual(pedido.guias.filter(paquete=c1).exclude(estado=Guia.CANCELADA).count(), 1)

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
        # Con todo empacado y sin líneas fuera de caja no hay nada que replanear (2026-10-19).
        Paquete.objects.filter(pedido=pedido).update(estado=Paquete.EMPACADO)
        respuesta = self.client.post(url, {"accion": "replanear_cajas", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "no tiene nada que replanear")

    def test_replanear_planea_lo_que_no_tiene_caja_y_regresa_a_empaque(self):
        """PED-00606 (2026-10-19): caja 1 empacada y con guía; una línea que
        entró después del plan sin caja. Replanear crea la caja 2 sin tocar la
        caja 1 ni su guía y regresa el pedido a EMPACADO sin dueño; el piso lo
        retoma en el wizard con la caja 2 por pesar."""
        from apps.catalogo.models import SKU

        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        caja1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja1, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        Pedido.objects.filter(pk=pedido.pk).update(asignado_a=self.operador)
        caja1.refresh_from_db()
        guia = caja1.guia_activa
        otro = SKU.objects.create(
            cliente=self.cliente, codigo="OTRO-12", codigo_barras="7500000000012", descripcion="Otro doce",
            peso_gr=7000, requiere_lote=False, precio_declarado=Decimal("500.00"),
        )
        extra = LineaPedido.objects.create(pedido=pedido, sku=otro, cantidad=1, reservada=True, cantidad_pickeada=1)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        self.assertIn('value="replanear_cajas"', self.client.get(url).content.decode())
        respuesta = self.client.post(url, {"accion": "replanear_cajas", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "replaneado")
        self.assertContains(respuesta, "Regresó a empaquetado sin dueño")
        pedido.refresh_from_db()
        caja1.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.asignado_a), (Pedido.EMPACADO, None))
        self.assertEqual((caja1.estado, caja1.guia_activa.pk), (Paquete.EMPACADO, guia.pk))
        nuevas = list(pedido.paquetes.exclude(pk=caja1.pk))
        self.assertEqual([(c.numero, c.estado) for c in nuevas], [(2, Paquete.PLANEADO)])
        self.assertEqual([(pl.linea_pedido_id, pl.cantidad) for pl in nuevas[0].lineas.all()], [(extra.pk, 1)])
        # El piso lo retoma: el wizard pide la caja 2.
        self.client.force_login(self.operador)
        respuesta = self.client.get(reverse("piso:empaque_pedido", args=[pedido.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["paso"], "caja")
        self.assertEqual(respuesta.context["caja"].numero, 2)


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


@override_settings(ENVIA_API_KEY="")
class QuitarDeSalidaTests(PisoTestCase):
    """PED-00319 (Chema 2026-10-05): las cajas firmaron manifiesto pero el
    carrier nunca las recogió; Mesa canceló las guías y las regresa a bodega
    con el botón "Quitar de salida" por caja, para sacarlas con la flota propia."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def dos_cajas_fuera(self):
        from apps.envios.services import registrar_manifiesto

        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        registrar_manifiesto("estafeta", "SAL-OTRO", self.operador, [(pedido, [c1, c2])], chofer="Juan")
        pedido.refresh_from_db()
        return pedido, Paquete.objects.get(pk=c1.pk), Paquete.objects.get(pk=c2.pk)

    def test_el_boton_aparece_solo_con_la_guia_cancelada_y_regresa_la_caja(self):
        pedido, c1, c2 = self.dos_cajas_fuera()
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertNotIn('value="quitar_de_salida_caja"', html)  # guías vivas: primero se cancelan
        # Cancelar la guía de la caja 1 (despachada, el carrier nunca la registró) la vuelve quitable.
        respuesta = self.client.post(url, {"accion": "cancelar_guia_caja", "folio": pedido.folio, "caja": c1.pk}, follow=True)
        self.assertContains(respuesta, "la caja sigue como despachada")
        html = respuesta.content.decode()
        self.assertEqual(html.count('value="quitar_de_salida_caja"'), 1)
        self.assertIn("Quitar de salida", html)
        with patch("apps.integraciones.services.cancelar_fulfillment_caja") as shopify, self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "quitar_de_salida_caja", "folio": pedido.folio, "caja": c1.pk}, follow=True)
        shopify.assert_not_called()  # fulfillment al llegar a Salida (2026-10-08): la caja lo conserva
        self.assertContains(respuesta, "Caja 1 de " + pedido.folio + " quitada de salida")
        self.assertContains(respuesta, "parcialmente despachado")
        pedido.refresh_from_db()
        c1.refresh_from_db()
        self.assertEqual((pedido.estado, c1.estado), (Pedido.PARCIALMENTE_DESPACHADO, Paquete.EMPACADO))
        html = respuesta.content.decode()
        # De vuelta en bodega: la caja 1 ya tiene su selector de paquetería (incluida la entrega propia) y ya no el botón.
        self.assertEqual(html.count('value="cambiar_paqueteria_caja"'), 1)
        self.assertNotIn('value="quitar_de_salida_caja"', html)
        respuesta = self.client.post(url, {"accion": "cambiar_paqueteria_caja", "folio": pedido.folio, "caja": c1.pk, "carrier": "local"}, follow=True)
        self.assertContains(respuesta, "Caja 1")
        c1.refresh_from_db()
        self.assertEqual((c1.carrier, c1.carrier_forzado), ("local", "local"))
        # La caja 2 sigue fuera con su guía viva: nada que quitar.
        respuesta = self.client.post(url, {"accion": "quitar_de_salida_caja", "folio": pedido.folio, "caja": c2.pk}, follow=True)
        self.assertContains(respuesta, "cancela la guía primero")
        # Otra caja despachada con guía cancelada que el carrier sí recogió: la acción avisa.
        from apps.envios.models import Guia
        g2 = c2.guia_activa
        self.client.post(url, {"accion": "cancelar_guia_caja", "folio": pedido.folio, "caja": c2.pk})
        Guia.objects.filter(pk=g2.pk).update(ts_recolectado_carrier=timezone.now())
        html = self.client.get(url).content.decode()
        self.assertNotIn('value="quitar_de_salida_caja"', html)
        respuesta = self.client.post(url, {"accion": "quitar_de_salida_caja", "folio": pedido.folio, "caja": c2.pk}, follow=True)
        self.assertContains(respuesta, "El carrier reportó que recogió la caja 2")

    def test_la_hoja_del_manifiesto_conserva_la_caja_con_la_nota(self):
        from apps.envios.models import Manifiesto

        pedido, c1, c2 = self.dos_cajas_fuera()
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        self.client.post(url, {"accion": "cancelar_guia_caja", "folio": pedido.folio, "caja": c1.pk})
        with patch("apps.integraciones.services.cancelar_fulfillment_caja"), self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {"accion": "quitar_de_salida_caja", "folio": pedido.folio, "caja": c1.pk})
        hoja = Manifiesto.objects.get()
        html = self.client.get(reverse("piso:manifiesto", args=[hoja.pk])).content.decode()
        self.assertIn("NO SALIÓ", html)
        self.assertIn("<b>2</b> caja", html)  # la hoja firmada no cambia su conteo
        self.assertIn("<b>1</b> no salió", html)
        lista = self.client.get(reverse("mesa:manifiestos")).content.decode()
        self.assertIn("1 no salió", lista)


@override_settings(ENVIA_API_KEY="")
class CostoEntregaPropiaTests(PisoTestCase):
    """Chema 2026-10-06: sin flota, un pedido que sale "sin guía" no cuesta nada
    o cuesta una cantidad arbitraria que Mesa captura por pedido; es costo para
    finanzas, no cobro al cliente, y la guía interna nace con costo 0."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def test_captura_por_pedido_desde_el_detalle_y_la_guia_interna_no_cuesta(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        self.assertNotIn('value="costo_entrega_propia"', self.client.get(url).content.decode())  # sin entrega propia no aplica
        respuesta = self.client.post(url, {"accion": "cambiar_paqueteria_caja", "folio": pedido.folio, "caja": caja.pk, "carrier": "local"}, follow=True)
        self.assertContains(respuesta, "sin costo de carrier")  # ya no sale "$100.00"
        self.assertNotContains(respuesta, "$100.00")
        services.generar_guia(pedido)
        caja.refresh_from_db()
        self.assertEqual((caja.guia_activa.costo_cotizado, caja.guia_activa.costo_preferencial), (Decimal("0"), Decimal("0")))
        html = self.client.get(url).content.decode()
        self.assertIn('value="costo_entrega_propia"', html)
        respuesta = self.client.post(url, {"accion": "costo_entrega_propia", "folio": pedido.folio, "costo": "180"}, follow=True)
        self.assertContains(respuesta, "costo de entrega propia $180.00 (sin IVA), solo para finanzas")
        pedido.refresh_from_db()
        self.assertEqual(pedido.costo_entrega_propia, Decimal("180.00"))
        self.assertContains(respuesta, 'value="180.00"')
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="costo_entrega_propia").exists())
        respuesta = self.client.post(url, {"accion": "costo_entrega_propia", "folio": pedido.folio, "costo": "abc"}, follow=True)
        self.assertContains(respuesta, "no es un número válido")
        respuesta = self.client.post(url, {"accion": "costo_entrega_propia", "folio": pedido.folio, "costo": ""}, follow=True)
        self.assertContains(respuesta, "costo de entrega propia en $0")
        pedido.refresh_from_db()
        self.assertIsNone(pedido.costo_entrega_propia)


@override_settings(ENVIA_API_KEY="")
class SustituirGuiaDesdeMesaTests(PisoTestCase):
    """"Sustituir guía para esta caja" (Chema 2026-10-14): botón y modal solo
    en cajas despachadas con guía viva no entregada; la acción compra la guía
    nueva y la tarjeta Cajas muestra "sustituye a" / "sustituida por"."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def test_boton_modal_accion_y_pills(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        vieja = c1.guia_activa
        Paquete.objects.filter(pk=c1.pk).update(estado=Paquete.DESPACHADO)
        Guia.objects.filter(pk=vieja.pk).update(estado=Guia.INTENTO_FALLIDO)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.PARCIALMENTE_DESPACHADO)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn(f"modal-sustituir-{c1.pk}", html)       # la caja que ya salió
        self.assertNotIn(f"modal-sustituir-{c2.pk}", html)    # la que sigue en bodega no
        self.assertIn("Sustituir guía para la caja 1", html)
        self.assertIn("no regresa a picking ni a empaque", html)
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {
                "accion": "sustituir_guia_caja", "folio": pedido.folio, "caja": c1.pk,
                "carrier": "estafeta", "motivo": "Retorno falso: entrega con guía nueva",
            }, follow=True)
        c1.refresh_from_db()
        vieja.refresh_from_db()
        nueva = c1.guia_activa
        self.assertNotEqual(nueva.pk, vieja.pk)
        self.assertContains(respuesta, f"guía nueva {nueva.numero}")
        self.assertContains(respuesta, f"la guía {vieja.numero} queda sustituida")
        html = respuesta.content.decode()
        self.assertIn(f"sustituye a <span class=\"mono\">{vieja.numero}</span>", html)
        self.assertIn(f"{vieja.numero} ↗</a> · sustituida por otra guía · por {nueva.numero}", html)
        self.assertEqual(vieja.estado, Guia.SUSTITUIDA)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)
        # Sin motivo: error claro y nada cambia.
        respuesta = self.client.post(url, {"accion": "sustituir_guia_caja", "folio": pedido.folio, "caja": c1.pk, "carrier": "estafeta", "motivo": " "}, follow=True)
        self.assertContains(respuesta, "Escribe el motivo")
        self.assertEqual(Guia.objects.filter(paquete=c1).count(), 2)


class DesasignarOperadorTests(PisoTestCase):
    """Chema 2026-10-15: Mesa quita el pedido de las manos de un operador que
    no vino; vuelve a la cola de Mi turno con su avance. Sin "Reasignar a…"."""

    def setUp(self):
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)
        self.pedido = self.crear_pedido(cantidad=2)
        services.iniciar_picking(self.pedido, self.operador)
        services.confirmar_linea_pick(self.pedido.lineas.get(), 1, self.operador)
        self.url = reverse("mesa:pedido_detalle", args=[self.pedido.pk])

    def _desasignar(self):
        return self.client.post(self.url, {"accion": "desasignar_operador", "folio": self.pedido.folio}, follow=True)

    def test_boton_y_desasignar_conserva_avance(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'value="desasignar_operador"')
        self.assertContains(r, "Desasignar operador")
        r = self._desasignar()
        self.assertContains(r, f"piso1 ya no tiene {self.pedido.folio}; vuelve a la cola de Mi turno.")
        self.pedido.refresh_from_db()
        self.assertIsNone(self.pedido.asignado_a)
        self.assertIsNone(self.pedido.transferencia_a)
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 1)
        evento = EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk), accion="pedido_soltado").latest("ts")
        self.assertEqual((evento.delta["de"], evento.delta["por"]), ("piso1", "mesa"))
        self.assertNotContains(r, 'value="desasignar_operador"')
        r = self._desasignar()
        self.assertContains(r, "no tiene operador asignado")

    def test_fuera_del_piso_no_se_puede(self):
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.ENTREGADO)
        r = self._desasignar()
        self.assertContains(r, "ya no está en el piso")
        self.assertNotContains(r, 'value="desasignar_operador"')

    def test_piso_no_puede(self):
        self.client.force_login(self.operador)
        r = self.client.post(self.url, {"accion": "desasignar_operador", "folio": self.pedido.folio})
        self.assertEqual(r.status_code, 403)

