"""Tests de la página pública de rastreo brandeada."""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

CON_FORMULARIO = {**settings.TORRE, "RASTREO_REPORTE_FORMULARIO": True}

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


class TestWhatsApp(BaseRastreo):
    """Chema 2026-09-28: en la página solo va el WhatsApp del cliente con el
    mensaje prellenado; el formulario de reporte queda apagado por config."""

    def test_solo_whatsapp_con_mensaje_prellenado_y_sin_formulario(self):
        from apps.envios.models import Guia

        Guia.objects.create(pedido=self.pedido, carrier="estafeta", numero="EST-77", proveedor="mock")
        self.pedido.shopify_order_name = "#4074"
        self.pedido.save(update_fields=["shopify_order_name"])
        html = self.client.get(f"/r/{self.token}/").content.decode()
        self.assertIn("https://wa.me/5231211122?text=Hola%2C%20escribo%20por%20mi%20pedido%20%234074%20%28gu%C3%ADa%20EST-77%29.", html)
        self.assertIn("Escríbenos por WhatsApp", html)
        self.assertNotIn("Reportar un problema", html)
        self.assertNotIn(f"/r/{self.token}/reporte/", html)

    def test_sin_whatsapp_no_hay_bloque_y_el_reporte_no_abre_nada(self):
        from apps.incidencias.models import Incidencia

        self.cliente.branding["whatsapp_soporte"] = ""
        self.cliente.save(update_fields=["branding"])
        html = self.client.get(f"/r/{self.token}/").content.decode()
        self.assertNotIn("¿Algo no salió bien?", html)
        respuesta = self.client.post(f"/r/{self.token}/reporte/", {"tipo": "DAN", "texto": "x"})
        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(Incidencia.objects.exists())

    @override_settings(TORRE=CON_FORMULARIO)
    def test_con_el_flag_vuelven_el_formulario_y_el_whatsapp(self):
        html = self.client.get(f"/r/{self.token}/").content.decode()
        self.assertIn("Reportar un problema", html)
        self.assertIn("https://wa.me/5231211122?text=", html)


@override_settings(TORRE=CON_FORMULARIO)
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

        from apps.incidencias.models import MensajeIncidencia

        for i in range(5):
            cache.clear()  # que el throttle no interfiera con el límite de negocio
            self.client.post(f"/r/{self.token}/reporte/", {"tipo": "RET", "texto": f"intento {i}"})
        # Un solo caso por tipo y pedido (2026-09-28): los reportes se suman al
        # mismo folio, y el tope de 3 cuenta reportes, no folios.
        self.assertEqual(Incidencia.objects.filter(pedido=self.pedido, origen="comprador").count(), 1)
        reportes = MensajeIncidencia.objects.filter(
            incidencia__pedido=self.pedido, rol_autor="comprador", texto__startswith="[Reporte del comprador",
        )
        self.assertEqual(reportes.count(), 3)
        self.assertEqual(MensajeIncidencia.objects.filter(incidencia__pedido=self.pedido, texto="intento 3").count(), 0)


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


class TestPaquetes(BaseRastreo):
    """Chema 2026-10-08: estado, línea de tiempo y fechas POR PAQUETE; la guía
    es un link al rastreo del carrier; sin línea de tiempo del pedido."""

    def _caja(self, carrier="estafeta", estado="EMPACADO", numero=1):
        from decimal import Decimal

        from apps.envios.models import Paquete, PaqueteLinea

        caja = Paquete.objects.create(
            pedido=self.pedido, numero=numero, peso_kg=Decimal("4"), carrier=carrier, estado=estado,
        )
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=self.pedido.lineas.first(), cantidad=1)
        return caja

    def _guia(self, caja, carrier="estafeta", numero="EST-77", dias=3, **extra):
        from apps.envios.models import Guia

        return Guia.objects.create(
            pedido=self.pedido, paquete=caja, carrier=carrier, numero=numero, proveedor="mock",
            dias_promesa=dias, **extra,
        )

    def _salida(self, caja, guia, no_salio=False):
        from apps.envios.models import LineaManifiesto, Manifiesto

        hoja = Manifiesto.objects.create(carrier=guia.carrier)
        return LineaManifiesto.objects.create(
            manifiesto=hoja, pedido=self.pedido, paquete=caja, guia=guia, numero_guia=guia.numero,
            caja=caja.numero, no_salio=no_salio,
        )

    def _html(self):
        return self.client.get(f"/r/{self.token}/").content.decode()

    def test_listo_para_salir_con_link_y_promesa_en_dias(self):
        caja = self._caja()
        self._guia(caja)
        html = self._html()
        self.assertIn("Listo para salir", html)
        self.assertIn('href="https://cs.estafeta.com/es/Tracking/searchByGet?wayBill=EST-77"', html)
        self.assertIn("Rastrea tu guía", html)
        self.assertIn("Tu paquete está programado para llegar 3 días después de que salga, sin contar domingos.", html)
        self.assertNotIn("Llega a más tardar", html)
        self.assertNotIn('class="linea-tiempo"', html)
        self.assertIn("Empacado con cuidado", html)   # paso de la caja
        self.assertIn("Recolectado por la paquetería", html)

    def test_ya_salio_fecha_programada_y_paso_con_hora(self):
        from datetime import date

        caja = self._caja()
        guia = self._guia(caja, fecha_compromiso=date(2099, 10, 10))
        self._salida(caja, guia)
        html = self._html()
        self.assertIn("Salió de nuestra bodega", html)
        self.assertIn("Fecha programada de entrega: <b>10 de Octubre</b>", html)
        self.assertNotIn("después de que salga", html)

    def test_vencida_sin_entrega_dice_retraso(self):
        from datetime import date

        caja = self._caja()
        guia = self._guia(caja, fecha_compromiso=date(2020, 1, 1), estado="EN_TRANSITO")
        self._salida(caja, guia)
        html = self._html()
        self.assertIn("La paquetería va con retraso; ya estamos encima.", html)
        self.assertIn("En camino", html)

    def test_entregado_sin_fecha(self):
        from datetime import date

        caja = self._caja()
        guia = self._guia(caja, fecha_compromiso=date(2099, 10, 10), estado="ENTREGADO")
        self._salida(caja, guia)
        html = self._html()
        self.assertIn('class="chip entregado">Entregado<', html)
        self.assertNotIn("Fecha programada", html)
        self.assertNotIn("después de que salga", html)

    def test_entrega_local_sin_numero_ni_pasos_de_paqueteria(self):
        caja = self._caja(carrier="local")
        self._guia(caja, carrier="local", numero="LOCAL-0001", dias=1)
        html = self._html()
        self.assertIn("Entrega local", html)
        self.assertNotIn("LOCAL-0001", html)
        self.assertNotIn("Rastrea tu guía", html)
        self.assertIn("Tu paquete llega al día siguiente de que salga.", html)
        self.assertNotIn("Recolectado por la paquetería", html)

    def test_dos_cajas_cada_una_con_su_estado(self):
        from datetime import date

        caja1 = self._caja(numero=1)
        caja2 = self._caja(numero=2)
        guia1 = self._guia(caja1, numero="EST-1", fecha_compromiso=date(2099, 10, 10))
        self._guia(caja2, numero="EST-2")
        self._salida(caja1, guia1)
        html = self._html()
        self.assertIn("Paquete 1 de 2", html)
        self.assertIn("Paquete 2 de 2", html)
        self.assertIn("Salió de nuestra bodega", html)
        self.assertIn("Listo para salir", html)

    def test_quitada_de_salida_se_quedo_en_bodega(self):
        caja = self._caja()
        guia = self._guia(caja)
        self._salida(caja, guia, no_salio=True)
        html = self._html()
        self.assertIn("Se quedó en bodega; sale en la siguiente salida", html)

    def test_guia_cancelada_y_nueva_avisa_el_cambio(self):
        caja = self._caja()
        self._guia(caja, numero="EST-VIEJA", estado="CANCELADA")
        self._guia(caja, numero="EST-NUEVA")
        html = self._html()
        self.assertIn("EST-NUEVA", html)
        self.assertNotIn("EST-VIEJA", html)
        self.assertIn("Cambiamos la guía de este paquete", html)

    def test_detenido_se_dice_en_revision(self):
        self.pedido.estado = "EN_PICKING"
        self.pedido.detenido = True
        self.pedido.save(update_fields=["estado", "detenido"])
        self._caja(estado="PLANEADO")
        html = self._html()
        self.assertIn("Estamos revisando tu pedido antes de que salga", html)
        self.assertIn("Preparando tu pedido", html)
