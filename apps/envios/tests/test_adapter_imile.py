"""AdapterImile: sobre firmado (MD5 validado contra el ejemplo de su doc), token
cacheado con re-auth en 407, calShippingFee, createOrder con etiqueta base64 y
recuperación por orderNo duplicado, deleteOrder, track con la hora exacta del
carrier y /order/pick/notify; más el ruteo (get_adapter, cotizador sin caché,
recolección desde Salida, respaldo por envia).

Todo requests parchado. Las credenciales están pendientes (2026-09-25): el
contrato real se valida en su sandbox (IMILE_API_BASE de pruebas) al recibirlas.
"""
import base64
import json
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios import cotizador, services
from apps.envios.adapters import (
    AdapterImile,
    ErrorCarrier,
    ErrorImile,
    MockAdapter,
    _parsear_fecha_imile,
    normalizar_estado_imile,
)
from apps.envios.models import CotizacionCache, Guia, Paquete, PaqueteLinea
from apps.pedidos.models import LineaPedido

from .base import crear_cliente, crear_pedido, crear_tienda

PDF_FALSO = b"%PDF-1.4 etiqueta imile de prueba"
SECRET_DOC = "MIICdwIBADANBgkqhkiG9w0BAQEFAASCAmEwggJdAgEAAoGBAMfSz+7WGuw8nwu9"
TORRE_IMILE_DIRECTO = {**settings.TORRE, "PROVEEDOR_POR_CARRIER": {"imile": "imile"}}


def _resp(status=200, cuerpo=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = cuerpo if cuerpo is not None else {}
    r.text = json.dumps(cuerpo or {})
    return r


def _grant(token="tok-1"):
    return _resp(200, {"code": "200", "message": "success", "data": {"accessToken": token, "expiresIn": 7200}})


def _ok(data):
    return _resp(200, {"code": "200", "message": "success", "data": data})


def _falla(codigo, mensaje):
    return _resp(200, {"code": str(codigo), "message": mensaje})


def _cuerpo(llamada):
    """El JSON que viajó en esa llamada a requests.post (va como bytes compactos)."""
    return json.loads(llamada.kwargs["data"].decode("utf-8"))


def _b64(pdf=PDF_FALSO):
    return base64.b64encode(pdf).decode()


@override_settings(
    IMILE_API_KEY="C21018141:" + SECRET_DOC, IMILE_MODO="full", IMILE_PRODUCT_CODE="MX-STD",
    IMILE_API_BASE="https://test-openapi.52imile.cn", ENVIA_API_KEY="",
)
class AdapterImileTests(TestCase):
    def setUp(self):
        AdapterImile.reiniciar_token()
        self.adapter = AdapterImile()

    # ── sobre y firma ──
    def test_firma_coincide_con_el_ejemplo_de_su_doc(self):
        """Integration Guide → Signature rules: MD5 en mayúsculas de secretKey +
        llaves comunes ordenadas con su valor + JSON compacto de param + secretKey."""
        comunes = {
            "customerId": "C21018141", "signMethod": "MD5", "format": "json", "version": "1.0.0",
            "timeZone": "+8", "timestamp": "1655438036695", "accessToken": "9d269815-e928-41a0-8653-608ff0d9ee6c",
        }
        param_json = json.dumps({"orderType": "1", "orderNo": "6082824250179", "language": "2"}, separators=(",", ":"))
        self.assertEqual(self.adapter.firmar(comunes, param_json), "45858AABA92082D9AF784E05EBE6368C")

    def test_llamar_pide_token_y_manda_el_sobre_firmado_compacto(self):
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"x": 1})]) as post:
            respuesta = self.adapter._llamar("/client/track/getOne", {"orderNo": "1", "orderType": "1"})
        self.assertEqual(respuesta["data"], {"x": 1})
        self.assertEqual(post.call_args_list[0].args[0], "https://test-openapi.52imile.cn/auth/accessToken/grant")
        grant = _cuerpo(post.call_args_list[0])
        self.assertNotIn("accessToken", grant)  # el grant se firma sin token
        self.assertEqual(grant["param"], {"grantType": "clientCredential"})
        self.assertEqual(post.call_args_list[1].args[0], "https://test-openapi.52imile.cn/client/track/getOne")
        cuerpo = _cuerpo(post.call_args_list[1])
        self.assertEqual(cuerpo["accessToken"], "tok-1")
        self.assertEqual(cuerpo["customerId"], "C21018141")
        self.assertEqual((cuerpo["signMethod"], cuerpo["format"], cuerpo["version"], cuerpo["timeZone"]), ("MD5", "json", "1.0.0", "-6"))
        comunes = {k: v for k, v in cuerpo.items() if k not in ("sign", "param")}
        param_json = json.dumps(cuerpo["param"], separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(cuerpo["sign"], self.adapter.firmar(comunes, param_json))
        crudo = post.call_args_list[1].kwargs["data"].decode()
        self.assertNotIn(SECRET_DOC, crudo)  # la secretKey jamás viaja
        self.assertNotIn(": ", crudo)  # JSON compacto: lo firmado es lo que viaja
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Content-Type"], "application/json; charset=utf-8")

    def test_token_se_cachea_y_407_reautentica_una_vez(self):
        respuestas = [_grant("tok-1"), _ok({}), _falla(407, "invalid token"), _grant("tok-2"), _ok({})]
        with patch("apps.envios.adapters.requests.post", side_effect=respuestas) as post:
            self.adapter._llamar("/a", {})
            self.adapter._llamar("/a", {})
        self.assertEqual(post.call_count, 5)  # un solo grant para la primera; re-auth en la segunda
        self.assertEqual(_cuerpo(post.call_args_list[4])["accessToken"], "tok-2")

    def test_token_invalido_dos_veces_seguidas_truena(self):
        respuestas = [_grant("tok-1"), _falla(408, "expired"), _grant("tok-2"), _falla(408, "expired")]
        with patch("apps.envios.adapters.requests.post", side_effect=respuestas):
            with self.assertRaises(ErrorImile) as ctx:
                self.adapter._llamar("/a", {})
        self.assertEqual(ctx.exception.codigo, "408")

    def test_error_de_negocio_trae_el_code_de_imile(self):
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _falla(40025, "consignee city [Foo] not exist")]):
            with self.assertRaises(ErrorImile) as ctx:
                self.adapter._llamar("/client/order/v2/createOrder", {})
        self.assertEqual(ctx.exception.codigo, "40025")
        self.assertIn("not exist", str(ctx.exception))
        self.assertIsInstance(ctx.exception, ErrorCarrier)  # el reintento con municipio lo reconoce

    def test_http_caido_es_error_carrier(self):
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _resp(502, {"message": "bad gateway"})]):
            with self.assertRaises(ErrorCarrier):
                self.adapter._llamar("/a", {})

    # ── cotización ──
    def test_cotizar_lane_feliz(self):
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"totalAmount": "85.5", "currency": "MXN"})]) as post:
            fila = self.adapter.cotizar_lane("imile", "44100", 3, (30, 25, 20))
        self.assertTrue(fila["ok"])
        self.assertEqual(fila["precio"], Decimal("85.50"))
        self.assertEqual(fila["servicio"], "standard")
        self.assertEqual(fila["estimado"], "")
        self.assertEqual(post.call_args_list[1].args[0], "https://test-openapi.52imile.cn/client/order/calShippingFee")
        param = _cuerpo(post.call_args_list[1])["param"]
        self.assertEqual(param["consigneeInfo"], {"country": "MEX", "province": "Jalisco", "city": "", "zipCode": "44100"})
        self.assertEqual(param["senderInfo"]["zipCode"], "01780")
        self.assertEqual(param["senderInfo"]["province"], "Ciudad de México")
        self.assertEqual(param["totalWeight"], 3.0)
        self.assertEqual(param["totalVolume"], 15000)
        self.assertEqual((param["orderType"], param["paymentMethod"], param["clientDeclaredCurrency"]), ("100", "PPD", "Local"))

    def test_cotizar_lane_error_de_imile_es_sin_cobertura(self):
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _falla(40012, "city not exist")]):
            fila = self.adapter.cotizar_lane("imile", "99999", 8)
        self.assertFalse(fila["ok"])
        self.assertIsNone(fila["precio"])
        self.assertIn("40012", fila["detalle"])

    @override_settings(IMILE_DIAS_PROMESA="3")
    def test_cotizar_lane_estimado_sale_de_settings(self):
        """Su API no regresa fecha estimada: la promesa foránea es configuración."""
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"totalAmount": 120})]):
            fila = AdapterImile().cotizar_lane("imile", "64000", 3)
        self.assertEqual(fila["estimado"], "3 días")
        self.assertEqual(fila["precio"], Decimal("120.00"))

    # ── generación ──
    def _pedido(self, **kwargs):
        cliente = crear_cliente()
        tienda = crear_tienda(cliente)
        return crear_pedido(cliente, tienda, **kwargs)

    def _caja(self, pedido):
        """Caja de 2 six (4 kg c/u) con dimensiones propias."""
        six = SKU.objects.create(cliente=pedido.cliente, codigo="SIX", descripcion="Six Colimita",
                                 peso_gr=4000, precio_declarado=Decimal("300"))
        linea = LineaPedido.objects.create(pedido=pedido, sku=six, cantidad=2, reservada=True)
        paquete = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("8.40"),
                                         largo_cm=40, ancho_cm=30, alto_cm=25, carrier="imile", servicio="standard",
                                         precio_cotizado=Decimal("120"))
        PaqueteLinea.objects.create(paquete=paquete, linea_pedido=linea, cantidad=2)
        return paquete

    def test_generar_manda_la_orden_completa_y_decodifica_la_etiqueta(self):
        pedido = self._pedido()
        respuestas = [_grant(), _ok({"expressNo": "IM123456", "imileAwb": _b64(), "subWaybillNo": ["IM123456-1"]})]
        with patch("apps.envios.adapters.requests.post", side_effect=respuestas) as post:
            datos = self.adapter.generar(pedido, "imile", "standard")
        self.assertEqual(datos["numero"], "IM123456")
        self.assertEqual(datos["etiqueta_pdf"], PDF_FALSO)
        self.assertEqual(datos["etiqueta_url"], "")
        self.assertIsNone(datos["costo"])
        self.assertEqual(datos["raw"], {"proveedor": "imile", "expressNo": "IM123456", "orderNo": f"{pedido.folio}-1",
                                        "subWaybillNo": ["IM123456-1"]})
        self.assertEqual(post.call_args_list[1].args[0], "https://test-openapi.52imile.cn/client/order/v2/createOrder")
        param = _cuerpo(post.call_args_list[1])["param"]
        self.assertEqual(param["orderNo"], f"{pedido.folio}-1")
        self.assertEqual(param["orderType"], "100")
        self.assertEqual(param["serviceInfo"], {"logisticsProductCode": "MX-STD", "pickupService": 0, "deliveryService": "Delivery"})
        paquete = param["packageInfo"]
        self.assertEqual(paquete["grossWeight"], 1.2)  # KILOS: 1200 g esperados del pedido
        self.assertEqual((paquete["length"], paquete["width"], paquete["high"], paquete["totalVolume"]), (30, 25, 20, 15000))
        self.assertEqual(paquete["clientDeclaredValue"], 500.0)  # sin líneas: el valor declarado del pedido
        self.assertEqual((paquete["paymentMethod"], paquete["clientDeclaredCurrency"], paquete["totalCount"]), ("PPD", "Local", 1))
        self.assertEqual(len(param["skuInfos"]), 1)
        self.assertEqual(param["skuInfos"][0]["skuNo"], "MERCANCIA")
        self.assertNotIn("skuHsCode", param["skuInfos"][0])
        destino = param["consigneeInfo"]
        self.assertEqual((destino["country"], destino["zipCode"], destino["province"], destino["city"]), ("MEX", "44100", "Jalisco", "Guadalajara"))
        self.assertEqual(destino["externalNo"], "123")
        self.assertEqual(destino["contacts"], "Ana Compradora")
        self.assertTrue(destino["phone"].endswith("3120000000"))
        self.assertEqual(destino["addressType"], "customer")
        origen = param["senderInfo"]
        self.assertEqual((origen["addressType"], origen["zipCode"], origen["externalNo"], origen["country"]), ("warehouse", "01780", "380", "MEX"))

    @override_settings(IMILE_HS_CODE_DEFAULT="220300")
    def test_generar_por_caja_usa_sus_lineas_peso_y_medidas(self):
        pedido = self._pedido()
        paquete = self._caja(pedido)
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"expressNo": "IM1", "imileAwb": _b64()})]) as post:
            AdapterImile().generar(pedido, "imile", "standard", paquete=paquete)
        param = _cuerpo(post.call_args_list[1])["param"]
        self.assertEqual(param["orderNo"], f"{pedido.folio}-1")
        info = param["packageInfo"]
        self.assertEqual(info["grossWeight"], 8.4)
        self.assertEqual((info["length"], info["width"], info["high"], info["totalVolume"]), (40, 30, 25, 30000))
        self.assertEqual(info["clientDeclaredValue"], 600.0)  # 2 × $300, no el total del pedido
        self.assertEqual(param["skuInfos"], [{
            "skuNo": "SIX", "skuName": "Six Colimita", "skuLocalName": "Six Colimita", "skuQty": 2,
            "skuDeclaredValue": 300.0, "skuWeight": 4.0, "skuHsCode": "220300",
        }])

    def test_generar_con_ciudad_forzada_la_manda_en_el_destino(self):
        """El reintento dirigido (`ciudad=` municipio del catálogo) ante 'city not exist'."""
        pedido = self._pedido()
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"expressNo": "IM1", "imileAwb": _b64()})]) as post:
            self.adapter.generar(pedido, "imile", "standard", ciudad="Zapopan")
        self.assertEqual(_cuerpo(post.call_args_list[1])["param"]["consigneeInfo"]["city"], "Zapopan")

    def test_generar_sin_product_code_no_llama_a_nadie(self):
        pedido = self._pedido()
        with override_settings(IMILE_PRODUCT_CODE=""), patch("apps.envios.adapters.requests.post") as post:
            with self.assertRaises(ErrorCarrier):
                AdapterImile().generar(pedido, "imile", "standard")
        post.assert_not_called()

    def test_generar_30001_recupera_la_guia_existente_con_su_etiqueta(self):
        """orderNo duplicado tras un timeout: la guía ya existe allá; se reimprime
        en vez de comprar otra."""
        pedido = self._pedido()
        respuestas = [_grant(), _falla(30001, "orderNo duplicate"), _ok({"expressNo": "IM777", "imileAwb": _b64()})]
        with patch("apps.envios.adapters.requests.post", side_effect=respuestas) as post:
            datos = self.adapter.generar(pedido, "imile", "standard")
        self.assertEqual(datos["numero"], "IM777")
        self.assertEqual(post.call_args_list[2].args[0], "https://test-openapi.52imile.cn/client/order/reprintOrder")
        self.assertEqual(_cuerpo(post.call_args_list[2])["param"]["orderCode"], f"{pedido.folio}-1")

    def test_generar_sin_pdf_valido_truena(self):
        pedido = self._pedido()
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"expressNo": "IM5", "imileAwb": _b64(b"no soy pdf")})]):
            with self.assertRaises(ErrorCarrier):
                self.adapter.generar(pedido, "imile", "standard")

    def test_order_no_cambia_tras_una_guia_cancelada(self):
        """Tras cancelar (cambio de dirección o de paquetería) la recompra lleva
        otro orderNo: iMile rechaza el repetido (30001) y lo recuperaría."""
        pedido = self._pedido()
        Guia.objects.create(pedido=pedido, carrier="imile", numero="IM1", proveedor="imile", estado=Guia.CANCELADA)
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"expressNo": "IM2", "imileAwb": _b64()})]) as post:
            self.adapter.generar(pedido, "imile", "standard")
        self.assertEqual(_cuerpo(post.call_args_list[1])["param"]["orderNo"], f"{pedido.folio}-1-r1")

    # ── cancelación ──
    def test_cancelar_manda_delete_order_con_nuestro_order_no(self):
        guia = MagicMock()
        guia.numero = "IM1"
        guia.raw = {"proveedor": "imile", "orderNo": "PED-00001-1"}
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok(None)]) as post:
            self.assertTrue(self.adapter.cancelar(guia))
        self.assertEqual(post.call_args_list[1].args[0], "https://test-openapi.52imile.cn/client/order/deleteOrder")
        self.assertEqual(_cuerpo(post.call_args_list[1])["param"], {"orderCode": "PED-00001-1", "waybillNo": "IM1"})

    # ── rastreo ──
    def test_rastrear_trae_historial_con_hora_exacta_y_estados_mapeados(self):
        data = {
            "latestStatus": "Delivered", "latestStatusTime": "2026-09-24 15:42:10", "latestSite": "CDMX Sur",
            "locusType": "delivery", "timeZone": "GMT-06:00",
            "locus": [
                {"latestStatus": "SubmitOrder", "latestStatusTime": "2026-09-23 10:00:00", "latestSite": "",
                 "locusDetailed": "Orden creada", "locusType": "submitted", "timeZone": "GMT-06:00"},
                {"latestStatus": "Delivered", "latestStatusTime": "2026-09-24 15:42:10", "latestSite": "CDMX Sur",
                 "locusDetailed": "Entregado", "locusType": "delivery", "timeZone": "GMT-06:00"},
                {"latestStatus": "OutForDelivery", "latestStatusTime": "2026-09-24 08:15:00", "latestSite": "CDMX Sur",
                 "locusDetailed": "En ruta de entrega", "locusType": "delivery", "timeZone": "GMT-06:00"},
            ],
        }
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok(data)]) as post:
            info = self.adapter.rastrear("IM1")
        self.assertEqual(_cuerpo(post.call_args_list[1])["param"], {"orderType": "1", "language": "3", "orderNo": "IM1"})
        self.assertEqual(info["estado"], "ENTREGADO")
        self.assertEqual(info["descripcion"], "Delivered · CDMX Sur")
        self.assertEqual(info["ts_evento"], datetime(2026, 9, 24, 21, 42, 10, tzinfo=dt_timezone.utc))  # 15:42 GMT-6
        self.assertEqual([e["estado"] for e in info["eventos"]], ["GUIA_CREADA", "EN_RUTA", "ENTREGADO"])  # ordenado por hora
        self.assertEqual(info["eventos"][1]["ts"], datetime(2026, 9, 24, 14, 15, tzinfo=dt_timezone.utc))
        self.assertEqual(info["eventos"][1]["descripcion"], "En ruta de entrega · CDMX Sur")
        self.assertEqual(info["eventos"][0]["crudo"], "SubmitOrder")

    def test_rastrear_estado_desconocido_no_mueve_la_guia(self):
        data = {"latestStatus": "Algo raro", "latestStatusTime": "2026-09-24 15:42:10", "locus": []}
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok(data)]):
            info = self.adapter.rastrear("IM1")
        self.assertIsNone(info["estado"])
        self.assertEqual(info["eventos"], [])

    def test_mapa_de_estados_cubre_el_ciclo(self):
        casos = {
            "SubmitOrder": "GUIA_CREADA", "Picked Up": "RECOLECTADO", "In Transit": "EN_TRANSITO",
            "Arrived at station": "EN_TRANSITO", "Out For Delivery": "EN_RUTA", "Delivered": "ENTREGADO",
            "Delivery Failed": "INTENTO_FALLIDO", "ReturnArrive": "RETORNO", "Return to sender": "RETORNO",
            "CancelOrder": "EXCEPCION", "On Hold": "RETENIDO", "Entregado": "ENTREGADO",
        }
        for crudo, canon in casos.items():
            self.assertEqual(normalizar_estado_imile(crudo), canon, crudo)
        self.assertEqual(normalizar_estado_imile("", "cancelOrder"), "EXCEPCION")
        self.assertIsNone(normalizar_estado_imile("Algo raro"))

    def test_parsear_fecha_imile_respeta_su_zona(self):
        self.assertEqual(_parsear_fecha_imile("2026-09-24 15:42:10", "GMT-06:00"), datetime(2026, 9, 24, 21, 42, 10, tzinfo=dt_timezone.utc))
        self.assertEqual(_parsear_fecha_imile("2026-09-24 15:42:10", "+8"), datetime(2026, 9, 24, 7, 42, 10, tzinfo=dt_timezone.utc))
        sin_zona = _parsear_fecha_imile("2026-09-24 15:42:10")
        self.assertEqual((sin_zona.hour, str(sin_zona.tzinfo)), (15, "America/Mexico_City"))
        self.assertIsNone(_parsear_fecha_imile("ayer"))
        self.assertIsNone(_parsear_fecha_imile(""))

    # ── recolección ──
    def test_agendar_recoleccion_notifica_todas_las_guias(self):
        guias = [MagicMock(numero="IM1"), MagicMock(numero="IM2")]
        with patch("apps.envios.adapters.requests.post", side_effect=[_grant(), _ok({"batchNo": "B-1"})]) as post:
            resultado = self.adapter.agendar_recoleccion("imile", date(2026, 9, 26), 10, 18, guias)
        self.assertEqual(resultado, {"folio": "B-1", "costo": None})
        self.assertEqual(post.call_args_list[1].args[0], "https://test-openapi.52imile.cn/order/pick/notify")
        self.assertEqual(_cuerpo(post.call_args_list[1])["param"], {
            "waybillNos": ["IM1", "IM2"], "pickDate": "2026-09-26", "pickStart": "10:00", "pickEnd": "18:00", "returnBatchNo": True,
        })


@override_settings(ENVIA_API_KEY="")
class RuteoImileTests(TestCase):
    """iMile directo solo con IMILE_API_KEY + IMILE_MODO y el mapa por carrier;
    sin eso el carrier imile sigue por envia (Mock en pruebas)."""

    def setUp(self):
        MockAdapter.reiniciar()

    @override_settings(TORRE=TORRE_IMILE_DIRECTO, IMILE_API_KEY="c:s", IMILE_MODO="full")
    def test_full_compra_cotiza_y_recolecta_directo(self):
        self.assertIsInstance(services.get_adapter(carrier="imile"), AdapterImile)
        self.assertIsInstance(services.get_adapter_cotizacion("imile"), AdapterImile)
        self.assertIsInstance(services.get_adapter(proveedor="imile"), AdapterImile)  # la guía emitida manda
        self.assertIsInstance(services.get_adapter(carrier="estafeta"), MockAdapter)
        self.assertTrue(services.carrier_acepta_recoleccion("imile"))
        self.assertEqual(dict(services.opciones_paqueteria())["imile"], "iMile directo")

    @override_settings(TORRE=TORRE_IMILE_DIRECTO, IMILE_API_KEY="c:s", IMILE_MODO="cotizar")
    def test_modo_cotizar_solo_cotiza(self):
        self.assertIsInstance(services.get_adapter_cotizacion("imile"), AdapterImile)
        self.assertIsInstance(services.get_adapter(carrier="imile"), MockAdapter)
        self.assertFalse(services.carrier_acepta_recoleccion("imile"))

    @override_settings(TORRE=TORRE_IMILE_DIRECTO, IMILE_API_KEY="", IMILE_MODO="full")
    def test_sin_credenciales_sigue_por_envia(self):
        self.assertIsInstance(services.get_adapter(carrier="imile"), MockAdapter)
        self.assertIsInstance(services.get_adapter_cotizacion("imile"), MockAdapter)
        self.assertFalse(services.carrier_acepta_recoleccion("imile"))
        self.assertEqual(dict(services.opciones_paqueteria())["imile"], "iMile (vía envia.com)")

    def test_carriers_pickup_de_envia_siguen_igual(self):
        self.assertTrue(services.carrier_acepta_recoleccion("fedex"))
        self.assertFalse(services.carrier_acepta_recoleccion("noventa9Minutos"))

    @override_settings(TORRE=TORRE_IMILE_DIRECTO)
    def test_cotizador_no_cachea_imile_directo(self):
        cliente = crear_cliente()
        directa = {"carrier": "imile", "servicio": "standard", "precio": Decimal("95"), "estimado": "", "ok": True}
        envia = {"carrier": "estafeta", "servicio": "ground", "precio": Decimal("150"), "estimado": "", "ok": True}
        with patch("apps.envios.services.cotizar_lane_carrier",
                   side_effect=lambda c, *a, **k: directa if c == "imile" else envia) as clc:
            cotizador.cotizar_lane("44100", 3, cliente=cliente, carriers=["imile", "estafeta"])
            filas = cotizador.cotizar_lane("44100", 3, cliente=cliente, carriers=["imile", "estafeta"])
        self.assertEqual(filas[0], directa)
        self.assertEqual(sorted(c.args[0] for c in clc.call_args_list), ["estafeta", "imile", "imile"])
        self.assertEqual(list(CotizacionCache.objects.values_list("carrier", flat=True)), ["estafeta"])

    @override_settings(TORRE=TORRE_IMILE_DIRECTO, IMILE_API_KEY="c:s", IMILE_MODO="full")
    def test_respaldo_por_envia_solo_con_su_flag(self):
        cliente = crear_cliente(carrier_preferente="imile")
        pedido = crear_pedido(cliente, crear_tienda(cliente))
        with patch.object(AdapterImile, "generar", side_effect=ErrorCarrier("caído")):
            with self.assertRaises(ErrorCarrier):
                services.generar_guia(pedido)
            with override_settings(IMILE_FALLBACK_ENVIA=True):
                guia = services.generar_guia(pedido)
        self.assertEqual((guia.carrier, guia.proveedor), ("imile", "mock"))  # envia sin key en pruebas = mock
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="fallback_envia")
        self.assertIn("iMile directo falló", evento.motivo)

    @override_settings(TORRE=TORRE_IMILE_DIRECTO, IMILE_API_KEY="c:s", IMILE_MODO="full")
    def test_servicio_de_recoleccion_usa_el_adapter_de_imile(self):
        cliente = crear_cliente()
        pedido = crear_pedido(cliente, crear_tienda(cliente))
        guia = Guia.objects.create(pedido=pedido, carrier="imile", numero="IM1", proveedor="imile", estado=Guia.GUIA_CREADA)
        with patch.object(AdapterImile, "agendar_recoleccion", return_value={"folio": "B-9", "costo": None}) as agendar:
            recoleccion = services.agendar_recoleccion("imile", date(2026, 9, 26), 10, 18, [guia], actor=None)
        self.assertEqual(recoleccion.folio_carrier, "B-9")
        self.assertIsNone(recoleccion.costo)
        self.assertEqual(agendar.call_args.args[0], "imile")
