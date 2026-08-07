"""Folio INC-AAAA-####: consecutivo único que reinicia por año."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia

from .utils import crear_cliente


class FolioTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.anio = timezone.localtime().year

    def test_folio_consecutivo_del_anio_en_curso(self):
        i1 = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL)
        i2 = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL)
        self.assertEqual(i1.folio, f"INC-{self.anio}-0001")
        self.assertEqual(i2.folio, f"INC-{self.anio}-0002")

    def test_folio_reinicia_por_anio(self):
        # Un folio alto de un año anterior NO arrastra el consecutivo actual.
        Incidencia.objects.create(
            cliente=self.cliente,
            tipo=Incidencia.TIPO_DAN,
            origen=Incidencia.ORIGEN_MANUAL,
            folio=f"INC-{self.anio - 1}-0042",
        )
        nueva = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL)
        self.assertEqual(nueva.folio, f"INC-{self.anio}-0001")

    def test_folio_es_unico(self):
        i1 = abrir_incidencia(self.cliente, Incidencia.TIPO_FAL, Incidencia.ORIGEN_MANUAL)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Incidencia.objects.create(
                    cliente=self.cliente,
                    tipo=Incidencia.TIPO_FAL,
                    origen=Incidencia.ORIGEN_MANUAL,
                    folio=i1.folio,
                )

    def test_consecutivo_ignora_huecos_no_intermedios(self):
        # El consecutivo se calcula sobre el folio máximo del año.
        Incidencia.objects.create(
            cliente=self.cliente,
            tipo=Incidencia.TIPO_DES,
            origen=Incidencia.ORIGEN_AUTO,
            folio=f"INC-{self.anio}-0007",
        )
        nueva = abrir_incidencia(self.cliente, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO)
        self.assertEqual(nueva.folio, f"INC-{self.anio}-0008")
