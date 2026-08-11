"""En ENVIA_MODO=full se imprime la etiqueta OFICIAL del carrier
(guia.etiqueta_url), jamás la dibujada: Estafeta/PuntoPost rutean con SUS
códigos. En mock/dev se sigue dibujando la de Torre. Decisión 2026-08-10."""
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from apps.envios.models import Guia, TrabajoImpresion
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.piso import etiquetas

PDF_CARRIER = b"%PDF-1.4 etiqueta-oficial-estafeta"


def crear_guia(pedido, **kwargs):
    valores = {
        "carrier": "estafeta", "servicio": "ground", "numero": "ETQ-123",
        "etiqueta_url": "https://api.envia.com/etiquetas/ETQ-123.pdf",
    }
    valores.update(kwargs)
    return Guia.objects.create(pedido=pedido, **valores)


@override_settings(TORRE_MODO_IMPRESION="relay")
class EtiquetaCarrierTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, cp="06600")
        self.guia = crear_guia(self.pedido)

    def _respuesta(self, contenido=PDF_CARRIER, status=200):
        respuesta = requests.Response()
        respuesta.status_code = status
        respuesta._content = contenido
        return respuesta

    @override_settings(ENVIA_MODO="full")
    def test_full_imprime_el_pdf_del_carrier(self):
        with patch("apps.piso.etiquetas.requests.get") as get:
            get.return_value = self._respuesta()
            etiquetas.imprimir_etiqueta(self.guia)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], self.guia.etiqueta_url)
        trabajo = TrabajoImpresion.objects.get(guia=self.guia)
        self.assertEqual(trabajo.pdf.read(), PDF_CARRIER)

    @override_settings(ENVIA_MODO="full")
    def test_full_sin_etiqueta_url_falla_con_instrucciones(self):
        self.guia.etiqueta_url = ""
        self.guia.save(update_fields=["etiqueta_url"])
        with self.assertRaisesMessage(ValueError, "no trae etiqueta del carrier"):
            etiquetas.imprimir_etiqueta(self.guia)
        self.assertFalse(TrabajoImpresion.objects.exists())

    @override_settings(ENVIA_MODO="full")
    def test_full_error_de_red_falla_sin_imprimir_la_dibujada(self):
        """Peor imprimir una etiqueta inservible que fallar y reintentar."""
        with patch("apps.piso.etiquetas.requests.get") as get:
            get.side_effect = requests.ConnectionError("timeout simulada")
            with self.assertRaisesMessage(ValueError, "No pude bajar la etiqueta"):
                etiquetas.imprimir_etiqueta(self.guia)
        self.assertFalse(TrabajoImpresion.objects.exists())

    @override_settings(ENVIA_MODO="full")
    def test_full_respuesta_que_no_es_pdf_falla(self):
        with patch("apps.piso.etiquetas.requests.get") as get:
            get.return_value = self._respuesta(b"<html>error del CDN</html>")
            with self.assertRaisesMessage(ValueError, "no es un PDF"):
                etiquetas.imprimir_etiqueta(self.guia)
        self.assertFalse(TrabajoImpresion.objects.exists())

    def test_modo_mock_sigue_dibujando_la_de_torre(self):
        """En tests ENVIA_MODO se fuerza a 'off': ni una llamada de red."""
        with patch("apps.piso.etiquetas.requests.get") as get:
            etiquetas.imprimir_etiqueta(self.guia)
        get.assert_not_called()
        trabajo = TrabajoImpresion.objects.get(guia=self.guia)
        self.assertTrue(trabajo.pdf.read().startswith(b"%PDF"))
