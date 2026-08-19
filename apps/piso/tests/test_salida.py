"""Salida en piso: staging por corral, generar guía y manifiesto → RECOLECTADO en lote.

Contrato del carril único: el manifiesto EXCLUYE y avisa pedidos sin foto de
caja cerrada (cajas_cerradas_completas), y sin flota propia
(TORRE["FLOTA_PROPIA"]=False) los pedidos es_local caen al corral de su
carrier real — solo las guías "local" viejas conservan SAL-LOCAL.
"""
from django.conf import settings
from django.db.models import Sum
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia
from apps.inventario.models import Movimiento, Saldo
from apps.pedidos.models import Pedido

from .base import PisoTestCase

TORRE_CON_FLOTA = {**settings.TORRE, "FLOTA_PROPIA": True}
# Pool pinneado: estas pruebas asumen que puntopost gana el lane local.
TORRE_POOL_LEGADO = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["puntopost", "estafeta", "paquetexpress", "fedex"],
}


@override_settings(TORRE=TORRE_POOL_LEGADO)
class SalidaPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:salida")

    def test_empacado_aparece_en_su_corral_sin_guia(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        respuesta = self.client.get(self.url)
        # El plan de envío elige el carrier más barato del lane (puntopost
        # en CDMX) → corral SAL-OTRO. SAL-PQX queda para guías paquetexpress.
        self.assertContains(respuesta, "SAL-OTRO")
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "Generar guía")

    def test_generar_guia_que_falta(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        respuesta = self.client.post(self.url, {
            "accion": "generar_guia", "pedido_id": pedido.pk,
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        guias = list(Guia.objects.filter(pedido=pedido))
        self.assertTrue(all(g.numero.startswith("MOCK-") for g in guias))
        # El carrier de cada guía es el que eligió el plan de división (el más barato).
        for g in guias:
            self.assertIsNotNone(g.paquete)
            self.assertEqual(g.carrier, g.paquete.carrier)

    def test_manifiesto_firmado_recolecta_en_lote_y_despacha(self):
        pedidos = [
            self.dejar_empacado(self.crear_pedido(cantidad=2)),
            self.dejar_empacado(self.crear_pedido(cantidad=1)),
        ]
        for pedido in pedidos:
            self.client.post(self.url, {"accion": "generar_guia", "pedido_id": pedido.pk})
            # Cierre por caja: sin la foto de la caja cerrada (etiqueta pegada)
            # el manifiesto los dejaría fuera.
            self.evidencia_cierre(pedido)

        corral = "SAL-OTRO"  # puntopost (el más barato del lane) vive aquí
        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": corral, "carrier": "puntopost",
            "pedido_id": [p.pk for p in pedidos],
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        for pedido in pedidos:
            pedido.refresh_from_db()
            self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
            self.assertIsNotNone(pedido.ts_recolectado)
            # El despacho quedó en el kardex con delta negativo.
            self.assertTrue(
                Movimiento.objects.filter(
                    sku=self.sku, tipo=Movimiento.SALIDA,
                    referencia=pedido.folio, delta__lt=0,
                ).exists()
            )
        en_empaque = (
            Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_EMPAQUE)
            .aggregate(t=Sum("cantidad"))["t"] or 0
        )
        self.assertEqual(en_empaque, 0)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="manifiesto", entidad_id=corral, accion="manifiesto_firmado",
            ).exists()
        )

    def test_manifiesto_excluye_pedido_sin_foto_de_cierre(self):
        con_cierre = self.dejar_empacado(self.crear_pedido(cantidad=1))
        sin_cierre = self.dejar_empacado(self.crear_pedido(cantidad=1))
        for pedido in (con_cierre, sin_cierre):
            self.client.post(self.url, {"accion": "generar_guia", "pedido_id": pedido.pk})
        self.evidencia_cierre(con_cierre)  # solo uno tiene su foto de cierre

        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
            "pedido_id": [con_cierre.pk, sin_cierre.pk],
        }, follow=True)
        self.assertContains(
            respuesta, f"{sin_cierre.folio} se queda: falta foto de caja cerrada"
        )

        con_cierre.refresh_from_db()
        sin_cierre.refresh_from_db()
        self.assertEqual(con_cierre.estado, Pedido.RECOLECTADO)
        # El pedido sin evidencia de cierre NO sube al manifiesto.
        self.assertEqual(sin_cierre.estado, Pedido.GUIA_GENERADA)

    def test_manifiesto_de_corral_vacio_avisa(self):
        corral = "SAL-OTRO"  # puntopost (el más barato del lane) vive aquí
        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": corral, "carrier": "puntopost",
            "pedido_id": ["99999"],
        }, follow=True)
        self.assertContains(respuesta, "Nada de puntopost listo")

    def test_manifiesto_corral_desconocido_avisa(self):
        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-FALSO",
        }, follow=True)
        self.assertContains(respuesta, "Corral desconocido")

    def test_manifiesto_sin_seleccion_avisa(self):
        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
        }, follow=True)
        self.assertContains(respuesta, "No palomeaste")

    def test_lo_no_palomeado_se_queda_para_la_siguiente_recoleccion(self):
        """Camión lleno / caja con detalle: el chofer firma SOLO lo palomeado."""
        se_va = self.dejar_empacado(self.crear_pedido(cantidad=1))
        se_queda = self.dejar_empacado(self.crear_pedido(cantidad=1))
        for pedido in (se_va, se_queda):
            self.client.post(self.url, {"accion": "generar_guia", "pedido_id": pedido.pk})
            self.evidencia_cierre(pedido)

        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
            "pedido_id": [se_va.pk],  # solo uno palomeado
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        se_va.refresh_from_db()
        se_queda.refresh_from_db()
        self.assertEqual(se_va.estado, Pedido.RECOLECTADO)
        self.assertEqual(se_queda.estado, Pedido.GUIA_GENERADA)  # sigue en el corral

    def test_el_manifiesto_de_un_carrier_jamas_se_lleva_los_de_otro(self):
        """SAL-OTRO junta carriers: firmarle a puntopost no toca lo de estafeta
        aunque venga palomeado (formulario viejo / doble submit)."""
        de_puntopost = self.dejar_empacado(self.crear_pedido(cantidad=1))
        self.client.post(self.url, {"accion": "generar_guia", "pedido_id": de_puntopost.pk})
        self.evidencia_cierre(de_puntopost)

        de_estafeta = self.crear_pedido(cantidad=1, estado=Pedido.GUIA_GENERADA)
        Guia.objects.create(
            pedido=de_estafeta, carrier="estafeta", servicio="ground",
            numero="EST-001", estado=Guia.GUIA_CREADA,
        )
        self.evidencia_cierre(de_estafeta)

        # La pantalla pinta un bloque de firma por carrier.
        pantalla = self.client.get(self.url)
        self.assertContains(pantalla, "PUNTOPOST")
        self.assertContains(pantalla, "ESTAFETA")
        self.assertContains(pantalla, "SALE LO PALOMEADO")

        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
            "pedido_id": [de_puntopost.pk, de_estafeta.pk],  # el ajeno viene colado
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        de_puntopost.refresh_from_db()
        de_estafeta.refresh_from_db()
        self.assertEqual(de_puntopost.estado, Pedido.RECOLECTADO)
        self.assertEqual(de_estafeta.estado, Pedido.GUIA_GENERADA)  # intacto


class SalidaCorralesFlotaTests(PisoTestCase):
    """Mapeo de corrales con TORRE["FLOTA_PROPIA"] (default False: sin flota)."""

    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:salida")

    def _grupos(self):
        respuesta = self.client.get(self.url)
        return {g["codigo"]: g for g in respuesta.context["corrales"]}

    def test_sin_flota_el_pedido_local_cae_al_corral_de_su_carrier_real(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1, es_local=True))
        grupos = self._grupos()
        # Sin flota propia no hay carril "local": el carrier real del cliente
        # (paquetexpress) manda el pedido a SAL-PQX, jamás a SAL-LOCAL.
        self.assertIn(pedido, grupos["SAL-PQX"]["sin_guia"])
        self.assertEqual(grupos["SAL-LOCAL"]["sin_guia"], [])
        self.assertEqual(grupos["SAL-LOCAL"]["listos"], [])

    @override_settings(TORRE=TORRE_CON_FLOTA)
    def test_con_flota_el_pedido_local_conserva_su_corral(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1, es_local=True))
        grupos = self._grupos()
        self.assertIn(pedido, grupos["SAL-LOCAL"]["sin_guia"])

    def test_guia_local_legacy_conserva_sal_local(self):
        # Datos viejos: una guía "local" ya emitida sigue mapeando a SAL-LOCAL
        # aunque ya no exista la flota (no se rompe historia).
        pedido = self.crear_pedido(cantidad=1, es_local=True, estado=Pedido.GUIA_GENERADA)
        Guia.objects.create(
            pedido=pedido, carrier="local", servicio="entrega_local",
            numero=f"LOCAL-{pedido.folio}", estado=Guia.GUIA_CREADA,
        )
        grupos = self._grupos()
        self.assertIn(pedido, grupos["SAL-LOCAL"]["listos"])


class SalidaOcultaSalLocalTests(PisoTestCase):
    """C2: sin flota propia, la card SAL-LOCAL vacía no se pinta en la UI."""

    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.url = reverse("piso:salida")

    def test_sin_flota_sal_local_vacio_se_oculta(self):
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, "SAL-LOCAL")
        self.assertNotContains(respuesta, "Entregas locales (POD)")

    @override_settings(TORRE=TORRE_CON_FLOTA)
    def test_con_flota_sal_local_es_visible(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "SAL-LOCAL")
        self.assertContains(respuesta, "Entregas locales (POD)")

    def test_guia_local_legacy_mantiene_la_card_visible_sin_flota(self):
        # El carril legado con contenido NO se esconde: esos paquetes existen
        # y el chofer los tiene que ver.
        self.crear_stock(cantidad=10)
        pedido = self.crear_pedido(cantidad=1, es_local=True, estado=Pedido.GUIA_GENERADA)
        Guia.objects.create(
            pedido=pedido, carrier="local", servicio="entrega_local",
            numero=f"LOCAL-{pedido.folio}", estado=Guia.GUIA_CREADA,
        )
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "SAL-LOCAL")
        self.assertContains(respuesta, pedido.folio)
