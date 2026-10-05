"""Link público de rastreo por paquetería (TORRE["RASTREO_CARRIER_URL"]): el
patrón de cada carrier con el número de guía, codificado; sin patrón o sin
número no hay link."""
from django.test import SimpleTestCase

from apps.envios.services import url_rastreo_carrier


class UrlRastreoCarrierTests(SimpleTestCase):
    def test_patrones_por_paqueteria(self):
        self.assertEqual(
            url_rastreo_carrier("estafeta", "005870980061070999ARYW"),
            "https://cs.estafeta.com/es/Tracking/searchByGet?wayBill=005870980061070999ARYW",
        )
        # amPm va con la M mayúscula, como lo nombra envia.com.
        self.assertEqual(
            url_rastreo_carrier("amPm", "7012345678"),
            "https://grupoampm.com/rastreador/?tracking-id=7012345678",
        )
        self.assertEqual(url_rastreo_carrier("noventa9Minutos", "1387524154"), "https://tracking.99minutos.com/search/1387524154")

    def test_numero_codificado_y_casos_sin_link(self):
        self.assertTrue(url_rastreo_carrier("amPm", "AB 12/3").endswith("tracking-id=AB%2012%2F3"))
        self.assertEqual(url_rastreo_carrier("local", "LOCAL-1"), "")   # entrega propia: sin rastreo público
        self.assertEqual(url_rastreo_carrier("amPm", ""), "")
        self.assertEqual(url_rastreo_carrier("", "123"), "")
