"""Zona de cobro (local | metro | nacional) por CP destino, leída de
config/zonas_cp.csv (Chema 2026-09-28): rangos inclusivos de CP con su zona,
armados con las bandas de 99minutos. Lo que no cae en ningún rango es
nacional. Solo para cobrar (finanzas, reporte de costos): el ruteo y la
cotización no lo usan. El archivo se lee una vez por proceso.
"""
import csv
from functools import lru_cache
from pathlib import Path

from django.conf import settings

ZONAS = ("local", "metro", "nacional")
ZONA_DEFAULT = "nacional"


def _archivo():
    return Path(settings.TORRE.get("ZONAS_CP_ARCHIVO") or (settings.BASE_DIR / "config" / "zonas_cp.csv"))


@lru_cache(maxsize=1)
def rangos():
    """[(desde, hasta, zona)] en el orden del archivo; vacío si no existe."""
    ruta = _archivo()
    if not ruta.exists():
        return []
    filas = []
    with ruta.open(encoding="utf-8") as f:
        lineas = (l for l in f if l.strip() and not l.lstrip().startswith("#"))
        for fila in csv.DictReader(lineas):
            zona = (fila.get("zona") or "").strip().lower()
            try:
                desde, hasta = int(fila["cp_desde"]), int(fila["cp_hasta"])
            except (KeyError, TypeError, ValueError):
                continue
            if zona in ZONAS and desde <= hasta:
                filas.append((desde, hasta, zona))
    return filas


def recargar():
    """Olvida el archivo leído (pruebas y cambios en caliente)."""
    rangos.cache_clear()


def zona_de_cp(cp):
    """Zona de cobro del CP; None si no hay CP válido (el caller decide el respaldo)."""
    cp = "".join(ch for ch in str(cp or "") if ch.isdigit())
    if len(cp) != 5:
        return None
    numero = int(cp)
    for desde, hasta, zona in rangos():
        if desde <= numero <= hasta:
            return zona
    return ZONA_DEFAULT
