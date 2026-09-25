"""Tests del portal del cliente: multi-tenant duro, acciones del día 1 y exports.

Lo crítico aquí es el aislamiento: un usuario de portal JAMÁS ve, lista ni
exporta datos de otro cliente — ni siquiera adivinando IDs.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.incidencias.models import Incidencia, MensajeIncidencia
from apps.incidencias.services import abrir_incidencia
from apps.integraciones.models import SyncLog, Tienda
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.pedidos.models import LineaPedido, Pedido


class BasePortal(TestCase):
    """Dos clientes, dos usuarios de portal: el escenario mínimo de aislamiento."""

    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.otro = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")

        User = get_user_model()
        cls.karina = User.objects.create_user("karina", password="colima2026", first_name="Karina")
        PerfilUsuario.objects.create(usuario=cls.karina, rol=PerfilUsuario.ROL_PORTAL, cliente=cls.colima)
        cls.ajeno = User.objects.create_user("nocturno", password="nocturno2026")
        PerfilUsuario.objects.create(usuario=cls.ajeno, rol=PerfilUsuario.ROL_PORTAL, cliente=cls.otro)

        cls.sku = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six pack",
            punto_reorden=10,
        )
        cls.sku_ajeno = SKU.objects.create(
            cliente=cls.otro, codigo="MEZCAL-750", descripcion="Mezcal joven 750 ml",
        )

        cls.pedido = Pedido.objects.create(
            cliente=cls.colima, comprador_nombre="Ana Comprador", cp="28017", es_local=True,
        )
        LineaPedido.objects.create(pedido=cls.pedido, sku=cls.sku, cantidad=2)
        cls.pedido_ajeno = Pedido.objects.create(
            cliente=cls.otro, comprador_nombre="Luis Ajeno", cp="06600",
        )

        cls.tienda = Tienda.objects.create(cliente=cls.colima, dominio="colima-mx.myshopify.com")
        SyncLog.objects.create(
            tienda=cls.tienda, direccion=SyncLog.DIRECCION_PUSH,
            resultado=SyncLog.RESULTADO_OK, detalle="push ok (mock)",
        )

        cls.incidencia = abrir_incidencia(
            cls.colima, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE,
            pedido=cls.pedido, texto="La caja llegó rota.",
        )
        cls.incidencia_ajena = abrir_incidencia(
            cls.otro, Incidencia.TIPO_RET, Incidencia.ORIGEN_CLIENTE,
            pedido=cls.pedido_ajeno, texto="Va tarde.",
        )

    def entrar(self):
        self.client.login(username="karina", password="colima2026")


class TestAcceso(BasePortal):
    def test_portal_exige_login(self):
        respuesta = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("entrar", respuesta["Location"])

    def test_rol_piso_no_entra_al_portal(self):
        User = get_user_model()
        piso = User.objects.create_user("piso1", password="piso2026")
        PerfilUsuario.objects.create(usuario=piso, rol=PerfilUsuario.ROL_PISO)
        self.client.login(username="piso1", password="piso2026")
        respuesta = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(respuesta.status_code, 403)

    def test_dashboard_carga_con_corte_y_frescura(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Corte")
        self.assertContains(respuesta, "hace")           # frescura de sync
        self.assertNotContains(respuesta, "tiempo real")  # prohibido en el portal


class TestSinInventario(BasePortal):
    """Fulfillment parcial: Karina ve por línea qué espera inventario (tag "Sin
    inventario") en la lista y en el detalle; una línea con reserva no lo trae."""

    def _pedido_parcial(self):
        pedido = Pedido.objects.create(cliente=self.colima, comprador_nombre="Ana", cp="28017")
        LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=2, reservada=True)
        agotado = SKU.objects.create(cliente=self.colima, codigo="AGOTADO-SIX", descripcion="Six agotado")
        LineaPedido.objects.create(pedido=pedido, sku=agotado, cantidad=3)  # sin reserva: faltante
        return pedido

    def test_lista_y_detalle_marcan_la_linea_sin_inventario(self):
        pedido = self._pedido_parcial()
        self.entrar()
        respuesta = self.client.get(reverse("portal:pedidos") + "?ver=todos")
        self.assertContains(respuesta, "Sin inventario · 3 pzas")
        respuesta = self.client.get(reverse("portal:pedido_detalle", args=[pedido.pk]))
        self.assertContains(respuesta, "sale después, con el mismo folio")
        self.assertEqual(respuesta.content.decode().count("Sin inventario"), 1)  # solo la línea agotada


class TestLinkShopify(BasePortal):
    """La sección de pedidos del portal enlaza la orden en el admin de Shopify,
    el mismo link que Mesa; un pedido manual no lo trae."""

    def _pedido_shopify(self):
        return Pedido.objects.create(
            cliente=self.colima, tienda=self.tienda, shopify_order_id="8396980125858",
            comprador_nombre="Enrique Lopez", cp="72830",
        )

    def test_lista_enlaza_la_orden_de_shopify(self):
        pedido = self._pedido_shopify()
        self.entrar()
        respuesta = self.client.get(reverse("portal:pedidos") + "?ver=todos")
        self.assertContains(respuesta, "https://colima-mx.myshopify.com/admin/orders/8396980125858")
        self.assertContains(respuesta, "#8396980125858 ↗")
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, self.pedido.folio)  # el manual sigue listado, sin link
        self.assertEqual(respuesta.content.decode().count("/admin/orders/"), 1)

    def test_detalle_enlaza_la_orden_y_el_manual_no(self):
        pedido = self._pedido_shopify()
        self.entrar()
        respuesta = self.client.get(reverse("portal:pedido_detalle", args=[pedido.pk]))
        self.assertContains(respuesta, "https://colima-mx.myshopify.com/admin/orders/8396980125858")
        self.assertContains(respuesta, "Orden #8396980125858 en Shopify")
        respuesta = self.client.get(reverse("portal:pedido_detalle", args=[self.pedido.pk]))
        self.assertNotContains(respuesta, "/admin/orders/")


class TestAislamientoTenant(BasePortal):
    def test_pedido_ajeno_es_404(self):
        self.entrar()
        respuesta = self.client.get(
            reverse("portal:pedido_detalle", args=[self.pedido_ajeno.pk])
        )
        self.assertEqual(respuesta.status_code, 404)

    def test_pedido_propio_carga(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:pedido_detalle", args=[self.pedido.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.pedido.folio)

    def test_incidencia_ajena_es_404(self):
        self.entrar()
        respuesta = self.client.get(
            reverse("portal:incidencia_detalle", args=[self.incidencia_ajena.pk])
        )
        self.assertEqual(respuesta.status_code, 404)

    def test_lista_pedidos_no_mezcla_clientes(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:pedidos"), {"ver": "todos"})
        self.assertContains(respuesta, self.pedido.folio)
        self.assertNotContains(respuesta, self.pedido_ajeno.folio)

    def test_canal_en_lista_filtro_y_dashboard(self):
        b2b = Pedido.objects.create(cliente=self.colima, comprador_nombre="Mayorista", cp="06600", canal=Pedido.CANAL_B2B)
        self.entrar()
        lista = self.client.get(reverse("portal:pedidos"), {"ver": "todos"})
        self.assertContains(lista, "B2B")
        self.assertContains(lista, "Tienda en línea")
        self.assertContains(lista, "canal=b2b")  # el filtro aparece porque hay más de un canal
        solo_b2b = self.client.get(reverse("portal:pedidos"), {"ver": "todos", "canal": "b2b"})
        self.assertContains(solo_b2b, b2b.folio)
        self.assertNotContains(solo_b2b, self.pedido.folio)
        inicio = self.client.get(reverse("portal:dashboard"))
        self.assertContains(inicio, "Por canal de venta")
        self.assertContains(inicio, "B2B")

    def test_inventario_solo_muestra_mis_skus(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:inventario"))
        self.assertContains(respuesta, "COLIMITA-SIX")
        self.assertNotContains(respuesta, "MEZCAL-750")

    def test_kardex_de_sku_ajeno_es_404(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:inventario"), {"sku": "MEZCAL-750"})
        self.assertEqual(respuesta.status_code, 404)


class TestAccionesIncidencia(BasePortal):
    def test_responder_agrega_mensaje_visible(self):
        self.entrar()
        url = reverse("portal:incidencia_detalle", args=[self.incidencia.pk])
        respuesta = self.client.post(url, {"accion": "responder", "texto": "¿Cómo va mi caso?"})
        self.assertRedirects(respuesta, url)
        mensaje = self.incidencia.mensajes.filter(interno=False).latest("ts")
        self.assertEqual(mensaje.texto, "¿Cómo va mi caso?")
        self.assertEqual(mensaje.rol_autor, MensajeIncidencia.ROL_CLIENTE)

    def test_responder_vacio_no_crea_mensaje(self):
        self.entrar()
        antes = self.incidencia.mensajes.count()
        url = reverse("portal:incidencia_detalle", args=[self.incidencia.pk])
        respuesta = self.client.post(url, {"accion": "responder", "texto": ""})
        self.assertEqual(respuesta.status_code, 200)  # re-render con error
        self.assertEqual(self.incidencia.mensajes.count(), antes)

    def test_marcar_urgente_sube_a_p1_y_audita(self):
        self.entrar()
        # DAN nace P1 por regla; bajamos a P2 para probar que el botón sube.
        Incidencia.objects.filter(pk=self.incidencia.pk).update(prioridad=Incidencia.P2)
        self.incidencia.refresh_from_db()
        self.assertNotEqual(self.incidencia.prioridad, Incidencia.P1)
        url = reverse("portal:incidencia_detalle", args=[self.incidencia.pk])
        self.client.post(url, {"accion": "urgente"})
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.prioridad, Incidencia.P1)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="incidencia",
                entidad_id=self.incidencia.folio,
                accion="marcar_urgente",
            ).exists()
        )
        # La acción consta en el timeline visible
        self.assertTrue(
            self.incidencia.mensajes.filter(interno=False, texto__icontains="urgente").exists()
        )

    def test_marcar_urgente_dos_veces_no_duplica(self):
        self.entrar()
        url = reverse("portal:incidencia_detalle", args=[self.incidencia.pk])
        self.client.post(url, {"accion": "urgente"})
        eventos = EventoAuditoria.objects.filter(
            entidad="incidencia", entidad_id=self.incidencia.folio, accion="marcar_urgente",
        ).count()
        self.client.post(url, {"accion": "urgente"})
        despues = EventoAuditoria.objects.filter(
            entidad="incidencia", entidad_id=self.incidencia.folio, accion="marcar_urgente",
        ).count()
        self.assertEqual(eventos, despues)

    def test_nueva_incidencia_queda_con_origen_cliente(self):
        self.entrar()
        respuesta = self.client.post(reverse("portal:incidencia_nueva"), {
            "pedido": str(self.pedido.pk),
            "tipo": Incidencia.TIPO_FAL,
            "descripcion": "Faltó una pieza en el pedido.",
        })
        nueva = Incidencia.objects.filter(cliente=self.colima).latest("ts_apertura")
        self.assertRedirects(
            respuesta, reverse("portal:incidencia_detalle", args=[nueva.pk])
        )
        self.assertEqual(nueva.origen, Incidencia.ORIGEN_CLIENTE)
        self.assertEqual(nueva.pedido_id, self.pedido.pk)
        self.assertTrue(nueva.folio.startswith("INC-"))
        self.assertIsNotNone(nueva.sla_respuesta_limite)

    def test_cambio_de_direccion_entra_por_el_portal_como_p1(self):
        """Chema 2026-09-23: el cliente avisa el cambio de dirección con una
        incidencia propia (CDR) que nace P1 para que Mesa cancele la guía a tiempo."""
        self.entrar()
        respuesta = self.client.post(reverse("portal:incidencia_nueva"), {
            "pedido": str(self.pedido.pk),
            "tipo": Incidencia.TIPO_CDR,
            "descripcion": "Ya corregí la dirección en Shopify: Av. Vallarta 500.",
        })
        nueva = Incidencia.objects.filter(cliente=self.colima).latest("ts_apertura")
        self.assertRedirects(respuesta, reverse("portal:incidencia_detalle", args=[nueva.pk]))
        self.assertEqual((nueva.tipo, nueva.prioridad), (Incidencia.TIPO_CDR, Incidencia.P1))

    def test_detalle_del_pedido_lleva_a_levantar_incidencia_con_el_pedido_puesto(self):
        """Chema 2026-09-23: el link de la orden de Shopify cae en el detalle del
        pedido; desde ahí se levanta la incidencia sin buscar el pedido."""
        self.entrar()
        url_nueva = reverse("portal:incidencia_nueva") + f"?pedido={self.pedido.pk}"
        self.assertContains(self.client.get(reverse("portal:pedido_detalle", args=[self.pedido.pk])), url_nueva)
        respuesta = self.client.get(url_nueva)
        self.assertContains(respuesta, f'<option value="{self.pedido.pk}" selected>')
        # Un pedido ajeno o basura no selecciona nada ni rompe la página.
        self.assertEqual(self.client.get(reverse("portal:incidencia_nueva") + "?pedido=abc").status_code, 200)

    def test_banner_de_retornado_solo_promete_incidencia_cuando_existe(self):
        """Chema 2026-09-23 (PED-00039): el banner decía "ya hay una incidencia" sin haberla."""
        limpio = Pedido.objects.create(
            cliente=self.colima, origen="manual", comprador_nombre="Sin incidencia", cp="28017",
            estado=Pedido.RETORNADO,
        )
        self.entrar()
        url = reverse("portal:pedido_detalle", args=[limpio.pk])
        respuesta = self.client.get(url)
        self.assertContains(respuesta, "El paquete regresó a la bodega.")
        self.assertNotContains(respuesta, "Ya hay una incidencia dándole seguimiento")
        abrir_incidencia(self.colima, "RET", "auto", pedido=limpio, texto="Regresó a bodega")
        self.assertContains(self.client.get(url), "Ya hay una incidencia dándole seguimiento")

    def test_direccion_pendiente_se_explica_en_el_detalle(self):
        self.pedido.direccion_pendiente = {"address1": "Av. Vallarta 500", "city": "Guadalajara", "zip": "44100"}
        self.pedido.save(update_fields=["direccion_pendiente"])
        self.entrar()
        respuesta = self.client.get(reverse("portal:pedido_detalle", args=[self.pedido.pk]))
        self.assertContains(respuesta, "Dirección nueva pendiente")
        self.assertContains(respuesta, "Av. Vallarta 500, Guadalajara, 44100")

    def test_expediente_con_guias_y_orden_de_shopify(self):
        """Chema 2026-09-23: el portal también muestra el expediente, con todas
        las guías (una por caja) y el botón a la orden de Shopify."""
        from decimal import Decimal

        from apps.envios.models import Guia, Paquete

        Pedido.objects.filter(pk=self.pedido.pk).update(tienda=self.tienda, shopify_order_id="8398059995298")
        for n in (1, 2):
            caja = Paquete.objects.create(pedido=self.pedido, numero=n, peso_kg=Decimal("2"), carrier="estafeta", estado=Paquete.EMPACADO)
            Guia.objects.create(pedido=self.pedido, paquete=caja, carrier="estafeta", numero=f"EST-{n}", proveedor="mock")
        self.entrar()
        html = self.client.get(reverse("portal:incidencia_detalle", args=[self.incidencia.pk])).content.decode()
        self.assertIn("Expediente", html)
        self.assertIn("Guía caja 1: estafeta", html)
        self.assertIn("EST-1", html)
        self.assertIn("Guía caja 2: estafeta", html)
        self.assertIn("EST-2", html)
        self.assertIn("Ver orden #8398059995298 en Shopify", html)
        self.assertIn("https://colima-mx.myshopify.com/admin/orders/8398059995298", html)

    def test_las_incidencias_internas_no_existen_para_el_portal(self):
        """Chema 2026-09-24: las de la bodega ("Sin paquetería que cotice") no
        se listan, no abren, no cuentan en el badge ni salen en el CSV."""
        limpio = Pedido.objects.create(cliente=self.colima, origen="manual", comprador_nombre="Sin incidencia", cp="28017")
        interna = abrir_incidencia(self.colima, Incidencia.TIPO_PAQ, Incidencia.ORIGEN_AUTO,
                                   pedido=limpio, texto="Nadie cotiza", interna=True)
        self.entrar()
        self.assertNotIn(interna.folio, self.client.get(reverse("portal:incidencias")).content.decode())
        self.assertEqual(self.client.get(reverse("portal:incidencia_detalle", args=[interna.pk])).status_code, 404)
        self.assertNotIn(interna.folio, self.client.get(reverse("portal:exportar"), {"csv": "incidencias"}).content.decode())
        detalle = self.client.get(reverse("portal:pedido_detalle", args=[limpio.pk])).content.decode()
        self.assertNotIn(interna.folio, detalle)
        self.assertNotIn("Con incidencia", detalle)  # la interna no marca incidencia_activa
        hoy = self.client.get(reverse("portal:dashboard")).content.decode()
        self.assertNotIn(interna.folio, hoy)
        # El badge del menú solo cuenta las públicas sin cerrar.
        publicas = Incidencia.objects.filter(cliente=self.colima, interna=False).exclude(estado=Incidencia.CERRADA).count()
        self.assertIn(f'Incidencias <span class="pill warn">{publicas}</span>', hoy)
        self.assertNotIn(f'Incidencias <span class="pill warn">{publicas + 1}</span>', hoy)

    def test_la_orden_de_shopify_se_muestra_por_su_nombre(self):
        """Chema 2026-09-25: en pantalla va el nombre ("#4074"); el link sigue con el id."""
        Pedido.objects.filter(pk=self.pedido.pk).update(tienda=self.tienda, shopify_order_id="8398059995298", shopify_order_name="#4074")
        self.entrar()
        detalle = self.client.get(reverse("portal:pedido_detalle", args=[self.pedido.pk])).content.decode()
        self.assertIn("Orden #4074 en Shopify", detalle)
        self.assertIn("/admin/orders/8398059995298", detalle)
        self.assertIn("#4074 ↗", self.client.get(reverse("portal:pedidos")).content.decode())
        self.assertIn("Ver orden #4074 en Shopify", self.client.get(reverse("portal:incidencia_detalle", args=[self.incidencia.pk])).content.decode())

    def test_nueva_incidencia_rechaza_pedido_ajeno(self):
        self.entrar()
        antes = Incidencia.objects.count()
        respuesta = self.client.post(reverse("portal:incidencia_nueva"), {
            "pedido": str(self.pedido_ajeno.pk),
            "tipo": Incidencia.TIPO_FAL,
            "descripcion": "Intento de cruzar tenants.",
        })
        self.assertEqual(respuesta.status_code, 200)  # re-render con error de form
        self.assertEqual(Incidencia.objects.count(), antes)


class TestRecepciones(BasePortal):
    def test_anunciar_entrega_crea_asn_con_lineas(self):
        self.entrar()
        fecha = timezone.localdate() + timedelta(days=3)
        respuesta = self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
            "sku_1": str(self.sku.pk),
            "cantidad_1": "48",
        })
        self.assertRedirects(respuesta, reverse("portal:recepciones"))
        orden = OrdenEntrada.objects.filter(cliente=self.colima).latest("creado")
        self.assertTrue(orden.folio.startswith("ASN-"))
        self.assertEqual(orden.estado, OrdenEntrada.ANUNCIADA)
        self.assertEqual(orden.fecha_compromiso, fecha)
        linea = orden.lineas.get()
        self.assertEqual(linea.sku_id, self.sku.pk)
        self.assertEqual(linea.cantidad_anunciada, 48)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="asn", entidad_id=orden.folio, accion="anunciada_portal",
            ).exists()
        )

    def test_anunciar_con_lote_lo_guarda_en_la_linea(self):
        self.entrar()
        fecha = timezone.localdate() + timedelta(days=3)
        self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
            "sku_1": str(self.sku.pk), "cantidad_1": "12", "lote_1": "L-PORTAL", "caducidad_1": "2027-04-01",
        })
        linea = OrdenEntrada.objects.filter(cliente=self.colima).latest("creado").lineas.get()
        self.assertEqual((linea.lote_codigo, linea.fecha_caducidad.isoformat()), ("L-PORTAL", "2027-04-01"))

    def test_formato_csv_prellena_los_productos_marcados(self):
        kit = SKU.objects.create(cliente=self.colima, codigo="KIT-3", descripcion="Kit", es_kit=True)
        self.entrar()
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, "Descargar formato (CSV)")
        # Mapa codigo → pk para volcar el CSV en los renglones desde el navegador.
        self.assertContains(respuesta, 'id="asn-codigos"')
        self.assertContains(respuesta, f'"{self.sku.codigo}": {self.sku.pk}')
        self.assertContains(respuesta, f'name="sku" value="{self.sku.pk}"')
        self.assertNotContains(respuesta, f'name="sku" value="{kit.pk}"')
        respuesta = self.client.get(reverse("portal:recepciones_plantilla"), {
            "sku": [str(self.sku.pk), str(self.sku_ajeno.pk), str(kit.pk)],
        })
        self.assertEqual(respuesta["Content-Type"], "text/csv; charset=utf-8")
        lineas = respuesta.content.decode("utf-8-sig").splitlines()
        self.assertEqual(lineas[0], "codigo,descripcion,cantidad,lote,caducidad")
        self.assertEqual(lineas[1:], [f"{self.sku.codigo},{self.sku.descripcion},,,AAAA-MM-DD"])
        # Sin marcar nada: solo el encabezado.
        vacio = self.client.get(reverse("portal:recepciones_plantilla")).content.decode("utf-8-sig").splitlines()
        self.assertEqual(vacio, ["codigo,descripcion,cantidad,lote,caducidad"])

    def test_formato_csv_relleno_se_sube_tal_cual(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.entrar()
        csv_texto = ("codigo,descripcion,cantidad,lote,caducidad\n"
                     f"{self.sku.codigo},{self.sku.descripcion},12,L-CSV,2027-05-01\n")
        fecha = timezone.localdate() + timedelta(days=3)
        respuesta = self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
            "renglones_csv": SimpleUploadedFile("asn.csv", csv_texto.encode("utf-8-sig"), content_type="text/csv"),
        })
        self.assertRedirects(respuesta, reverse("portal:recepciones"))
        linea = OrdenEntrada.objects.filter(cliente=self.colima).latest("creado").lineas.get()
        self.assertEqual((linea.cantidad_anunciada, linea.lote_codigo, linea.fecha_caducidad.isoformat()),
                         (12, "L-CSV", "2027-05-01"))

    def test_datalist_de_lotes_trae_la_caducidad(self):
        from apps.catalogo.models import Lote

        Lote.objects.create(sku=self.sku, codigo="L-PREVIO", fecha_caducidad=date(2027, 6, 1))
        self.entrar()
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, 'value="L-PREVIO" data-caducidad="2027-06-01"')

    def test_anunciar_sin_lineas_no_crea_nada(self):
        self.entrar()
        antes = OrdenEntrada.objects.count()
        fecha = timezone.localdate() + timedelta(days=3)
        respuesta = self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(OrdenEntrada.objects.count(), antes)

    def test_no_puede_anunciar_sku_ajeno(self):
        self.entrar()
        antes = OrdenEntrada.objects.count()
        fecha = timezone.localdate() + timedelta(days=3)
        respuesta = self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
            "sku_1": str(self.sku_ajeno.pk),
            "cantidad_1": "10",
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(OrdenEntrada.objects.count(), antes)

    def test_lista_muestra_mis_asn(self):
        orden = OrdenEntrada.objects.create(cliente=self.colima)
        LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=24)
        self.entrar()
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, orden.folio)

    @override_settings(MEDIA_ROOT="/tmp/torre-test-recepcion-portal")
    def test_acordeon_con_detalle_por_sku_fotos_e_incidencia(self):
        from django.core.files.base import ContentFile

        from apps.core.models import EvidenciaFoto

        orden = OrdenEntrada.objects.create(cliente=self.colima, estado=OrdenEntrada.CERRADA)
        LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=24, cantidad_recibida=20, cantidad_danada=2, lote_codigo="L-9")
        foto = EvidenciaFoto.objects.create(
            entidad="asn", entidad_id=orden.folio, tipo="llegada",
            archivo=ContentFile(b"\x89PNG", name="llegada.png"), tomada_por="piso1",
        )
        des = abrir_incidencia(self.colima, "DES", "auto", texto="Diferencias", orden=orden)
        ajena = OrdenEntrada.objects.create(cliente=self.otro)
        self.entrar()
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, 'class="colapsable recepcion-fila')
        self.assertContains(respuesta, "Evidencias de llegada")
        self.assertContains(respuesta, "L-9")
        self.assertContains(respuesta, ">-2<")  # diferencia: llegaron 22 de 24
        self.assertContains(respuesta, reverse("core:evidencia", args=[foto.pk]))
        self.assertContains(respuesta, reverse("portal:incidencia_detalle", args=[des.pk]))
        self.assertNotContains(respuesta, "(piso1)")  # el portal no ve quién tomó la foto
        self.assertNotContains(respuesta, ajena.folio)


@override_settings(MEDIA_ROOT="/tmp/torre-test-reporte-portal")
class TestReporteDia(BasePortal):
    def test_solo_mis_pedidos_sin_operador(self):
        from django.core.files.base import ContentFile

        from apps.core.models import EvidenciaFoto

        self.pedido.transicionar(Pedido.EN_PICKING, actor=self.karina)
        foto = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(self.pedido.pk), tipo="contenido",
            archivo=ContentFile(b"\x89PNG", name="c.png"), tomada_por="piso1",
        )
        self.entrar()
        respuesta = self.client.get(reverse("portal:reporte_dia"))
        self.assertContains(respuesta, self.pedido.folio)
        self.assertNotContains(respuesta, self.pedido_ajeno.folio)
        self.assertContains(respuesta, reverse("core:evidencia", args=[foto.pk]))
        self.assertNotContains(respuesta, "· karina")   # el portal no ve quién
        self.assertNotContains(respuesta, "(piso1)")
        self.assertNotContains(respuesta, "Toda la bodega")
        self.assertContains(respuesta, 'data-grupo="portal-reportes"')

    def test_csv_del_cliente(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:reporte_dia_csv"))
        lineas = respuesta.content.decode("utf-8-sig").splitlines()
        self.assertEqual(lineas[0].split(",")[0], "folio")
        self.assertTrue(any(self.pedido.folio in l for l in lineas[1:]))
        self.assertFalse(any(self.pedido_ajeno.folio in l for l in lineas[1:]))

    def test_anonimo_va_a_login(self):
        self.assertEqual(self.client.get(reverse("portal:reporte_dia")).status_code, 302)


class TestExportar(BasePortal):
    def _descargar(self, tipo, **extra):
        self.entrar()
        return self.client.get(reverse("portal:exportar"), {"csv": tipo, **extra})

    def test_pagina_exportar_carga(self):
        self.entrar()
        respuesta = self.client.get(reverse("portal:exportar"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Descargar pedidos")

    def test_csv_pedidos_solo_trae_lo_mio(self):
        respuesta = self._descargar("pedidos")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("text/csv", respuesta["Content-Type"])
        contenido = respuesta.content.decode("utf-8")
        self.assertIn(self.pedido.folio, contenido)
        self.assertNotIn(self.pedido_ajeno.folio, contenido)

    def test_csv_inventario_solo_trae_mis_skus(self):
        respuesta = self._descargar("inventario")
        contenido = respuesta.content.decode("utf-8")
        self.assertIn("COLIMITA-SIX", contenido)
        self.assertNotIn("MEZCAL-750", contenido)

    def test_csv_incidencias_solo_trae_lo_mio(self):
        respuesta = self._descargar("incidencias")
        contenido = respuesta.content.decode("utf-8")
        self.assertIn(self.incidencia.folio, contenido)
        self.assertNotIn(self.incidencia_ajena.folio, contenido)

    def test_csv_kardex_de_sku_ajeno_es_404(self):
        respuesta = self._descargar("kardex", sku="MEZCAL-750")
        self.assertEqual(respuesta.status_code, 404)

    def test_csv_kardex_propio_descarga(self):
        respuesta = self._descargar("kardex", sku="COLIMITA-SIX")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("text/csv", respuesta["Content-Type"])

    def test_reporte_inexistente_es_404(self):
        respuesta = self._descargar("nomina")
        self.assertEqual(respuesta.status_code, 404)

    def test_descarga_queda_auditada(self):
        self._descargar("pedidos")
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="export", entidad_id="pedidos", accion="descarga_csv",
                cliente=self.colima,
            ).exists()
        )


class TestAnuncioConTarimas(BasePortal):
    """B1: el anuncio del portal captura tarimas y la lista las muestra."""

    def test_anuncio_guarda_tarimas(self):
        self.entrar()
        fecha = timezone.localdate() + timedelta(days=3)
        respuesta = self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
            "tarimas": "4",
            "sku_1": str(self.sku.pk),
            "cantidad_1": "48",
        })
        self.assertRedirects(respuesta, reverse("portal:recepciones"))
        orden = OrdenEntrada.objects.filter(cliente=self.colima).latest("creado")
        self.assertEqual(orden.tarimas, 4)
        evento = EventoAuditoria.objects.get(
            entidad="asn", entidad_id=orden.folio, accion="anunciada_portal",
        )
        self.assertEqual(evento.delta["tarimas"], 4)

    def test_tarimas_vacias_quedan_en_cero(self):
        self.entrar()
        fecha = timezone.localdate() + timedelta(days=3)
        self.client.post(reverse("portal:recepciones"), {
            "fecha_compromiso": fecha.isoformat(),
            "sku_1": str(self.sku.pk),
            "cantidad_1": "12",
        })
        orden = OrdenEntrada.objects.filter(cliente=self.colima).latest("creado")
        self.assertEqual(orden.tarimas, 0)

    def test_lista_muestra_columna_de_tarimas(self):
        OrdenEntrada.objects.create(cliente=self.colima, tarimas=5)
        self.entrar()
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, "Tarimas")
        self.assertContains(respuesta, "¿Cuántas tarimas llegan?")
