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
from apps.catalogo.models import SKU
from apps.inventario.models import Movimiento, Saldo
from apps.pedidos.models import LineaPedido, Pedido

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


class ContenidoEnSalidaTests(PisoTestCase):
    """Salida muestra el contenido de cada pedido (todas las líneas, hijas de
    kit debajo de su kit) para resolver confusiones sin salir de la pantalla."""

    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:salida")

    def test_empacado_sin_guia_muestra_sus_lineas(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "ver contenido")
        self.assertContains(respuesta, "2 × Colimita six pack")
        self.assertContains(respuesta, "COLIMITA-SIX")

    def test_con_guia_generada_tambien(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=3))
        self.client.post(self.url, {"accion": "generar_guia", "pedido_id": pedido.pk})
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "3 × Colimita six pack")
        self.assertContains(respuesta, "3 piezas")

    def test_kit_muestra_sus_hijas_debajo(self):
        kit = SKU.objects.create(
            cliente=self.cliente, codigo="BOX3", descripcion="Mystery box", es_kit=True, peso_gr=350,
        )
        te = SKU.objects.create(cliente=self.cliente, codigo="TE-1", descripcion="Té verde", peso_gr=100)
        pedido = self.crear_pedido(cantidad=1, estado=Pedido.EMPACADO, reservar_stock=False)
        linea_kit = LineaPedido.objects.create(
            pedido=pedido, sku=kit, cantidad=1, cantidad_pickeada=1, reservada=True,
        )
        LineaPedido.objects.create(
            pedido=pedido, sku=te, cantidad=2, cantidad_pickeada=2, reservada=True,
            parte_de_kit=linea_kit, kit_caja=1,
        )
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "1 × Mystery box")
        self.assertContains(respuesta, "↳ 2 × Té verde")
        # Las hijas no se cuentan aparte: 1 six pack + 1 kit = 2 piezas.
        self.assertContains(respuesta, "2 piezas")


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


@override_settings(TORRE=TORRE_POOL_LEGADO)
class ManifiestoPorCajaTests(PisoTestCase):
    """Pedido empacado por caja: Salida palomea cada caja (paquete_id); solo
    la que tiene su foto de cierre sube; lo que sale deja el pedido
    PARCIALMENTE_DESPACHADO hasta que sale la última."""

    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:salida")

    def _pedido_dos_cajas(self):
        from decimal import Decimal

        from django.utils import timezone

        from apps.envios.models import Paquete, PaqueteLinea

        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4))
        linea = pedido.lineas.get()
        cajas = [
            Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal("2"), carrier="puntopost",
                estado=Paquete.EMPACADO,
            )
            for n in (1, 2)
        ]
        for caja in cajas:
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        self.client.post(self.url, {"accion": "generar_guia", "pedido_id": pedido.pk})
        Paquete.objects.filter(pk=cajas[0].pk).update(ts_cierre=timezone.now())
        pedido.refresh_from_db()
        return pedido, cajas[0], cajas[1]

    def test_salida_palomea_por_caja_y_solo_la_cerrada(self):
        pedido, c1, c2 = self._pedido_dos_cajas()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, f'name="paquete_id" value="{c1.pk}"')
        self.assertNotContains(respuesta, f'name="paquete_id" value="{c2.pk}"')
        self.assertNotContains(respuesta, f'name="pedido_id" value="{pedido.pk}"')
        self.assertContains(respuesta, "falta foto de cierre")

    def test_sale_una_caja_y_el_pedido_queda_parcial_hasta_la_ultima(self):
        from django.utils import timezone

        from apps.envios.models import Paquete

        pedido, c1, c2 = self._pedido_dos_cajas()
        respuesta = self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
            "paquete_id": [c1.pk, c2.pk],  # la 2 va palomeada pero no tiene cierre
        }, follow=True)
        self.assertContains(respuesta, "se queda en el corral")
        pedido.refresh_from_db()
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)
        self.assertEqual((c1.estado, c2.estado), (Paquete.DESPACHADO, Paquete.EMPACADO))
        # Sigue en Salida con su caja pendiente y marcado como salida parcial.
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, "salida parcial")
        self.assertNotContains(respuesta, f'name="paquete_id" value="{c1.pk}"')

        Paquete.objects.filter(pk=c2.pk).update(ts_cierre=timezone.now())
        self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
            "paquete_id": [c2.pk],
        }, follow=True)
        pedido.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
        self.assertEqual(c2.estado, Paquete.DESPACHADO)
        en_empaque = (
            Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_EMPAQUE)
            .aggregate(t=Sum("cantidad"))["t"] or 0
        )
        self.assertEqual(en_empaque, 0)
        self.assertNotContains(self.client.get(self.url), pedido.folio)

    def test_pedido_id_de_un_pedido_por_caja_sube_solo_las_cerradas(self):
        pedido, c1, c2 = self._pedido_dos_cajas()
        self.client.post(self.url, {
            "accion": "manifiesto", "corral": "SAL-OTRO", "carrier": "puntopost",
            "pedido_id": [pedido.pk],
        }, follow=True)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)
