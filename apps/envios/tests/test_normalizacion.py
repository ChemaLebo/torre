"""Normalización de estados de tracking de envia.com → estados canónicos."""
from django.test import SimpleTestCase

from apps.envios.adapters import normalizar_estado_envia


class NormalizacionEstadosTests(SimpleTestCase):
    def test_estados_en_ingles(self):
        casos = [
            ("Label created", "GUIA_CREADA"),
            ("Shipment generated", "GUIA_CREADA"),
            ("Picked up", "RECOLECTADO"),
            ("Package collected", "RECOLECTADO"),
            ("In Transit", "EN_TRANSITO"),
            ("Out for delivery", "EN_RUTA"),
            ("Delivered", "ENTREGADO"),
            ("Failed delivery attempt", "INTENTO_FALLIDO"),
            ("Attempted delivery", "INTENTO_FALLIDO"),
            ("Returned to sender", "RETORNO"),
            ("Held in customs", "RETENIDO"),
            ("Exception", "EXCEPCION"),
        ]
        for crudo, esperado in casos:
            with self.subTest(crudo=crudo):
                self.assertEqual(normalizar_estado_envia(crudo), esperado)

    def test_estados_en_espanol_con_acentos(self):
        casos = [
            ("Guía generada", "GUIA_CREADA"),
            ("Recolectado en origen", "RECOLECTADO"),
            ("En tránsito", "EN_TRANSITO"),
            ("En ruta de entrega", "EN_RUTA"),
            ("Salió a reparto", "EN_RUTA"),
            ("Entregado", "ENTREGADO"),
            ("Intento de entrega", "INTENTO_FALLIDO"),
            ("No entregado: destinatario ausente", "INTENTO_FALLIDO"),
            ("Devolución al remitente", "RETORNO"),
            ("Retenido en aduana", "RETENIDO"),
            ("Excepción del carrier", "EXCEPCION"),
        ]
        for crudo, esperado in casos:
            with self.subTest(crudo=crudo):
                self.assertEqual(normalizar_estado_envia(crudo), esperado)

    def test_canonicos_pasan_directo(self):
        for canonico in [
            "GUIA_CREADA", "RECOLECTADO", "EN_TRANSITO", "EN_RUTA", "ENTREGADO",
            "INTENTO_FALLIDO", "RETENIDO", "RETORNO", "EXCEPCION",
        ]:
            with self.subTest(canonico=canonico):
                self.assertEqual(normalizar_estado_envia(canonico), canonico)

    def test_intento_fallido_gana_antes_que_entregado(self):
        # "delivery"/"entrega" aparecen dentro de textos de intento fallido:
        # el orden de patrones debe resolverlos como INTENTO_FALLIDO.
        self.assertEqual(normalizar_estado_envia("failed delivery attempt"), "INTENTO_FALLIDO")
        self.assertEqual(normalizar_estado_envia("Intento de entrega fallido"), "INTENTO_FALLIDO")

    def test_desconocido_y_vacio_regresan_none(self):
        self.assertIsNone(normalizar_estado_envia("estado marciano xyz"))
        self.assertIsNone(normalizar_estado_envia(""))
        self.assertIsNone(normalizar_estado_envia(None))
