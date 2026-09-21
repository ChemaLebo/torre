"""Ciudad por CP desde el catálogo de envia (geocodes) con caché en LocalidadCP.

iMile valida CP↔ciudad y el conector de envia no traduce: el destino de la
guía toma la localidad y el estado del catálogo cuando lo conoce, y conserva
lo de Shopify cuando no (PED-00030 y PED-00034, 2026-09-21).
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings

from apps.core.models import EventoAuditoria
from apps.envios.adapters import EnviaAdapter
from apps.envios.localidades import localidad_por_cp
from apps.envios.models import LocalidadCP

GEOCODES_72830 = [{
    "zip_code": "72830",
    "country": {"name": "México", "code": "MX"},
    "state": {"name": "Puebla", "iso_code": "MX-PUE", "code": {"1digit": None, "2digit": "PU", "3digit": "PUE"}},
    "locality": "San Andrés Cholula",
    "suburbs": ["Alta Vista", "Lomas de Angelópolis"],
    "regions": {"region_1": "Puebla", "region_2": "San Andrés Cholula", "region_3": "", "region_4": ""},
}]


def _respuesta(status=200, cuerpo=None):
    resp = Mock(status_code=status)
    resp.json = Mock(return_value=cuerpo)
    resp.raise_for_status = Mock(side_effect=requests.HTTPError(str(status)) if status >= 400 else None)
    return resp


def _pedido(cp, city, province_code):
    return SimpleNamespace(
        cp=cp, direccion={"address1": "Chipre 139", "city": city, "zip": cp, "province_code": province_code},
        comprador_nombre="Prueba", comprador_tel="", comprador_email="",
    )


@override_settings(ENVIA_API_KEY="llave", ENVIA_MODO="cotizar")
class LocalidadPorCpTests(TestCase):
    def test_consulta_una_vez_y_cachea(self):
        with patch("apps.envios.localidades.requests.get", return_value=_respuesta(200, GEOCODES_72830)) as get:
            fila = localidad_por_cp("72830")
            localidad_por_cp("72830")
        self.assertEqual(get.call_count, 1)
        self.assertIn("geocodes.envia.com/zipcode/MX/72830", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer llave")
        self.assertEqual((fila.localidad, fila.municipio, fila.estado), ("San Andrés Cholula", "San Andrés Cholula", "PU"))
        self.assertEqual(fila.colonias, ["Alta Vista", "Lomas de Angelópolis"])
        self.assertEqual(LocalidadCP.objects.count(), 1)

    def test_cp_desconocido_se_cachea_vacio_y_no_insiste(self):
        with patch("apps.envios.localidades.requests.get", return_value=_respuesta(200, [])) as get:
            fila = localidad_por_cp("99999")
            localidad_por_cp("99999")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(fila.localidad, "")
        with patch("apps.envios.localidades.requests.get", return_value=_respuesta(404, {"statusCode": 404})):
            self.assertEqual(localidad_por_cp("99998").localidad, "")

    def test_fallo_de_red_regresa_none_sin_cachear_y_audita(self):
        with patch("apps.envios.localidades.requests.get", side_effect=requests.ConnectionError("sin red")):
            self.assertIsNone(localidad_por_cp("72830"))
        self.assertFalse(LocalidadCP.objects.exists())
        evento = EventoAuditoria.objects.get(accion="localidad_cp_fallo")
        self.assertEqual((evento.entidad, evento.entidad_id), ("cp", "72830"))
        with patch("apps.envios.localidades.requests.get", return_value=_respuesta(500, {})):
            self.assertIsNone(localidad_por_cp("72830"))

    def test_cp_invalido_ni_consulta(self):
        with patch("apps.envios.localidades.requests.get") as get:
            self.assertIsNone(localidad_por_cp("abc"))
            self.assertIsNone(localidad_por_cp(""))
        get.assert_not_called()


class SinLlaveTests(TestCase):
    def test_sin_llave_o_modo_off_no_pega_a_la_red(self):
        # settings de test: ENVIA_MODO="off" siempre.
        with patch("apps.envios.localidades.requests.get") as get:
            self.assertIsNone(localidad_por_cp("72830"))
        get.assert_not_called()
        self.assertFalse(LocalidadCP.objects.exists())


class DestinoConCatalogoTests(TestCase):
    def test_destino_usa_la_localidad_y_el_estado_del_catalogo(self):
        LocalidadCP.objects.create(cp="72830", localidad="San Andrés Cholula", municipio="San Andrés Cholula", estado="PU")
        LocalidadCP.objects.create(cp="77049", localidad="Chetumal", municipio="Othón P. Blanco", estado="QR")
        destino = EnviaAdapter._destino(_pedido("72830", "Puebla", "PUE"))
        self.assertEqual((destino["city"], destino["state"], destino["number"]), ("San Andrés Cholula", "PU", "139"))
        destino = EnviaAdapter._destino(_pedido("77049", "CHETUMAL", "Q ROO"))
        self.assertEqual((destino["city"], destino["state"]), ("Chetumal", "QR"))

    def test_sin_catalogo_o_cp_desconocido_conserva_shopify(self):
        LocalidadCP.objects.create(cp="99999", localidad="", municipio="", estado="")
        destino = EnviaAdapter._destino(_pedido("99999", "Villa Real", "PUE"))
        self.assertEqual((destino["city"], destino["state"]), ("Villa Real", "PU"))
        destino = EnviaAdapter._destino(_pedido("28048", "Colima", "COL"))
        self.assertEqual((destino["city"], destino["state"]), ("Colima", "CL"))
