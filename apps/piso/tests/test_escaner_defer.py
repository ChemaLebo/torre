"""Regresión del crítico frontend: scripts inline vs. dependencias tardías.

Dos clases del mismo bug (script corre al parsear, sus dependencias aún no
existen) que los tests de Django no pueden atrapar ejecutando JS:

1. escaner.js carga con `defer` (corre justo antes de DOMContentLoaded): un
   inline que toque Escaner/Feedback DURANTE el parseo muere en ReferenceError
   y se lleva TODO el JS de la página (cámara, AJAX, ×N, pinta()).
2. Un include con script parse-time que busca nodos renderizados DESPUÉS del
   include (plano_bodega → [data-panel]) obtiene un NodeList vacío en
   silencio: los paneles jamás cambian (así se rompió Bodega en Mesa/Portal).

Patrón obligatorio: envolver el inline en
document.addEventListener("DOMContentLoaded", ...) — o, para usos accesorios,
guardar con `window.Feedback && ...` / `if (window.Feedback)`.

Cobertura: TODOS los directorios de templates (proyecto + apps), no solo piso.
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR)


def _templates():
    """Todos los .html del proyecto: templates/ global + apps/*/templates."""
    rutas = list((BASE / "templates").rglob("*.html"))
    for app_templates in sorted((BASE / "apps").glob("*/templates")):
        rutas.extend(app_templates.rglob("*.html"))
    return rutas


class EscanerDeferTests(SimpleTestCase):
    def test_base_carga_escaner_con_defer(self):
        contenido = (BASE / "templates" / "base.html").read_text(encoding="utf-8")
        # El tag exacto: dos substrings sueltos pasarían con un defer en un comentario.
        self.assertIn("<script defer src=\"{% static 'js/escaner.js' %}\">", contenido)

    def test_inline_con_escaner_montar_espera_domcontentloaded(self):
        con_escaner = []
        for template in _templates():
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

    def test_inline_con_feedback_espera_o_guarda(self):
        """Todo uso inline de Feedback.* espera DOMContentLoaded o va guardado
        con window.Feedback (uso accesorio que tolera la ausencia)."""
        con_feedback = []
        for template in _templates():
            contenido = template.read_text(encoding="utf-8")
            posicion = contenido.find("Feedback.")
            if posicion == -1:
                continue
            con_feedback.append(template.name)
            while posicion != -1:
                espera = "DOMContentLoaded" in contenido[:posicion]
                guardado = "window.Feedback" in contenido[max(0, posicion - 80):posicion]
                self.assertTrue(
                    espera or guardado,
                    f"{template.name}: uso inline de Feedback.* sin esperar "
                    "DOMContentLoaded ni guardar con window.Feedback (con "
                    "defer, Feedback no existe durante el parseo).",
                )
                posicion = contenido.find("Feedback.", posicion + 1)
        # La página que más lo usa sigue cubierta.
        self.assertIn("empaque_pedido.html", con_feedback)

    def test_plano_bodega_espera_los_paneles(self):
        """El include del plano se emite ANTES de los [data-panel]: su script
        debe esperar DOMContentLoaded o el querySelectorAll regresa vacío."""
        contenido = (BASE / "templates" / "includes" / "plano_bodega.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("DOMContentLoaded", contenido)
        self.assertLess(
            contenido.index("DOMContentLoaded"),
            contenido.index("querySelectorAll"),
            "plano_bodega.html: el script consulta el DOM antes de esperar "
            "DOMContentLoaded — los paneles [data-panel] aún no existen.",
        )
