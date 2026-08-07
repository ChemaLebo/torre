"""Acceso al índice de manuales pre-renderizados (templates/manuales/_indice.json).

Los partials y el índice los genera `manage.py render_manuales` y viven en el
repo: las vistas de mesa y portal (y los tests) leen de aquí, nunca de la
carpeta fuente de .md.
"""
import json
from pathlib import Path

from django.conf import settings

CODIGO_PORTADA = "README"


def _ruta_indice():
    return Path(settings.BASE_DIR) / "templates" / "manuales" / "_indice.json"


def cargar_indice():
    """Lista completa del índice (incluye la portada README). [] si no existe."""
    try:
        with open(_ruta_indice(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def manuales_publicados():
    """Los SOPs del índice, sin la portada."""
    return [m for m in cargar_indice() if m["codigo"] != CODIGO_PORTADA]


def portada():
    """La entrada README (portada/intro del índice) o None."""
    return next((m for m in cargar_indice() if m["codigo"] == CODIGO_PORTADA), None)


def obtener_manual(slug):
    """Entrada del índice por slug, o None si no existe."""
    return next((m for m in cargar_indice() if m["slug"] == slug), None)


def agrupar(manuales):
    """[{nombre, manuales}] respetando el orden del índice (grupos contiguos)."""
    grupos = []
    for manual in manuales:
        if not grupos or grupos[-1]["nombre"] != manual["grupo"]:
            grupos.append({"nombre": manual["grupo"], "manuales": []})
        grupos[-1]["manuales"].append(manual)
    return grupos
