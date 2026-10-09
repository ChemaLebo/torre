"""Reintento por conexión (Chema 2026-10-09): UNA repetición de la misma
petición cuando falla la red. Cotizar, consultar y autenticar repiten ante
cualquier fallo; comprar, cancelar y agendar solo si la petición nunca llegó
al carrier (si salió y se perdió la respuesta, repetirla cobraría doble).
Todo requests parchado; la pausa entre intentos también."""
import json
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from apps.envios.adapters import (
    Adapter99Minutos,
    AdapterImile,
    EnviaAdapter,
    ErrorCarrier,
    _nunca_llego,
)

SIN_PAUSA = patch("apps.envios.adapters.time.sleep")


def _resp(status=200, cuerpo=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = cuerpo if cuerpo is not None else {}
    r.text = json.dumps(cuerpo or {})
    return r


def _sin_conexion():
    """requests.ConnectionError como lo arma requests cuando no hubo conexión."""
    return requests.ConnectionError(MaxRetryError(None, "https://carrier.test/x", reason=NewConnectionError(None, "refused")))


def _cortada():
    """La conexión se cortó a medias: la petición pudo haber llegado."""
    return requests.ConnectionError(ProtocolError("Connection aborted."))


class NuncaLlegoTests(SimpleTestCase):
    def test_clasifica_los_fallos_de_red(self):
        self.assertTrue(_nunca_llego(_sin_conexion()))
        self.assertTrue(_nunca_llego(requests.ConnectTimeout("connect")))
        self.assertFalse(_nunca_llego(requests.ReadTimeout("read")))
        self.assertFalse(_nunca_llego(_cortada()))
        self.assertFalse(_nunca_llego(requests.exceptions.ChunkedEncodingError("x")))
        self.assertFalse(_nunca_llego(requests.ConnectionError()))  # sin causa: no se asume


class EnviaReintentoTests(TestCase):
    def test_cotizar_repite_ante_cualquier_fallo(self):
        respuestas = [requests.ReadTimeout("read"), _resp(200, {"data": [{"service": "ground", "totalPrice": 90}]})]
        with SIN_PAUSA as pausa, patch("apps.envios.adapters.requests.post", side_effect=respuestas) as post:
            cuerpo = EnviaAdapter()._post("/ship/rate/", {})
        self.assertEqual(cuerpo["data"][0]["totalPrice"], 90)
        self.assertEqual(post.call_count, 2)
        pausa.assert_called_once()

    def test_cotizar_lane_repite_y_cotiza(self):
        tarifa = {"carrier": "estafeta", "service": "ground", "totalPrice": 120, "deliveryEstimate": "2 días"}
        respuestas = [_cortada(), _resp(200, {"data": [tarifa]})]
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=respuestas) as post:
            fila = EnviaAdapter().cotizar_lane("estafeta", "44100", 4)
        self.assertTrue(fila["ok"])
        self.assertEqual(post.call_count, 2)

    def test_comprar_repite_solo_si_nunca_llego(self):
        guia = {"data": [{"trackingNumber": "ENV1", "label": "http://l", "totalPrice": 100}]}
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=[_sin_conexion(), _resp(200, guia)]) as post:
            cuerpo = EnviaAdapter()._post("/ship/generate/", {}, crea=True)
        self.assertEqual(cuerpo["data"][0]["trackingNumber"], "ENV1")
        self.assertEqual(post.call_count, 2)

    def test_comprar_no_repite_si_la_respuesta_se_perdio(self):
        for fallo in (requests.ReadTimeout("read"), _cortada()):
            with SIN_PAUSA as pausa, patch("apps.envios.adapters.requests.post", side_effect=[fallo, _resp(200, {})]) as post:
                with self.assertRaises(ErrorCarrier) as ctx:
                    EnviaAdapter()._post("/ship/generate/", {}, crea=True)
            self.assertIn("No se pudo contactar a envia.com", str(ctx.exception))
            self.assertEqual(post.call_count, 1)
            pausa.assert_not_called()

    def test_dos_fallos_seguidos_suben_el_error(self):
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=[_sin_conexion(), _sin_conexion()]) as post:
            with self.assertRaises(ErrorCarrier):
                EnviaAdapter()._post("/ship/generate/", {}, crea=True)
        self.assertEqual(post.call_count, 2)

    def test_rastrear_repite(self):
        rastreo = {"data": [{"status": "delivered", "events": [{"status": "delivered", "description": "Entregado", "date": "2026-10-09T10:00:00Z"}]}]}
        with SIN_PAUSA, patch("apps.envios.adapters.requests.get", side_effect=[requests.ReadTimeout("r"), _resp(200, rastreo)]) as get:
            info = EnviaAdapter().rastrear("ENV1")
        self.assertEqual(info["descripcion"], "Entregado")
        self.assertEqual(get.call_count, 2)


@override_settings(NOVENTA9_API_KEY="cid-prueba:secreto-prueba")
class NoventaNueveReintentoTests(TestCase):
    def setUp(self):
        Adapter99Minutos.reiniciar_token()

    def _token_ok(self):
        return _resp(200, {"access_token": "jwt", "expires_in": 3599})

    def test_token_repite_ante_cualquier_fallo(self):
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=[requests.ReadTimeout("r"), self._token_ok()]) as post:
            self.assertEqual(Adapter99Minutos()._token(), "jwt")
        self.assertEqual(post.call_count, 2)

    def test_get_repite_ante_cualquier_fallo(self):
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", return_value=self._token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=[requests.ReadTimeout("r"), _resp(200, {"data": {}})]) as req:
            resp = Adapter99Minutos()._request("GET", "/api/v3/shipping/rates/sizes")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(req.call_count, 2)

    def test_post_repite_solo_si_nunca_llego(self):
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", return_value=self._token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=[_sin_conexion(), _resp(200, {"data": {}})]) as req:
            Adapter99Minutos()._request("POST", "/api/v3/orders", json_body={})
        self.assertEqual(req.call_count, 2)
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", return_value=self._token_ok()), \
             patch("apps.envios.adapters.requests.request", side_effect=[requests.ReadTimeout("r"), _resp(200, {})]) as req:
            with self.assertRaises(ErrorCarrier):
                Adapter99Minutos()._request("POST", "/api/v3/orders", json_body={})
        self.assertEqual(req.call_count, 1)


@override_settings(
    IMILE_API_KEY="C21018141:secreto", IMILE_MODO="full", IMILE_PRODUCT_CODE="MX-STD",
    IMILE_API_BASE="https://test-openapi.52imile.cn",
)
class ImileReintentoTests(TestCase):
    def setUp(self):
        AdapterImile.reiniciar_token()

    def _grant(self):
        return _resp(200, {"code": "200", "message": "success", "data": {"accessToken": "tok", "expiresIn": 7200}})

    def _ok(self, data=None):
        return _resp(200, {"code": "200", "message": "success", "data": data or {}})

    def test_cotizar_repite_ante_cualquier_fallo(self):
        respuestas = [self._grant(), requests.ReadTimeout("r"), self._ok({"totalAmount": "85.5"})]
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=respuestas) as post:
            respuesta = AdapterImile()._llamar("/client/order/calShippingFee", {"x": 1})
        self.assertEqual(respuesta["data"]["totalAmount"], "85.5")
        self.assertEqual(post.call_count, 3)

    def test_crear_orden_no_repite_si_la_respuesta_se_perdio(self):
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=[self._grant(), requests.ReadTimeout("r"), self._ok()]) as post:
            with self.assertRaises(ErrorCarrier):
                AdapterImile()._llamar("/client/order/v2/createOrder", {"x": 1})
        self.assertEqual(post.call_count, 2)

    def test_crear_orden_repite_si_nunca_llego(self):
        with SIN_PAUSA, patch("apps.envios.adapters.requests.post", side_effect=[self._grant(), _sin_conexion(), self._ok({"expressNo": "IM1"})]) as post:
            respuesta = AdapterImile()._llamar("/client/order/v2/createOrder", {"x": 1})
        self.assertEqual(respuesta["data"]["expressNo"], "IM1")
        self.assertEqual(post.call_count, 3)
