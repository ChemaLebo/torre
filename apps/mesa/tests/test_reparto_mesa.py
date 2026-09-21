"""Reparto de carriers por porcentajes desde Mesa: pesos en la ficha del
cliente (validación, bloque, base al cambiar) y el reporte mensual."""
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, EventoAuditoria
from apps.envios.models import Guia

from .test_clientes_gestion import datos_form_cliente
from .test_vistas import crear_pedido, crear_usuario


def datos_reparto(**pesos):
    datos = datos_form_cliente(nombre="Cervecería Colima", integracion_envios="reparto")
    for carrier, peso in pesos.items():
        datos[f"peso_{carrier}"] = peso
    return datos


# La lista blanca de producción cambia con el negocio (2026-09-20: solo imile);
# estas pruebas ejercitan el mecanismo con los carriers que conoce la tabla mock.
TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


@override_settings(TORRE=TORRE_CARRIERS_CLASICOS)
class FichaRepartoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.url = reverse("mesa:cliente_editar", args=[cls.colima.pk])

    def setUp(self):
        self.client.force_login(self.mesa)

    def test_guarda_pesos_con_bloque_y_auditoria(self):
        respuesta = self.client.post(self.url, datos_reparto(noventa9Minutos="75", estafeta="25"), follow=True)
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.integracion_envios, "reparto")
        self.assertEqual(self.colima.reparto_pesos, {"noventa9Minutos": 75, "estafeta": 25})
        self.assertContains(respuesta, "bloque de 4 cartas")
        self.assertContains(respuesta, "noventa9Minutos 75%")
        evento = EventoAuditoria.objects.get(entidad="cliente", entidad_id="colima", accion="edicion")
        self.assertEqual(evento.delta["reparto_pesos"], [{}, {"noventa9Minutos": 75, "estafeta": 25}])
        self.assertEqual(evento.delta["reparto_base"], [0, 0])

    def test_pesos_que_no_suman_100_no_se_guardan(self):
        respuesta = self.client.post(self.url, datos_reparto(noventa9Minutos="75", estafeta="5"))
        self.assertContains(respuesta, "deben sumar 100 (suman 80)")
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.integracion_envios, "envia")
        self.assertEqual(self.colima.reparto_pesos, {})
        respuesta = self.client.post(self.url, datos_reparto())
        self.assertContains(respuesta, "deben sumar 100 (suman 0)")

    def test_decimales_avisan_el_redondeo_al_tope(self):
        respuesta = self.client.post(self.url, datos_reparto(noventa9Minutos="92.25", estafeta="7.75"), follow=True)
        self.assertContains(respuesta, "bloque de 100 cartas")
        self.assertContains(respuesta, "noventa9Minutos 92.25 → 92")
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.reparto_pesos, {"noventa9Minutos": 92.25, "estafeta": 7.75})

    def test_cambiar_pesos_mueve_la_base_al_cursor(self):
        Cliente.objects.filter(pk=self.colima.pk).update(
            integracion_envios="reparto", reparto_pesos={"noventa9Minutos": 75, "estafeta": 25}, reparto_cursor=7,
        )
        self.client.post(self.url, datos_reparto(noventa9Minutos="50", estafeta="50"))
        self.colima.refresh_from_db()
        self.assertEqual((self.colima.reparto_base, self.colima.reparto_cursor), (7, 7))
        evento = EventoAuditoria.objects.get(entidad="cliente", accion="edicion")
        self.assertEqual(evento.delta["reparto_base"], [0, 7])
        # Guardar sin tocar los pesos no mueve la base ni registra cambio.
        self.client.post(self.url, datos_reparto(noventa9Minutos="50", estafeta="50"))
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.reparto_base, 7)
        self.assertEqual(EventoAuditoria.objects.filter(entidad="cliente", accion="edicion").count(), 1)

    def test_el_formulario_prefillea_los_pesos_y_los_conserva_al_cambiar_de_integracion(self):
        Cliente.objects.filter(pk=self.colima.pk).update(
            integracion_envios="reparto", reparto_pesos={"noventa9Minutos": 75, "estafeta": 25},
        )
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'name="peso_noventa9Minutos" value="75"')
        self.assertContains(respuesta, "Reparto por porcentajes")
        self.client.post(self.url, datos_form_cliente(nombre="Cervecería Colima", integracion_envios="envia"))
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.integracion_envios, "envia")
        self.assertEqual(self.colima.reparto_pesos, {"noventa9Minutos": 75, "estafeta": 25})

    def test_alta_de_cliente_en_reparto(self):
        datos = datos_reparto(noventa9Minutos="75", estafeta="25")
        datos.update(nombre="Ron Caney", slug="ron-caney")
        self.client.post(reverse("mesa:cliente_nuevo"), datos)
        nuevo = Cliente.objects.get(slug="ron-caney")
        self.assertEqual((nuevo.integracion_envios, nuevo.reparto_pesos), ("reparto", {"noventa9Minutos": 75, "estafeta": 25}))

    def test_la_ficha_muestra_el_reparto(self):
        Cliente.objects.filter(pk=self.colima.pk).update(
            integracion_envios="reparto", reparto_pesos={"noventa9Minutos": 75, "estafeta": 25}, reparto_cursor=5,
        )
        respuesta = self.client.get(reverse("mesa:cliente_detalle", args=[self.colima.pk]))
        self.assertContains(respuesta, "Reparto: estafeta 25% · noventa9Minutos 75%")
        self.assertContains(respuesta, "Bloque de 4 cartas · siguiente: carta 2 del bloque 1")


class ReporteRepartoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", integracion_envios="reparto",
            reparto_pesos={"noventa9Minutos": 75, "estafeta": 25}, reparto_cursor=3,
        )
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.piso = crear_usuario("piso1", "piso")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.url = reverse("mesa:reporte_reparto")

    def setUp(self):
        self.client.force_login(self.mesa)
        con_guia = crear_pedido(self.colima, reparto_carrier="noventa9Minutos")
        Guia.objects.create(pedido=con_guia, carrier="noventa9Minutos", numero="A", proveedor="mock")
        crear_pedido(self.colima, reparto_carrier="estafeta")

    def test_muestra_cartas_y_bloque_del_mes(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertContains(respuesta, "bloque de 4 cartas · siguiente: carta 4 del bloque 0")
        self.assertContains(respuesta, "2 con carta")
        self.assertContains(respuesta, 'class="nav-link activo" href="/mesa/reportes/reparto/"')
        respuesta = self.client.get(self.url, {"cliente": "colima", "mes": "2020-01"})
        self.assertContains(respuesta, "0 con carta")
        self.assertContains(respuesta, "Mes siguiente")
        respuesta = self.client.get(self.url, {"mes": "basura"})
        self.assertEqual(respuesta.status_code, 200)

    def test_portal_y_piso_no_entran(self):
        for usuario in (self.portal, self.piso):
            self.client.force_login(usuario)
            self.assertEqual(self.client.get(self.url).status_code, 403)
