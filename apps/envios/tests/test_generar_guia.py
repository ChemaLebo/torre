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
    """El state del destino sale del CP (tabla de envia), no del código ISO de Shopify."""

    def _pedido(self, cp, province_code):
        from types import SimpleNamespace
        return SimpleNamespace(
            cp=cp,
            direccion={"address1": "Calle 1", "city": "X", "zip": cp, "province_code": province_code},
            comprador_nombre="Prueba", comprador_tel="", comprador_email="",
        )

    def test_colima_manda_cl_no_col(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("28048", "COL"))
        self.assertEqual(destino["state"], "CL")  # COL de Shopify = error 1129 en envia

    def test_cdmx_sigue_siendo_df(self):
        from apps.envios.adapters import EnviaAdapter
        destino = EnviaAdapter._destino(self._pedido("01780", "DF"))
        self.assertEqual(destino["state"], "DF")

    def test_sin_cp_cae_al_codigo_de_la_direccion(self):
        from apps.envios.adapters import EnviaAdapter
        pedido = self._pedido("", "COL")
        pedido.direccion["zip"] = ""
        destino = EnviaAdapter._destino(pedido)
        self.assertEqual(destino["state"], "COL")


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
