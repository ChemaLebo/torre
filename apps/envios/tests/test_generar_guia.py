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
    """El state va en el vocabulario code_shopify de envia (FAQ: /ship/generate/
    lo valida; el province_code de Shopify ES ese vocabulario y pasa derecho)."""

    def _pedido(self, cp, province_code):
        from types import SimpleNamespace
        direccion = {"address1": "Calle 1", "city": "X", "zip": cp}
        if province_code is not None:
            direccion["province_code"] = province_code
        return SimpleNamespace(
            cp=cp, direccion=direccion,
            comprador_nombre="Prueba", comprador_tel="", comprador_email="",
        )

    def test_province_code_de_shopify_pasa_derecho(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("28048", "COL"))
        self.assertEqual(destino["state"], "COL")  # jamás CL: el 2-dígitos da 1129

    def test_pedido_manual_sin_province_code_deriva_del_cp(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("28048", None))
        self.assertEqual(destino["state"], "COL")

    def test_cdmx_manual_es_df(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("01780", None))
        self.assertEqual(destino["state"], "DF")

    def test_guadalajara_manual_es_jal(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("44100", None))
        self.assertEqual(destino["state"], "JAL")  # la tabla vieja decía JA


class OrigenPorCarrierTests(TestCase):
    """El state del ORIGEN se traduce por carrier: estafeta exige "CX" (2
    letras) y el resto viaja con el code_shopify "DF" — hallazgo en vivo de
    PED-00015 (mismo payload: DF → 1129 con estafeta, CX generó)."""

    def test_estafeta_manda_cx(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._origen("estafeta")["state"], "CX")

    def test_fedex_conserva_el_default_df(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._origen("fedex")["state"], "DF")

    def test_sin_carrier_es_df(self):
        from apps.envios.adapters import EnviaAdapter
        self.assertEqual(EnviaAdapter._origen()["state"], "DF")


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
        paquete = self._paquete(pedido, "noventa9Minutos")  # fuera de la lista
        guia = services.generar_guia(pedido)
        self.assertNotEqual(guia.carrier, "noventa9Minutos")
        self.assertIn(guia.carrier, settings.TORRE["CARRIERS_COTIZAR"])
        paquete.refresh_from_db()
        self.assertEqual(paquete.carrier, guia.carrier)
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(pedido.pk), accion="replan_paquete",
        )
        self.assertEqual(evento.delta["antes"], "noventa9Minutos")
        self.assertEqual(evento.delta["ahora"], guia.carrier)

    def test_carrier_permitido_no_se_toca(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        self._paquete(pedido, "fedex", servicio="ground")
        guia = services.generar_guia(pedido)
        self.assertEqual(guia.carrier, "fedex")
        self.assertFalse(
            EventoAuditoria.objects.filter(accion="replan_paquete").exists()
        )
