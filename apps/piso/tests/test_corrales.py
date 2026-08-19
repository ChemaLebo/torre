"""Mapeo carrier→corral leído de catalogo.Ubicacion (campo carriers).

El corral sin carriers es el comodín; "local" conserva SAL-LOCAL (datos
viejos); una BD sin corrales cae al mapeo legacy para no dejar el piso ciego.
"""
from django.test import TestCase

from apps.catalogo.models import Ubicacion
from apps.piso.views import _corral_de_carrier, _mapa_corrales, corrales_activos


def corral(codigo, carriers="", activo=True):
    return Ubicacion.objects.create(
        codigo=codigo, tipo=Ubicacion.SALIDA, carriers=carriers, activo=activo,
    )


class MapaCorralesTests(TestCase):
    def test_carrier_declarado_va_a_su_corral(self):
        corral("SAL-99MIN", "noventa9Minutos")
        corral("SAL-OTRO")
        self.assertEqual(_corral_de_carrier("noventa9Minutos"), "SAL-99MIN")

    def test_carrier_sin_corral_cae_al_comodin(self):
        corral("SAL-99MIN", "noventa9Minutos")
        corral("SAL-VARIOS")
        self.assertEqual(_corral_de_carrier("estafeta"), "SAL-VARIOS")

    def test_un_corral_acepta_varios_carriers(self):
        corral("SAL-EXPRESS", "fedex, estafeta")
        self.assertEqual(_corral_de_carrier("fedex"), "SAL-EXPRESS")
        self.assertEqual(_corral_de_carrier("estafeta"), "SAL-EXPRESS")

    def test_local_conserva_sal_local_aunque_no_exista(self):
        corral("SAL-OTRO")
        self.assertEqual(_corral_de_carrier("local"), "SAL-LOCAL")

    def test_sal_local_vacio_jamas_es_comodin(self):
        corral("SAL-LOCAL", "local")
        corral("SAL-OTRO")
        self.assertEqual(_corral_de_carrier("fedex"), "SAL-OTRO")

    def test_corral_apagado_no_mapea(self):
        corral("SAL-99MIN", "noventa9Minutos", activo=False)
        corral("SAL-OTRO")
        self.assertEqual(_corral_de_carrier("noventa9Minutos"), "SAL-OTRO")

    def test_bd_sin_corrales_usa_mapeo_legacy(self):
        self.assertEqual(_corral_de_carrier("paquetexpress"), "SAL-PQX")
        self.assertEqual(_corral_de_carrier("estafeta"), "SAL-OTRO")
        self.assertEqual(corrales_activos(), [
            ("SAL-PQX", "Paquetexpress"),
            ("SAL-LOCAL", "Entrega local"),
            ("SAL-OTRO", "Otros carriers"),
        ])

    def test_mapa_se_puede_precalcular_para_loops(self):
        corral("SAL-99MIN", "noventa9Minutos")
        corral("SAL-OTRO")
        mapa = _mapa_corrales()
        self.assertEqual(_corral_de_carrier("noventa9Minutos", mapa), "SAL-99MIN")
        self.assertEqual(_corral_de_carrier("fedex", mapa), "SAL-OTRO")

    def test_nombre_del_corral_es_su_lista_de_carriers(self):
        corral("SAL-99MIN", "noventa9Minutos")
        corral("SAL-VARIOS")
        self.assertEqual(corrales_activos(), [
            ("SAL-99MIN", "noventa9Minutos"),
            ("SAL-VARIOS", "Otros carriers"),
        ])
