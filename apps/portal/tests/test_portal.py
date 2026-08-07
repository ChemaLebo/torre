"""Tests del portal del cliente: multi-tenant duro, acciones del día 1 y exports.

Lo crítico aquí es el aislamiento: un usuario de portal JAMÁS ve, lista ni
exporta datos de otro cliente — ni siquiera adivinando IDs.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
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
