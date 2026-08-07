"""Tests de la página pública del repartidor (QR v2 de la etiqueta: /r/e/<token>/)."""
from urllib.parse import urlencode

from django.core.cache import cache
from django.test import TestCase

from apps.core.models import EventoAuditoria
from apps.envios.models import Guia
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.mensajeria.models import NotificacionEnviada
from apps.rastreo.services import obtener_o_crear_token_etiqueta, url_publica_etiqueta

DIRECCION = {
    "address1": "Av. Prueba 123",
    "address2": "Depto 4B, portón negro",
    "city": "Guadalajara",
    "province": "Jalisco",
    "zip": "44100",
}


class BaseEtiquetaRepartidor(TestCase):
    def setUp(self):
        cache.clear()  # throttle y dedupe por cache no deben filtrarse entre tests
        self.cliente = crear_cliente(
            contacto_nombre="Karina",
            contacto_whatsapp="5213121112233",
            branding={"nombre_publico": "Cervecería de Colima"},
        )
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(
            self.cliente, self.tienda,
            comprador_nombre="Fernanda López", comprador_tel="+5215511122233",
            direccion=dict(DIRECCION), cp="44100",
        )
        self.guia = Guia.objects.create(
            pedido=self.pedido, carrier="paquetexpress", servicio="terrestre",
            numero="PQX-0001",
        )
        self.token = obtener_o_crear_token_etiqueta(self.guia)
        self.url = f"/r/e/{self.token}/"

    def eventos(self, accion):
        return EventoAuditoria.objects.filter(
            entidad="etiqueta", entidad_id=self.guia.numero, accion=accion,
        )


class TestPaginaRepartidor(BaseEtiquetaRepartidor):
    def test_get_muestra_branding_folio_y_paquete(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        cuerpo = r.content.decode()
        self.assertIn("Cervecería de Colima", cuerpo)
        self.assertIn(f"Entrega {self.pedido.folio}", cuerpo)
        self.assertIn("Paquete 1 de 1", cuerpo)

    def test_boton_maps_con_pin_cuando_hay_coordenadas(self):
        self.pedido.direccion = dict(DIRECCION, latitude=20.676722, longitude=-103.347222)
        self.pedido.save(update_fields=["direccion"])
        r = self.client.get(self.url)
        self.assertContains(r, 'href="https://maps.google.com/?q=20.676722,-103.347222"')

    def test_boton_maps_cae_a_la_direccion_urlencodeada_sin_coordenadas(self):
        r = self.client.get(self.url)
        esperado = urlencode(
            {"q": "Av. Prueba 123, Depto 4B, portón negro, Guadalajara, Jalisco, 44100"}
        )
        self.assertContains(r, f'href="https://maps.google.com/?{esperado}"')

    def test_coordenadas_nulas_de_shopify_no_cuentan_como_pin(self):
        # Shopify manda latitude/longitude: null cuando no pudo geolocalizar.
        self.pedido.direccion = dict(DIRECCION, latitude=None, longitude=None)
        self.pedido.save(update_fields=["direccion"])
        r = self.client.get(self.url)
        self.assertContains(r, "maps.google.com/?q=Av.+Prueba+123")

    def test_referencias_visibles(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Depto 4B, portón negro")

    def test_sin_datos_sensibles(self):
        r = self.client.get(self.url)
        cuerpo = r.content.decode()
        self.assertIn("Fernanda", cuerpo)         # nombre de pila sí
        self.assertNotIn("López", cuerpo)         # apellido no
        self.assertNotIn("5511122233", cuerpo)    # teléfono jamás
        self.assertNotIn("500.00", cuerpo)        # valor declarado jamás

    def test_paquete_n_de_m_con_plan_de_dos_bultos(self):
        from decimal import Decimal

        from apps.envios.models import Paquete

        paquete_2 = Paquete.objects.create(
            pedido=self.pedido, numero=2, peso_kg=Decimal("5.00"), carrier="paquetexpress",
        )
        Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("7.50"), carrier="paquetexpress",
        )
        self.guia.paquete = paquete_2
        self.guia.save(update_fields=["paquete"])
        self.assertContains(self.client.get(self.url), "Paquete 2 de 2")

    def test_token_invalido_es_404(self):
        self.assertEqual(self.client.get("/r/e/AAAABBBBCCCC/").status_code, 404)

    def test_token_estable_y_url_publica(self):
        self.assertEqual(self.token, obtener_o_crear_token_etiqueta(self.guia))
        self.assertIn(f"/r/e/{self.token}/", url_publica_etiqueta(self.guia))


class TestEscaneoDeduplicado(BaseEtiquetaRepartidor):
    def test_tres_recargas_seguidas_registran_un_solo_evento(self):
        for _ in range(3):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.eventos("qr_escaneado").count(), 1)
        evento = self.eventos("qr_escaneado").get()
        self.assertEqual(evento.cliente, self.cliente)
        self.assertEqual(evento.delta, {"folio": self.pedido.folio})


class TestThrottle(BaseEtiquetaRepartidor):
    def test_el_onceavo_request_del_minuto_es_404(self):
        for _ in range(10):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class TestNoEncontrado(BaseEtiquetaRepartidor):
    def post_no_encontrado(self):
        return self.client.post(self.url, {"accion": "no_encontrado"})

    def test_post_abre_incidencia_dir_notifica_y_audita(self):
        from apps.incidencias.models import Incidencia

        r = self.post_no_encontrado()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], f"/r/e/{self.token}/?avisado=1")

        incidencia = Incidencia.objects.get(pedido=self.pedido, tipo="DIR")
        self.assertEqual(incidencia.origen, "comprador")
        apertura = incidencia.mensajes.get()
        self.assertIn(
            "El repartidor de paquetexpress no encuentra el domicilio", apertura.texto
        )
        self.assertIn("PQX-0001", apertura.texto)
        self.assertIn("Depto 4B, portón negro", apertura.texto)

        notificacion = NotificacionEnviada.objects.get(plantilla_clave="DIR")
        self.assertEqual(notificacion.destinatario, "5213121112233")
        self.assertEqual(notificacion.referencia, self.pedido.folio)
        self.assertIn("🚨 paquetexpress", notificacion.cuerpo)
        self.assertIn(self.pedido.folio, notificacion.cuerpo)
        self.assertIn("Guadalajara, CP 44100", notificacion.cuerpo)
        self.assertIn("Depto 4B, portón negro", notificacion.cuerpo)
        self.assertIn(f"/portal/pedidos/{self.pedido.pk}/", notificacion.cuerpo)

        self.assertEqual(self.eventos("domicilio_no_encontrado").count(), 1)
        evento = self.eventos("domicilio_no_encontrado").get()
        self.assertEqual(
            evento.delta, {"folio": self.pedido.folio, "carrier": "paquetexpress"}
        )

    def test_segundo_post_agrega_al_timeline_sin_duplicar_nada(self):
        from apps.incidencias.models import Incidencia

        self.post_no_encontrado()
        self.post_no_encontrado()

        incidencia = Incidencia.objects.get(pedido=self.pedido, tipo="DIR")
        self.assertEqual(
            Incidencia.objects.filter(pedido=self.pedido, tipo="DIR").count(), 1
        )
        # Apertura + mensaje del segundo aviso en el MISMO timeline.
        self.assertEqual(incidencia.mensajes.count(), 2)
        segundo = incidencia.mensajes.order_by("ts").last()
        self.assertEqual(segundo.autor, "Repartidor")
        self.assertEqual(segundo.rol_autor, "sistema")
        # La notificación DIR es idempotente por hora: el doble tap no spamea.
        self.assertEqual(
            NotificacionEnviada.objects.filter(plantilla_clave="DIR").count(), 1
        )

    def test_pantalla_de_confirmacion_sin_datos_sensibles(self):
        self.post_no_encontrado()
        r = self.client.get(f"{self.url}?avisado=1")
        cuerpo = r.content.decode()
        self.assertIn("ya fue avisado", cuerpo)
        self.assertIn("va a contactar al comprador", cuerpo)
        self.assertNotIn("5511122233", cuerpo)
        self.assertNotIn("López", cuerpo)

    def test_throttle_de_avisos_maximo_3_por_token_por_hora(self):
        from apps.incidencias.models import Incidencia

        for _ in range(4):
            r = self.post_no_encontrado()
            self.assertEqual(r.status_code, 302)  # al repartidor siempre se le confirma

        incidencia = Incidencia.objects.get(pedido=self.pedido, tipo="DIR")
        # 3 avisos procesados (apertura + 2 al timeline); el 4º fue no-op.
        self.assertEqual(incidencia.mensajes.count(), 3)
        self.assertEqual(self.eventos("domicilio_no_encontrado").count(), 3)
        self.assertEqual(
            NotificacionEnviada.objects.filter(plantilla_clave="DIR").count(), 1
        )
