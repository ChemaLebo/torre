"""Tests de la página pública de rastreo brandeada."""
from django.core.cache import cache
from django.test import TestCase

from apps.catalogo.models import SKU
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.pedidos.models import LineaPedido
from apps.rastreo.services import obtener_o_crear_token, url_publica


class BaseRastreo(TestCase):
    def setUp(self):
        cache.clear()  # el throttle por IP no debe filtrarse entre tests
        self.cliente = crear_cliente(branding={
            "nombre_publico": "Cervecería de Colima", "whatsapp_soporte": "5231211122",
        })
        self.tienda = crear_tienda(self.cliente)
        self.sku = SKU.objects.create(
            cliente=self.cliente, codigo="SIX", descripcion="Six Colimita", peso_gr=4000,
        )
        self.pedido = crear_pedido(
            self.cliente, self.tienda, cp="06600",
            comprador_nombre="Fernanda López", comprador_tel="+5215511122233",
        )
        LineaPedido.objects.create(pedido=self.pedido, sku=self.sku, cantidad=1)
        self.token = obtener_o_crear_token(self.pedido)


class TestPagina(BaseRastreo):
    def test_token_valido_muestra_la_marca_y_el_folio(self):
        r = self.client.get(f"/r/{self.token}/")
        self.assertEqual(r.status_code, 200)
        cuerpo = r.content.decode()
        self.assertIn("Cervecería de Colima", cuerpo)
        self.assertIn(self.pedido.folio, cuerpo)

    def test_token_invalido_es_404_generico(self):
        r = self.client.get("/r/AAAABBBBCCCC/")
        self.assertEqual(r.status_code, 404)

    def test_sin_datos_sensibles(self):
        r = self.client.get(f"/r/{self.token}/")
        cuerpo = r.content.decode()
        self.assertIn("Fernanda", cuerpo)              # nombre de pila sí
        self.assertNotIn("López", cuerpo)              # apellido no
        self.assertNotIn("5511122233", cuerpo)         # teléfono jamás
        self.assertNotIn("precio", cuerpo.lower())

    def test_token_estable_y_url_publica(self):
        self.assertEqual(self.token, obtener_o_crear_token(self.pedido))
        self.assertIn(f"/r/{self.token}/", url_publica(self.pedido))

    def test_embed_sin_header(self):
        r = self.client.get(f"/r/{self.token}/?embed=1")
        self.assertNotContains(r, "Seguimiento de tu pedido")

    def test_estados_en_lenguaje_humano(self):
        r = self.client.get(f"/r/{self.token}/")
        cuerpo = r.content.decode()
        self.assertNotIn("PENDIENTE", cuerpo)
        self.assertNotIn("EN_TRANSITO", cuerpo)


class TestReporte(BaseRastreo):
    def test_reporte_abre_incidencia_origen_comprador(self):
        from apps.incidencias.models import Incidencia

        r = self.client.post(f"/r/{self.token}/reporte/", {
            "tipo": "DAN", "texto": "Una botella llegó estrellada",
        })
        self.assertEqual(r.status_code, 302)
        incidencia = Incidencia.objects.get(pedido=self.pedido)
        self.assertEqual(incidencia.origen, "comprador")
        self.assertEqual(incidencia.tipo, "DAN")

    def test_limite_de_tres_reportes(self):
        from apps.incidencias.models import Incidencia

        for i in range(5):
            cache.clear()  # que el throttle no interfiera con el límite de negocio
            self.client.post(f"/r/{self.token}/reporte/", {"tipo": "RET", "texto": f"intento {i}"})
        self.assertEqual(
            Incidencia.objects.filter(pedido=self.pedido, origen="comprador").count(), 3
        )


class TestPodPublico(BaseRastreo):
    """El POD sale por /r/<token>/pod/ (el token es la credencial): MEDIA ya
    no se sirve directo ni en dev."""

    def _crear_pod(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.core.models import EvidenciaFoto

        return EvidenciaFoto.objects.create(
            entidad="entrega_local", entidad_id=str(self.pedido.pk), tipo="pod",
            archivo=SimpleUploadedFile("pod.jpg", b"poddemo"),
        )

    def test_pod_publico_solo_con_pedido_entregado(self):
        self._crear_pod()
        # Antes de entregar: 404 aunque la foto exista.
        self.assertEqual(self.client.get(f"/r/{self.token}/pod/").status_code, 404)
        cache.clear()
        self.pedido.estado = "ENTREGADO"
        self.pedido.save(update_fields=["estado"])
        respuesta = self.client.get(f"/r/{self.token}/pod/")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(b"".join(respuesta.streaming_content), b"poddemo")

    def test_pagina_entregada_apunta_al_pod_autorizado(self):
        self._crear_pod()
        self.pedido.estado = "ENTREGADO"
        self.pedido.save(update_fields=["estado"])
        r = self.client.get(f"/r/{self.token}/")
        cuerpo = r.content.decode()
        self.assertIn(f"/r/{self.token}/pod/", cuerpo)
        self.assertNotIn("/media/", cuerpo)


class TestBrandingPorCliente(BaseRastreo):
    """La página es 100% del cliente: lema, pie y link a su tienda salen de su
    branding; sin ellos, defaults neutros (el pie de Colima no se cuela en
    pedidos de otro cliente)."""

    def _pagina(self, **branding):
        self.cliente.branding = {**self.cliente.branding, **branding}
        self.cliente.save(update_fields=["branding"])
        return self.client.get(f"/r/{self.token}/").content.decode()

    def test_sin_pie_ni_tienda_el_footer_solo_lleva_el_nombre(self):
        cuerpo = self._pagina()
        self.assertNotIn("Hecho con cariño", cuerpo)
        self.assertIn("<footer>Cervecería de Colima</footer>", cuerpo)
        self.assertIn("Seguimiento de tu pedido", cuerpo)  # lema por default
        self.assertNotIn("Volver a la tienda", cuerpo)

    def test_pie_lema_y_tienda_del_cliente(self):
        cuerpo = self._pagina(pie="Hecho con cariño en Colima", lema="Tu cerveza, en camino", dominio_tienda="cerveceriadecolima.com")
        self.assertIn("Cervecería de Colima · Hecho con cariño en Colima", cuerpo)
        self.assertIn("Tu cerveza, en camino", cuerpo)
        self.assertNotIn("Seguimiento de tu pedido", cuerpo)
        self.assertIn('<a href="https://cerveceriadecolima.com">Volver a la tienda</a>', cuerpo)

    def test_dominio_con_esquema_se_respeta(self):
        cuerpo = self._pagina(dominio_tienda="http://tienda.local:8080")
        self.assertIn('href="http://tienda.local:8080"', cuerpo)
