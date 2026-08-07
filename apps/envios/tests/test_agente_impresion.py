"""El agente de bodega (deploy/agente_impresion.py) SIN red ni impresora reales.

El script es stdlib puro y no ejecuta nada al importarse (main queda tras el
guard de __main__), así que se carga con importlib y se prueba
`procesar_trabajo` parchando sus dependencias (_peticion / _imprimir_pdf):
urllib y lp jamás se tocan aquí.
"""
import importlib.util
import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

RUTA_AGENTE = Path(settings.BASE_DIR) / "deploy" / "agente_impresion.py"

BASE = "https://torre.ejemplo.mx"
TOKEN = "token-agente"
IMPRESORA = "Aiyin_BY480BT"
TRABAJO = {
    "id": 7, "folio": "PED-00001", "guia": "PQX-0001",
    "url_pdf": f"{BASE}/api/impresion/7/pdf/",
}


def cargar_agente():
    espec = importlib.util.spec_from_file_location("agente_impresion", RUTA_AGENTE)
    modulo = importlib.util.module_from_spec(espec)
    espec.loader.exec_module(modulo)
    return modulo


class AgenteImpresionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agente = cargar_agente()

    def procesar(self, trabajo=None, descarga=b"%PDF-1.4 etiqueta", imprime=(True, "")):
        """Corre procesar_trabajo con red y lp falsos. Regresa (ok, peticiones, impresiones)."""
        peticiones, impresiones = [], []

        def peticion_falsa(url, token, datos=None):
            peticiones.append((url, token, datos))
            if datos is None and isinstance(descarga, Exception):
                raise descarga
            return descarga if datos is None else b"{}"

        def imprimir_falso(ruta, impresora):
            with open(ruta, "rb") as archivo:
                contenido = archivo.read()
            impresiones.append({"ruta": ruta, "impresora": impresora, "contenido": contenido})
            return imprime

        with mock.patch.object(self.agente, "_peticion", peticion_falsa), \
                mock.patch.object(self.agente, "_imprimir_pdf", imprimir_falso), \
                mock.patch.object(self.agente, "_log"):
            ok = self.agente.procesar_trabajo(
                dict(trabajo or TRABAJO), BASE, TOKEN, IMPRESORA
            )
        return ok, peticiones, impresiones

    def test_exito_baja_imprime_y_confirma_ok(self):
        ok, peticiones, impresiones = self.procesar()
        self.assertTrue(ok)
        # 1) bajó el PDF con el token, 2) confirmó el resultado con el token.
        self.assertEqual(peticiones[0], (TRABAJO["url_pdf"], TOKEN, None))
        self.assertEqual(
            peticiones[1],
            (f"{BASE}/api/impresion/7/resultado/", TOKEN, {"ok": True}),
        )
        # lp recibió un tempfile con el PDF completo y la cola configurada.
        self.assertEqual(len(impresiones), 1)
        self.assertEqual(impresiones[0]["impresora"], IMPRESORA)
        self.assertEqual(impresiones[0]["contenido"], b"%PDF-1.4 etiqueta")

    def test_borra_el_tempfile_al_terminar(self):
        for imprime in ((True, ""), (False, "sin papel")):
            _, _, impresiones = self.procesar(imprime=imprime)
            self.assertFalse(os.path.exists(impresiones[0]["ruta"]))

    def test_falla_de_lp_reporta_el_error_a_torre(self):
        ok, peticiones, _ = self.procesar(imprime=(False, "la cola no existe"))
        self.assertFalse(ok)
        self.assertEqual(
            peticiones[-1],
            (f"{BASE}/api/impresion/7/resultado/", TOKEN,
             {"ok": False, "error": "la cola no existe"}),
        )

    def test_falla_de_descarga_reporta_sin_intentar_imprimir(self):
        ok, peticiones, impresiones = self.procesar(descarga=RuntimeError("timeout de red"))
        self.assertFalse(ok)
        self.assertEqual(impresiones, [])
        url, token, datos = peticiones[-1]
        self.assertEqual(url, f"{BASE}/api/impresion/7/resultado/")
        self.assertFalse(datos["ok"])
        self.assertIn("timeout de red", datos["error"])

    def test_url_pdf_relativa_se_completa_con_la_base(self):
        trabajo = dict(TRABAJO, url_pdf="/api/impresion/7/pdf/")
        _, peticiones, _ = self.procesar(trabajo=trabajo)
        self.assertEqual(peticiones[0][0], f"{BASE}/api/impresion/7/pdf/")

    def test_el_docstring_trae_la_instalacion_launchd_y_systemd(self):
        doc = self.agente.__doc__
        self.assertIn("launchctl load", doc)
        self.assertIn("systemctl enable --now torre-impresion", doc)
        self.assertIn("TORRE_TOKEN_IMPRESION", doc)
        self.assertIn("IMPRESORA", doc)
