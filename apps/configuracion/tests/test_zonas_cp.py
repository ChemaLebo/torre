"""Orígenes y zonas de cobro por CP en Configuración (Chema 2026-10-22): el
comando carga WOP y los rangos del CSV; la tabla manda sobre el archivo; los
rangos de un origen no se traslapan; la caché se olvida al editar."""
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.configuracion.models import Origen, ZonaCP
from apps.configuracion.services import origen_principal
from apps.finanzas import zonas
from apps.finanzas.services import zona_de_cp


def correr(*args):
    salida = StringIO()
    call_command("cargar_zonas_cp", *args, stdout=salida)
    return salida.getvalue()


class CargarZonasCPTests(TestCase):
    def tearDown(self):
        zonas.recargar()

    def test_crea_wop_y_carga_el_csv_del_repo_una_sola_vez(self):
        self.assertIsNone(origen_principal())
        salida = correr()
        wop = origen_principal()
        self.assertEqual((wop.cp, wop.principal, wop.ciudad), ("01780", True, "Ciudad de Mexico"))
        self.assertIn("Origen principal creado", salida)
        self.assertEqual(ZonaCP.objects.filter(origen=wop).count(), 5)
        self.assertEqual(ZonaCP.objects.get(cp_desde="00000").cp_hasta, "16999")
        # La tabla ya manda: mismas zonas de siempre, ahora desde la base.
        self.assertEqual((zona_de_cp("06600"), zona_de_cp("44100"), zona_de_cp("76000"), zona_de_cp("64000")),
                         ("local", "metro", "metro", "nacional"))
        self.assertEqual(zonas.ciudad_de_cp("76000"), "")  # el CSV del repo no trae ciudad
        # Segunda corrida: nada que hacer, sin duplicados.
        self.assertIn("nada que hacer", correr())
        self.assertEqual((Origen.objects.count(), ZonaCP.objects.count()), (1, 5))

    def test_reemplazar_con_otro_archivo_y_ciudad(self):
        correr()
        ruta = Path(tempfile.mkdtemp()) / "zonas.csv"
        ruta.write_text(
            "cp_desde,cp_hasta,zona,ciudad\n00000,16999,local,CDMX\n76000,76999,metro,Querétaro\n"
            "20100,20100,metro,Un solo CP\n", encoding="utf-8",
        )
        salida = correr("--archivo", str(ruta), "--reemplazar")
        self.assertIn("3 rango(s) cargados", salida)
        self.assertIn("5 reemplazados", salida)
        self.assertEqual(ZonaCP.objects.count(), 3)
        self.assertEqual((zona_de_cp("76100"), zonas.ciudad_de_cp("76100")), ("metro", "Querétaro"))
        self.assertEqual((zona_de_cp("44100"), zonas.ciudad_de_cp("44100")), ("nacional", ""))  # ya no está en la tabla
        self.assertEqual((zona_de_cp("20100"), zona_de_cp("20101")), ("metro", "nacional"))  # un CP suelto: desde = hasta

    def test_sin_reemplazar_no_toca_y_archivo_invalido_aborta(self):
        correr()
        ruta = Path(tempfile.mkdtemp()) / "malo.csv"
        ruta.write_text("cp_desde,cp_hasta,zona\n", encoding="utf-8")
        with self.assertRaises(CommandError):
            correr("--archivo", str(ruta), "--reemplazar")
        self.assertEqual(ZonaCP.objects.count(), 5)  # la transacción no escribió nada

    def test_tabla_vacia_sigue_leyendo_el_csv_de_respaldo(self):
        self.assertEqual(ZonaCP.objects.count(), 0)
        zonas.recargar()
        self.assertEqual(zona_de_cp("06600"), "local")
        self.assertEqual(len(zonas.rangos()), 5)

    def test_la_tabla_manda_sobre_el_archivo_y_la_cache_se_olvida_al_editar(self):
        correr()
        ruta = Path(tempfile.mkdtemp()) / "otro.csv"
        ruta.write_text("cp_desde,cp_hasta,zona\n64000,64999,local\n", encoding="utf-8")
        with override_settings(TORRE={**settings.TORRE, "ZONAS_CP_ARCHIVO": str(ruta)}):
            zonas.recargar()
            self.assertEqual(zona_de_cp("64100"), "nacional")  # el archivo ya no se lee: la tabla tiene renglones
            rango = ZonaCP.objects.get(cp_desde="44000")
            rango.zona = ZonaCP.LOCAL
            rango.save()
            self.assertEqual(zona_de_cp("44100"), "local")  # sin recargar a mano: la señal olvidó la caché
            rango.delete()
            self.assertEqual(zona_de_cp("44100"), "nacional")


class ValidacionZonaCPTests(TestCase):
    def setUp(self):
        self.wop = Origen.objects.create(nombre="WOP", cp="01780", principal=True)
        self.otro = Origen.objects.create(nombre="Bodega del cliente", cp="76000")

    def _rango(self, desde, hasta, origen=None, **extra):
        z = ZonaCP(origen=origen or self.wop, cp_desde=desde, cp_hasta=hasta, zona=ZonaCP.METRO, **extra)
        z.full_clean()
        z.save()
        return z

    def test_cinco_digitos_y_orden(self):
        with self.assertRaises(ValidationError):
            self._rango("1251", "01300")
        with self.assertRaises(ValidationError):
            self._rango("01300", "01251")
        self.assertEqual(str(self._rango("01253", "01253", ciudad="CDMX")), "01253-01253 → Metropolitana (CDMX)")

    def test_sin_traslapes_en_el_mismo_origen_pero_si_entre_origenes(self):
        self._rango("01251", "01300")
        with self.assertRaises(ValidationError) as ctx:
            self._rango("01300", "01400")
        self.assertIn("Se traslapa con 01251-01300", str(ctx.exception))
        self._rango("01301", "01400")  # el hueco se parte en dos rangos pegados
        self._rango("01251", "01300", origen=self.otro)  # mismo rango, otro origen: válido
        inactivo = self._rango("01350", "01360", activo=False)  # un rango inactivo no cuenta para el traslape
        self.assertFalse(inactivo.activo)

    def test_un_solo_origen_principal(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            Origen.objects.create(nombre="Otro principal", cp="01780", principal=True)

    def test_origen_exige_cp_de_cinco_digitos(self):
        with self.assertRaises(ValidationError):
            Origen(nombre="Malo", cp="178").full_clean()
