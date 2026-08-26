"""Adapter99Minutos: auth JWT cacheado, rates por CP, orders+guías base64 y estados.

Todo requests parchado — el contrato viene de developers.99minutos.com (2026-08);
los campos con esquema sin documentar se parsean defensivo y se validan en sandbox.
"""
import base64
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.envios.adapters import (
    Adapter99Minutos,
    CODIGOS_ESTADO_99MIN,
    ErrorCarrier,
    delivery_type_99min,
)

from .base import crear_cliente, crear_pedido, crear_tienda

PDF_FALSO = b"%PDF-1.4 etiqueta de prueba"


def _resp(status=200, cuerpo=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = cuerpo if cuerpo is not None else {}
    r.text = json.dumps(cuerpo or {})
    return r


def _token_ok():
    return _resp(200, {"access_token": "jwt-prueba", "expires_in": 3599, "token_type": "Bearer"})


@override_settings(NOVENTA9_API_KEY="cid-prueba:secreto-prueba")
class Adapter99MinutosTests(TestCase):
    def setUp(self):
        Adapter99Minutos.reiniciar_token()
        self.adapter = Adapter99Minutos()

    def test_delivery_type_mapea_servicios_de_torre(self):
        self.assertEqual(delivery_type_99min("ground"), "NAL")
        self.assertEqual(delivery_type_99min(""), "NAL")
        self.assertEqual(delivery_type_99min("SPT"), "SPT")
        self.assertEqual(delivery_type_99min("same_day"), "SMD")

    def test_token_se_cachea_entre_llamadas(self):
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()) as post, \
             patch("apps.envios.adapters.requests.request", return_value=_resp(200, {"data": {}})):
            self.adapter._request("GET", "/api/v3/x")
            self.adapter._request("GET", "/api/v3/y")
        post.assert_called_once()  # un solo oauth para las dos operaciones

    def test_401_reautentica_una_vez(self):
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()) as post, \
             patch("apps.envios.adapters.requests.request",
                   side_effect=[_resp(401, {}), _resp(200, {"data": {}})]) as req:
            resp = self.adapter._request("GET", "/api/v3/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(req.call_count, 2)
        self.assertEqual(post.call_count, 2)  # token inicial + re-auth

    def test_cotizar_lane_feliz(self):
        respuestas = [
            _resp(200, {"data": {"size": "m"}}),  # sizes
            _resp(200, {"data": [{"totalPrice": 145.5, "deliveryType": "NAL",
                                  "deliveryEstimate": "2-4 días"}]}),
        ]
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=respuestas):
            fila = self.adapter.cotizar_lane("noventa9Minutos", "44100", 8)
        self.assertTrue(fila["ok"])
        self.assertEqual(fila["precio"], Decimal("145.50"))
        self.assertEqual(fila["servicio"], "NAL")

    def test_cotizar_lane_412_es_sin_cobertura(self):
        respuestas = [_resp(200, {"data": {"size": "m"}}), _resp(412, {"message": "no coverage"})]
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=respuestas):
            fila = self.adapter.cotizar_lane("noventa9Minutos", "99999", 8)
        self.assertFalse(fila["ok"])
        self.assertIsNone(fila["precio"])

    def _pedido(self):
        cliente = crear_cliente()
        tienda = crear_tienda(cliente)
        return crear_pedido(cliente, tienda)

    def test_generar_manda_gramos_e_internal_key_y_decodifica_pdf(self):
        pedido = self._pedido()
        respuestas = [
            _resp(201, {"data": {"shipments": [{"trackingId": 12345}]}}),
            _resp(200, {"data": [{"id": "12345", "pdf": base64.b64encode(PDF_FALSO).decode()}]}),
        ]
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=respuestas) as req:
            datos = self.adapter.generar(pedido, "noventa9Minutos", "ground")
        self.assertEqual(datos["numero"], "12345")
        self.assertEqual(datos["etiqueta_pdf"], PDF_FALSO)
        self.assertEqual(datos["etiqueta_url"], "")
        envio = req.call_args_list[0].kwargs["json"]["shipments"][0]
        self.assertEqual(envio["internalKey"], f"{pedido.folio}-1")
        self.assertEqual(envio["deliveryType"], "NAL")
        self.assertEqual(envio["items"][0]["weight"], 1200)  # GRAMOS, no KG
        etiqueta = req.call_args_list[1].kwargs["json"]
        self.assertEqual(etiqueta["guides"][0]["size"], "zebra")

    def test_generar_202_recupera_la_guia_existente(self):
        pedido = self._pedido()
        respuestas = [
            _resp(202, {"message": "duplicated"}),
            _resp(200, {"data": {"trackingId": 777, "status": 1002}}),
            _resp(200, {"data": [{"id": "777", "pdf": base64.b64encode(PDF_FALSO).decode()}]}),
        ]
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=respuestas):
            datos = self.adapter.generar(pedido, "noventa9Minutos", "ground")
        self.assertEqual(datos["numero"], "777")

    def test_generar_sin_pdf_valido_truena(self):
        pedido = self._pedido()
        respuestas = [
            _resp(201, {"data": {"shipments": [{"trackingId": 5}]}}),
            _resp(200, {"data": [{"id": "5", "pdf": base64.b64encode(b"no soy pdf").decode()}]}),
        ]
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=respuestas):
            with self.assertRaises(ErrorCarrier):
                self.adapter.generar(pedido, "noventa9Minutos", "ground")

    def test_cancelar_borra_el_shipment(self):
        guia = MagicMock()
        guia.numero = "12345"
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", return_value=_resp(200, {"message": "ok"})) as req:
            self.assertTrue(self.adapter.cancelar(guia))
        self.assertEqual(req.call_args.args[0], "DELETE")
        self.assertIn("/api/v3/shipments/12345", req.call_args.args[1])

    def test_rastrear_mapea_codigo_numerico(self):
        cuerpo = {"data": {"trackingId": 1, "status": 3001, "updatedAt": "2026-08-26T10:00:00Z"}}
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", return_value=_resp(200, cuerpo)):
            info = self.adapter.rastrear("1")
        self.assertEqual(info["estado"], "EN_TRANSITO")
        self.assertIsNotNone(info["ts_evento"])

    def test_rastrear_status_como_dict(self):
        cuerpo = {"data": {"status": {"code": 4002, "name": "Entrega confirmada"}}}
        with patch("apps.envios.adapters.requests.post", return_value=_token_ok()), \
             patch("apps.envios.adapters.requests.request", return_value=_resp(200, cuerpo)):
            info = self.adapter.rastrear("1")
        self.assertEqual(info["estado"], "ENTREGADO")
        self.assertIn("Entrega confirmada", info["descripcion"])

    def test_mapa_de_codigos_cubre_el_ciclo_completo(self):
        self.assertEqual(CODIGOS_ESTADO_99MIN[2003], "RECOLECTADO")
        self.assertEqual(CODIGOS_ESTADO_99MIN[4101], "INTENTO_FALLIDO")
        self.assertEqual(CODIGOS_ESTADO_99MIN[5001], "RETORNO")
        self.assertEqual(CODIGOS_ESTADO_99MIN[8003], "EXCEPCION")
