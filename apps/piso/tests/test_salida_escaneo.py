"""Registrar salida por escaneo (Chema 2026-09-22): la etiqueta interna de
cada caja se escanea (respaldo: número de guía o folio), la lista vive en la
sesión del operador, el resumen confirma solo lo escaneado y al confirmar
nace el manifiesto con folio, imprimible."""
from decimal import Decimal

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Manifiesto, Paquete, PaqueteLinea
from apps.pedidos.models import Pedido
from apps.rastreo.services import obtener_o_crear_token_etiqueta

from .base import PisoTestCase

# Pool pinneado: estas pruebas asumen que puntopost gana el lane (CP 44100).
TORRE_POOL_LEGADO = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["puntopost", "estafeta", "paquetexpress", "fedex"],
}
PARAMS = "?corral=SAL-OTRO&carrier=puntopost"


@override_settings(TORRE=TORRE_POOL_LEGADO)
class RegistrarSalidaTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:salida_registrar") + PARAMS

    def _con_guia(self, pedido):
        from apps.pedidos.services import generar_guia
        generar_guia(pedido)
        pedido.refresh_from_db()
        return pedido

    def _pedido_dos_cajas(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        cajas = [
            Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal("2"), carrier="puntopost", estado=Paquete.EMPACADO,
            )
            for n in (1, 2)
        ]
        for caja in cajas:
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        self._con_guia(pedido)
        Paquete.objects.filter(pk__in=[c.pk for c in cajas]).update(ts_cierre=timezone.now())
        pedido.refresh_from_db()
        return pedido, cajas[0], cajas[1]

    def _pedido_entero(self):
        """Empacado sin cajas propias (el plan nace con la guía): sale entero."""
        pedido = self._con_guia(self.dejar_empacado(self.crear_pedido(cantidad=1)))
        self.evidencia_cierre(pedido)
        return pedido

    def _escanear(self, codigo):
        return self.client.post(
            self.url,
            {"accion": "escanear", "corral": "SAL-OTRO", "carrier": "puntopost", "codigo": codigo},
            HTTP_ACCEPT="application/json",
        )

    def test_salida_ofrece_registrar_salida_por_carrier(self):
        self._pedido_dos_cajas()
        respuesta = self.client.get(reverse("piso:salida"))
        self.assertContains(respuesta, "Registrar salida")
        self.assertContains(respuesta, "salida/registrar/?corral=SAL-OTRO&amp;carrier=puntopost")

    def test_escanea_token_guia_y_folio_sin_duplicar(self):
        pedido, c1, c2 = self._pedido_dos_cajas()
        entero = self._pedido_entero()
        token = obtener_o_crear_token_etiqueta(c1.guia_activa)
        r = self._escanear(f"https://fulfillment.wop.partners/r/e/{token}/")  # el QR de la etiqueta interna
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["agregadas"], [f"{pedido.folio} · caja 1"])
        self.assertEqual((r.json()["escaneadas"], r.json()["listas"]), (1, 3))
        r = self._escanear(c2.guia_activa.numero)  # respaldo: número de guía del carrier
        self.assertEqual(r.json()["agregadas"], [f"{pedido.folio} · caja 2"])
        r = self._escanear(entero.folio.lower())  # respaldo: folio del pedido
        self.assertEqual(r.json()["agregadas"], [entero.folio])
        r = self._escanear(token)  # el Code128 trae el token pelón; ya estaba
        self.assertEqual(r.status_code, 400)
        self.assertIn("ya está en la lista", r.json()["error"])
        pantalla = self.client.get(self.url)
        self.assertContains(pantalla, 'escaneadas <b class="mono">3</b> de <b>3</b> listas')

    def test_rechaza_lo_que_no_es_de_esta_salida(self):
        ajeno = self.crear_pedido(cantidad=1, estado=Pedido.GUIA_GENERADA)
        Guia.objects.create(pedido=ajeno, carrier="estafeta", numero="EST-77", proveedor="mock")
        self.evidencia_cierre(ajeno)
        r = self._escanear("EST-77")
        self.assertEqual(r.status_code, 400)
        self.assertIn("viaja con estafeta", r.json()["error"])
        r = self._escanear("NADA-123")
        self.assertEqual(r.status_code, 400)
        self.assertIn("No reconozco", r.json()["error"])
        sin_cierre = self._con_guia(self.dejar_empacado(self.crear_pedido(cantidad=1)))
        r = self._escanear(sin_cierre.folio)
        self.assertEqual(r.status_code, 400)
        self.assertIn("foto de cierre", r.json()["error"])

    def test_cerrar_resumen_y_confirmar_crea_el_manifiesto(self):
        pedido, c1, c2 = self._pedido_dos_cajas()
        self._escanear(obtener_o_crear_token_etiqueta(c1.guia_activa))
        r = self.client.post(self.url, {"accion": "cerrar", "corral": "SAL-OTRO", "carrier": "puntopost"})
        self.assertRedirects(r, reverse("piso:salida_resumen") + PARAMS, fetch_redirect_response=False)
        resumen = self.client.get(reverse("piso:salida_resumen") + PARAMS)
        self.assertContains(resumen, f'name="paquete_id" value="{c1.pk}" checked')
        self.assertNotContains(resumen, f'value="{c2.pk}"')  # lo no escaneado no se puede palomear
        self.assertContains(resumen, "Se quedan en el corral")
        self.assertContains(resumen, "caja 2")
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse("piso:salida"), {
                "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
                "paquete_id": [c1.pk], "desde_escaner": "1", "chofer": "Juan Pérez",
            })
        hoja = Manifiesto.objects.get()
        self.assertRedirects(r, reverse("piso:manifiesto", args=[hoja.pk]), fetch_redirect_response=False)
        self.assertTrue(hoja.folio.startswith(f"MAN-{timezone.localtime().year}-"))
        self.assertEqual(
            (hoja.carrier, hoja.corral, hoja.chofer, hoja.operador),
            ("puntopost", "SAL-OTRO", "Juan Pérez", self.operador),
        )
        [linea] = hoja.lineas.all()
        self.assertEqual(
            (linea.pedido_id, linea.paquete_id, linea.caja, linea.numero_guia),
            (pedido.pk, c1.pk, 1, c1.guia_activa.numero),
        )
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)  # la caja 2 se quedó
        self.assertContains(self.client.get(self.url), 'escaneadas <b class="mono">0</b>')  # sesión limpia
        hoja_html = self.client.get(reverse("piso:manifiesto", args=[hoja.pk]))
        self.assertContains(hoja_html, hoja.folio)
        self.assertContains(hoja_html, "Juan Pérez")
        self.assertContains(hoja_html, c1.guia_activa.numero)
        self.assertContains(hoja_html, "PUNTOPOST")

    def test_el_orden_es_del_mas_antiguo_al_mas_reciente(self):
        viejo = self._pedido_entero()
        _pedido, c1, c2 = self._pedido_dos_cajas()
        pantalla = self.client.get(self.url)
        self.assertEqual(pantalla.context["siguiente"]["pedido"].pk, viejo.pk)
        self.assertContains(pantalla, "Siguiente en orden")
        self.assertEqual(
            [(u["pedido"].pk, u["id"]) for u in pantalla.context["se_quedan"]],
            [(viejo.pk, viejo.pk), (c1.pedido_id, c1.pk), (c2.pedido_id, c2.pk)],
        )
        self._escanear(viejo.folio)  # escaneado el viejo, el siguiente es la caja 1
        pantalla = self.client.get(self.url)
        self.assertEqual(pantalla.context["siguiente"]["id"], c1.pk)

    def test_sin_escanear_no_hay_resumen_y_descartar_limpia(self):
        r = self.client.get(reverse("piso:salida_resumen") + PARAMS)
        self.assertRedirects(r, self.url, fetch_redirect_response=False)
        _pedido, c1, _c2 = self._pedido_dos_cajas()
        self._escanear(obtener_o_crear_token_etiqueta(c1.guia_activa))
        r = self.client.post(self.url, {"accion": "cancelar", "corral": "SAL-OTRO", "carrier": "puntopost"})
        self.assertRedirects(r, reverse("piso:salida"), fetch_redirect_response=False)
        self.assertContains(self.client.get(self.url), 'escaneadas <b class="mono">0</b>')
        self.assertEqual(Manifiesto.objects.count(), 0)
