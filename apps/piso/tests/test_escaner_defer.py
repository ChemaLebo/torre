"""Regresión del crítico frontend: escaner.js carga con `defer`.

Un script inline que llame Escaner/Feedback DURANTE el parseo muere en
ReferenceError (defer corre justo antes de DOMContentLoaded) y se lleva TODO
el JS de la página: cámara, AJAX, ×N, pinta(). El patrón obligatorio es
envolver el inline en document.addEventListener("DOMContentLoaded", ...).
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TEMPLATES_PISO = Path(__file__).resolve().parent.parent / "templates" / "piso"


class EscanerDeferTests(SimpleTestCase):
    def test_base_carga_escaner_con_defer(self):
        base = Path(settings.BASE_DIR) / "templates" / "base.html"
        contenido = base.read_text(encoding="utf-8")
        self.assertIn("defer", contenido)
        self.assertIn("escaner.js", contenido)

    def test_inline_con_escaner_montar_espera_domcontentloaded(self):
        con_escaner = []
        for template in sorted(TEMPLATES_PISO.glob("*.html")):
            contenido = template.read_text(encoding="utf-8")
            if "Escaner.montar" not in contenido:
                continue
            con_escaner.append(template.name)
            posicion = contenido.index("Escaner.montar")
            self.assertIn(
                "DOMContentLoaded", contenido[:posicion],
                f"{template.name}: script inline llama Escaner.montar sin "
                "esperar DOMContentLoaded (escaner.js carga con defer y aún "
                "no existe durante el parseo).",
            )
        # Las dos páginas del visor de cámara existen y están cubiertas.
        self.assertIn("picking_pedido.html", con_escaner)
        self.assertIn("recepcion_detalle.html", con_escaner)