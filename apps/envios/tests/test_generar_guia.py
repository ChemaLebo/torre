"""generar_guia: idempotencia, entrega local y reexpedición tras retorno."""
from decimal import Decimal

from django.conf import settings
from django.test import TestCase, override_settings

from apps.core.models import EventoAuditoria
from apps.envios import services
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia

from .base import crear_cliente, crear_pedido, crear_tienda


class DestinoEnviaTests(TestCase):
    """El state del destino viaja en los códigos de 2 letras de envia (su FAQ:
    no reusar los de Shopify). Entra el province_code de Shopify (o CP_ESTADO
    para manuales) y estado_envia lo traduce; lo desconocido pasa derecho."""

    def _pedido(self, cp, province_code):
        from types import SimpleNamespace
        direccion = {"address1": "Calle 1", "city": "X", "zip": cp}
        if province_code is not None:
            direccion["province_code"] = province_code
        return SimpleNamespace(
            cp=cp, direccion=direccion,
            comprador_nombre="Prueba", comprador_tel="", comprador_email="",
        )

    def test_province_code_de_shopify_se_traduce_a_2_letras(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("28048", "COL"))
        self.assertEqual(destino["state"], "CL")

    def test_pedido_manual_sin_province_code_deriva_del_cp(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("28048", None))
        self.assertEqual(destino["state"], "CL")

    def test_cdmx_es_cx(self):
        from apps.envios.adapters import EnviaAdapter
        # DF (code_shopify) daba 1129 con estafeta: PED-00019/00020.
        destino = EnviaAdapter._destino(self._pedido("01780", "DF"))
        self.assertEqual(destino["state"], "CX")

    def test_chihuahua_cabe_en_el_esquema(self):
        from apps.envios.adapters import EnviaAdapter
        # CHIH (4 letras) daba "String is too long": PED-00021.
        destino = EnviaAdapter._destino(self._pedido("31416", "CHIH"))
        self.assertEqual(destino["state"], "CH")

    def test_valor_desconocido_pasa_derecho(self):
        from apps.envios.adapters import EnviaAdapter
        # Dirección corregida a mano ya en 2 letras: no se toca.
        destino = EnviaAdapter._destino(self._pedido("97000", "YU"))
        self.assertEqual(destino["state"], "YU")

    def test_diccionario_cubre_los_32_estados(self):
        from apps.envios.cotizador import ESTADO_ENVIA, ESTADOS_MX, estado_envia
        self.assertEqual(len(ESTADOS_MX), 32)
        self.assertTrue(all(len(v) == 2 for v in ESTADO_ENVIA.values()))
        self.assertEqual(estado_envia("Q ROO"), "QR")
        self.assertEqual(estado_envia("TAMPS"), "TM")
        self.assertEqual(estado_envia(""), "")

    def test_cotizacion_manda_el_destino_en_2_letras(self):
        from unittest.mock import MagicMock, patch

        from apps.envios.adapters import EnviaAdapter
        respuesta = MagicMock()
        respuesta.json.return_value = {"meta": "rate", "data": []}
        with patch("apps.envios.adapters.requests.post", return_value=respuesta) as post:
            EnviaAdapter().cotizar_lane("fedex", "31416", 1.5)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["destination"]["state"], "CH")
        self.assertEqual(payload["origin"]["state"], "CX")


class OrigenEnviaTests(TestCase):
    """El state del ORIGEN va en el code_2_digits de envia ("CX") para todo
    carrier — el override solo-estafeta de agosto era el síntoma (PED-00015)."""

    def test_estafeta_manda_cx(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._origen("estafeta")["state"], "CX")

    def test_fedex_tambien_cx(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._origen("fedex")["state"], "CX")

    def test_sin_carrier_es_cx(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._origen()["state"], "CX")


class ContenidoPorCarrierTests(TestCase):
    """El `content` del bulto respeta el tope del conector: estafeta rechaza
    más de 25 caracteres (PED-00018); los demás conservan la descripción
    completa. El corte cae en límite de palabra y jamás viaja vacío."""

    TEXTO = "2x Black Tea Punch: Tisana Ponche Navideño, 2x Tropical Bloom: Tisana de Melocotón"

    def test_estafeta_corta_en_limite_de_palabra(self):
        from apps.envios.adapters import EnviaAdapter
        corte = EnviaAdapter._recortar_contenido(self.TEXTO, "estafeta")
        self.assertLessEqual(len(corte), 25)
        self.assertEqual(corte, "2x Black Tea Punch")  # ni a media palabra ni con ':' colgando

    def test_otros_carriers_conservan_el_texto_completo(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._recortar_contenido(self.TEXTO, "fedex"), self.TEXTO)
        self.assertEqual(EnviaAdapter._recortar_contenido(self.TEXTO, "paquetexpress"), self.TEXTO)
        largo = ", ".join(["1x Producto de prueba"] * 10)  # 228 chars → tope general 120
        self.assertLessEqual(len(EnviaAdapter._recortar_contenido(largo, "fedex")), 120)

    def test_palabra_mas_larga_que_el_tope_se_corta_seca(self):
        from apps.envios.adapters import EnviaAdapter
        corte = EnviaAdapter._recortar_contenido("Supercalifragilisticoespialidoso", "estafeta")
        self.assertEqual(len(corte), 25)

    def test_vacio_viaja_como_mercancia(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._recortar_contenido("", "estafeta"), "Mercancía")
        self.assertEqual(EnviaAdapter._recortar_contenido("   ", "fedex"), "Mercancía")

    def test_payload_por_caja_aplica_el_tope_del_carrier(self):
        from apps.catalogo.models import SKU
        from apps.envios.adapters import EnviaAdapter
        from apps.envios.models import Paquete, PaqueteLinea
        from apps.pedidos.models import LineaPedido
        cliente = crear_cliente()
        pedido = crear_pedido(cliente, crear_tienda(cliente))
        paquete = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("1.2"), carrier="estafeta",
        )
        for codigo, descripcion in (
            ("TE-1", "Black Tea Punch: Tisana Ponche Navideño"),
            ("TE-2", "Tropical Bloom: Tisana de Melocotón"),
        ):
            sku = SKU.objects.create(cliente=cliente, codigo=codigo, descripcion=descripcion, peso_gr=100)
            linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=2)
            PaqueteLinea.objects.create(paquete=paquete, linea_pedido=linea, cantidad=2)
        adapter = EnviaAdapter()
        estafeta = adapter._payload(pedido, "estafeta", "ground", paquete=paquete)["packages"][0]["content"]
        fedex = adapter._payload(pedido, "fedex", "ground", paquete=paquete)["packages"][0]["content"]
        self.assertLessEqual(len(estafeta), 25)
        self.assertGreater(len(fedex), 25)
        self.assertTrue(fedex.startswith("2x Black Tea Punch: Tisana Ponche Navideño, 2x Tropical"))


TORRE_99MIN_DIRECTO = {**settings.TORRE, "PROVEEDOR_POR_CARRIER": {"noventa9Minutos": "99minutos"}}


@override_settings(
    TORRE=TORRE_99MIN_DIRECTO, ENVIA_API_KEY="",
    NOVENTA9_API_KEY="cid:sec", NOVENTA9_MODO="full",
)
class Fallback99MinutosTests(TestCase):
    """El directo de 99minutos falla → NOVENTA9_FALLBACK_ENVIA decide."""

    def setUp(self):
        from apps.envios.adapters import MockAdapter
        MockAdapter.reiniciar()
        # carrier_preferente noventa9Minutos + mapa directo → Adapter99Minutos
        self.cliente = crear_cliente(carrier_preferente="noventa9Minutos")
        self.tienda = crear_tienda(self.cliente)

    def _generar_con_directo_caido(self):
        from unittest.mock import patch

        from apps.envios.adapters import Adapter99Minutos, ErrorCarrier
        pedido = crear_pedido(self.cliente, self.tienda)
        with patch.object(Adapter99Minutos, "generar", side_effect=ErrorCarrier("caído")):
            return services.generar_guia(pedido), pedido

    @override_settings(NOVENTA9_FALLBACK_ENVIA=True)
    def test_con_flag_reintenta_por_envia_y_audita(self):
        guia, pedido = self._generar_con_directo_caido()
        self.assertEqual(guia.carrier, "noventa9Minutos")
        self.assertEqual(guia.proveedor, "mock")  # envia sin key en tests = mock
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id=str(pedido.pk), accion="fallback_envia",
            ).exists()
        )

    @override_settings(NOVENTA9_FALLBACK_ENVIA=False)
    def test_sin_flag_el_error_se_superficia(self):
        from apps.envios.adapters import ErrorCarrier
        with self.assertRaises(ErrorCarrier):
            self._generar_con_directo_caido()

    @override_settings(NOVENTA9_FALLBACK_ENVIA=False)
    def test_guia_directa_persiste_el_pdf_base64(self):
        import tempfile

        from unittest.mock import patch

        from apps.envios.adapters import Adapter99Minutos
        pedido = crear_pedido(self.cliente, self.tienda)
        datos = {
            "numero": "990001", "etiqueta_url": "", "etiqueta_pdf": b"%PDF-1.4 x",
            "costo": None, "raw": {"trackingId": 990001},
        }
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()), \
             patch.object(Adapter99Minutos, "generar", return_value=datos):
            guia = services.generar_guia(pedido)
            self.assertEqual(guia.proveedor, "99minutos")
            self.assertTrue(guia.etiqueta_pdf)
            self.assertEqual(guia.etiqueta_url, guia.etiqueta_pdf.url)
            with guia.etiqueta_pdf.open("rb") as archivo:
                self.assertTrue(archivo.read().startswith(b"%PDF"))


class GetAdapterRoutingTests(TestCase):
    """get_adapter: el proveedor de la guía manda; el mapa por carrier decide lo demás."""

    def test_proveedor_mock_regresa_mock_siempre(self):
        with override_settings(ENVIA_API_KEY="k", ENVIA_MODO="full"):
            self.assertIsInstance(services.get_adapter(proveedor="mock"), MockAdapter)

    def test_envia_full_da_adapter_real(self):
        from apps.envios.adapters import EnviaAdapter
        with override_settings(ENVIA_API_KEY="k", ENVIA_MODO="full"):
            self.assertIsInstance(services.get_adapter(carrier="estafeta"), EnviaAdapter)

    def test_sin_configuracion_todo_es_mock(self):
        with override_settings(ENVIA_API_KEY=""):
            self.assertIsInstance(services.get_adapter(carrier="estafeta"), MockAdapter)
            self.assertIsInstance(services.get_adapter(), MockAdapter)


@override_settings(ENVIA_API_KEY="")
class GenerarGuiaTests(TestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)

    def test_genera_guia_mock_con_costos_y_etiqueta(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        guia = services.generar_guia(pedido)
        self.assertTrue(guia.numero.startswith("MOCK-"))
        self.assertEqual(guia.proveedor, "mock")  # cancelar/rastrear rutean por aquí
        self.assertEqual(guia.carrier, "paquetexpress")
        self.assertEqual(guia.estado, Guia.GUIA_CREADA)
        self.assertGreater(guia.costo_cotizado, Decimal("0"))
        self.assertGreater(guia.costo_preferencial, Decimal("0"))
        self.assertLessEqual(guia.costo_preferencial, guia.costo_cotizado)  # plan: mismo precio
        self.assertTrue(guia.etiqueta_url)
        self.assertTrue(
            EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(guia.pk), accion="guia_generada").exists()
        )

    def test_es_idempotente_regresa_la_guia_activa(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        primera = services.generar_guia(pedido)
        segunda = services.generar_guia(pedido)
        self.assertEqual(primera.pk, segunda.pk)
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 1)

    def test_transiciona_pedido_empacado_a_guia_generada(self):
        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, "GUIA_GENERADA")

    @override_settings(TORRE={**settings.TORRE, "FLOTA_PROPIA": True})
    def test_pedido_local_con_flota_sin_guia_externa(self):
        # Comportamiento de flota propia: solo aplica con TORRE["FLOTA_PROPIA"]=True.
        pedido = crear_pedido(self.cliente, self.tienda, es_local=True, cp="01780")
        guia = services.generar_guia(pedido)
        self.assertEqual(guia.carrier, "local")
        self.assertEqual(guia.servicio, "entrega_local")
        self.assertTrue(guia.numero.startswith(f"LOCAL-{pedido.folio}"))  # sufijo -N por paquete
        # Flota local: $100 flat por paquete ≤20 kg (CDMX + metro hasta Toluca)
        self.assertEqual(guia.costo_preferencial, Decimal("100"))
        self.assertEqual(guia.etiqueta_url, "")
        self.assertEqual(guia.proveedor, "local")

    def test_pedido_local_sin_flota_viaja_con_carrier_real(self):
        # Default TORRE["FLOTA_PROPIA"]=False: el es_local genera guía externa
        # (mock) con carrier real — nada cae al carril muerto de SAL-LOCAL.
        pedido = crear_pedido(self.cliente, self.tienda, es_local=True, cp="01780")
        guia = services.generar_guia(pedido)
        self.assertNotEqual(guia.carrier, "local")
        self.assertTrue(guia.numero.startswith("MOCK-"))

    def test_retorno_permite_generar_guia_nueva(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        primera = services.generar_guia(pedido)
        primera.transicionar(Guia.RETORNO, motivo="Retornado por el carrier")
        segunda = services.generar_guia(pedido)
        self.assertNotEqual(primera.pk, segunda.pk)
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 2)

    def test_guia_en_transito_sigue_activa_y_no_se_duplica(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        guia = services.generar_guia(pedido)
        guia.transicionar(Guia.EN_TRANSITO)
        misma = services.generar_guia(pedido)
        self.assertEqual(guia.pk, misma.pk)


@override_settings(ENVIA_API_KEY="", NOVENTA9_API_KEY="cid:sec", NOVENTA9_MODO="full")
class IntegracionClienteRoutingTests(TestCase):
    """integracion_envios del cliente manda el proveedor sin mapa global."""

    def test_cliente_99minutos_rutea_al_adapter_directo(self):
        from apps.envios.adapters import Adapter99Minutos
        cliente = crear_cliente(integracion_envios="99minutos")
        self.assertIsInstance(
            services.get_adapter(carrier="noventa9Minutos", cliente=cliente),
            Adapter99Minutos,
        )

    def test_cliente_envia_ignora_el_directo(self):
        cliente = crear_cliente(integracion_envios="envia")
        # envia sin key en tests = mock: lo importante es que NO es el directo.
        self.assertIsInstance(
            services.get_adapter(carrier="noventa9Minutos", cliente=cliente), MockAdapter,
        )

    @override_settings(NOVENTA9_API_KEY="")
    def test_sin_key_el_flip_no_explota(self):
        # Fail-safe de configuración: cliente flipeado sin credenciales cae a
        # envia (mock en tests) en vez de tronar.
        cliente = crear_cliente(integracion_envios="99minutos")
        self.assertIsInstance(
            services.get_adapter(carrier="noventa9Minutos", cliente=cliente), MockAdapter,
        )


@override_settings(ENVIA_API_KEY="")
class ReplanAlGenerarTests(TestCase):
    """Un plan viejo con un carrier que la config vigente ya no permite se
    re-cotiza AL GENERAR (fix #10, sep-2026): el plan no ata — quitar un
    carrier de CARRIERS_COTIZAR o flipear la integración surte efecto de
    inmediato, sin importar cuándo se planeó el pedido."""

    def setUp(self):
        MockAdapter.reiniciar()
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)

    def _paquete(self, pedido, carrier, servicio="local_next_day"):
        from apps.envios.models import Paquete
        return Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("3"),
            carrier=carrier, servicio=servicio,
        )

    def test_carrier_ya_no_permitido_se_replanea_y_audita(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        # puntopost: en la tabla mock, fuera de CARRIERS_COTIZAR (noventa9Minutos
        # regresó a la lista el 2026-09-07 y ya no sirve de ejemplo).
        paquete = self._paquete(pedido, "puntopost")
        guia = services.generar_guia(pedido)
        self.assertNotEqual(guia.carrier, "puntopost")
        self.assertIn(guia.carrier, settings.TORRE["CARRIERS_COTIZAR"])
        paquete.refresh_from_db()
        self.assertEqual(paquete.carrier, guia.carrier)
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(pedido.pk), accion="replan_paquete",
        )
        self.assertEqual(evento.delta["antes"], "puntopost")
        self.assertEqual(evento.delta["ahora"], guia.carrier)

    def test_carrier_permitido_no_se_toca(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        self._paquete(pedido, "fedex", servicio="ground")
        guia = services.generar_guia(pedido)
        self.assertEqual(guia.carrier, "fedex")
        self.assertFalse(
            EventoAuditoria.objects.filter(accion="replan_paquete").exists()
        )
