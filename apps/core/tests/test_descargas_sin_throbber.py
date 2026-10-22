"""El throbber de base.html se apaga solo cuando carga la página siguiente;
una descarga no navega, así que cada link o formulario que baja un archivo
debe ir marcado (`download` en links, `data-descarga` en forms) para que el
spinner no se quede encendido ni el botón bloqueado (Chema 2026-10-22).
Esta prueba lee las plantillas y vigila que ninguna descarga nueva se cuele
sin la marca."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

RAIZ = Path(settings.BASE_DIR)
# Nombres de URL y patrones de href que responden con un archivo (Content-Disposition: attachment).
MARCAS_LINK = (
    "cliente_skus_exportar", "inventario_exportar_conteo", "inventario_reacomodo_csv",
    "recepcion_realidad_csv", "reporte_dia_csv", "reporte_csv", "{{ url_csv }}", "?csv=",
)
# Formularios cuya respuesta es un archivo: el action o la acción oculta los delatan.
MARCAS_FORM = ("recepciones_plantilla", "descargar_transformado")


def _plantillas():
    return sorted(set(RAIZ.glob("templates/**/*.html")) | set(RAIZ.glob("apps/*/templates/**/*.html")))


class DescargasSinThrobberTests(SimpleTestCase):
    def test_el_throbber_ignora_links_download_y_forms_data_descarga(self):
        base = (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn('a.hasAttribute("download")', base)
        self.assertIn('ev.target.hasAttribute("data-descarga")', base)

    def test_todo_link_que_descarga_lleva_download(self):
        faltan = []
        for ruta in _plantillas():
            for numero, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), start=1):
                if "<a " not in linea or not any(m in linea for m in MARCAS_LINK):
                    continue
                for enlace in re.findall(r"<a [^>]*>", linea):
                    if any(m in enlace for m in MARCAS_LINK) and not re.search(r"\sdownload(\s|>|=)", enlace):
                        faltan.append(f"{ruta.relative_to(RAIZ)}:{numero}")
        self.assertEqual(faltan, [], "links que descargan sin atributo download (el spinner se queda encendido)")

    def test_todo_form_que_descarga_lleva_data_descarga(self):
        faltan = []
        for ruta in _plantillas():
            lineas = ruta.read_text(encoding="utf-8").splitlines()
            for numero, linea in enumerate(lineas, start=1):
                if not any(m in linea for m in MARCAS_FORM) or linea.lstrip().startswith("{#"):
                    continue
                # El form que descarga es el <form de esa línea o el más cercano arriba (acción oculta).
                for anterior in range(numero - 1, max(numero - 8, -1), -1):
                    if "<form" in lines_(lineas, anterior):
                        if "data-descarga" not in lines_(lineas, anterior):
                            faltan.append(f"{ruta.relative_to(RAIZ)}:{anterior + 1}")
                        break
        self.assertEqual(faltan, [], "forms que descargan sin data-descarga (spinner y botón bloqueado)")

    def test_las_pantallas_conocidas_siguen_cubiertas(self):
        """Si alguien renombra una URL de descarga, que la lista de marcas no quede vacía sin avisar."""
        texto = "\n".join(r.read_text(encoding="utf-8") for r in _plantillas())
        for marca in MARCAS_LINK[:4] + MARCAS_FORM:
            self.assertIn(marca, texto, f"la marca {marca!r} ya no aparece en ninguna plantilla: actualiza esta prueba")


def lines_(lineas, indice):
    """Línea `indice` (base 0) o cadena vacía fuera de rango."""
    return lineas[indice] if 0 <= indice < len(lineas) else ""
