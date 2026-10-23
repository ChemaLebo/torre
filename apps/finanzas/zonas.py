"""Zona de cobro (local | metro | nacional) por CP destino.

Desde 2026-10-22 la fuente es `configuracion.ZonaCP` del origen principal
(editable en el admin, cargada con `manage.py cargar_zonas_cp`); mientras esa
tabla no tenga renglones activos se lee `config/zonas_cp.csv` como respaldo
(rangos inclusivos de CP con su zona, armados con las bandas de 99minutos,
Chema 2026-09-28). Lo que no cae en ningún rango es nacional. Solo para cobrar
y para reportes: el ruteo y la cotización no lo usan. Caché por proceso que se
invalida al guardar o borrar rangos u orígenes (`finanzas.apps`).
"""
import csv
from functools import lru_cache
from pathlib import Path

from django.conf import settings

ZONAS = ("local", "metro", "nacional")
ZONA_DEFAULT = "nacional"


def _archivo():
    return Path(settings.TORRE.get("ZONAS_CP_ARCHIVO") or (settings.BASE_DIR / "config" / "zonas_cp.csv"))


def rangos_de_archivo(ruta=None):
    """[(desde, hasta, zona, ciudad)] del CSV (columnas cp_desde, cp_hasta,
    zona y, opcional, ciudad; líneas con # se ignoran); vacío si no existe.
    Las filas que no se entienden se saltan."""
    ruta = Path(ruta) if ruta else _archivo()
    if not ruta.exists():
        return []
    filas = []
    with ruta.open(encoding="utf-8-sig") as f:
        lineas = (linea for linea in f if linea.strip() and not linea.lstrip().startswith("#"))
        for fila in csv.DictReader(lineas):
            zona = (fila.get("zona") or "").strip().lower()
            try:
                desde, hasta = int(fila["cp_desde"]), int(fila["cp_hasta"])
            except (KeyError, TypeError, ValueError):
                continue
            if zona in ZONAS and desde <= hasta:
                filas.append((desde, hasta, zona, (fila.get("ciudad") or "").strip()))
    return filas


def _rangos_de_tabla():
    """Rangos activos del origen principal (o del único activo) en configuracion.ZonaCP."""
    from apps.configuracion.models import Origen, ZonaCP  # lazy: modelo de otra app

    origen = Origen.objects.filter(activo=True).order_by("-principal", "pk").first()
    if origen is None:
        return []
    return [
        (int(z.cp_desde), int(z.cp_hasta), z.zona, z.ciudad)
        for z in ZonaCP.objects.filter(origen=origen, activo=True).order_by("cp_desde", "cp_hasta")
    ]


@lru_cache(maxsize=1)
def rangos():
    """[(desde, hasta, zona, ciudad)]: la tabla si tiene renglones, si no el archivo."""
    return _rangos_de_tabla() or rangos_de_archivo()


def recargar():
    """Olvida lo leído (pruebas, cambios en el admin y cambios en caliente)."""
    rangos.cache_clear()


def invalidar(**_kwargs):
    """Receptor de post_save/post_delete de Origen y ZonaCP (ver finanzas.apps)."""
    recargar()


def _rango_de(cp):
    cp = "".join(ch for ch in str(cp or "") if ch.isdigit())
    if len(cp) != 5:
        return None
    numero = int(cp)
    for desde, hasta, zona, ciudad in rangos():
        if desde <= numero <= hasta:
            return zona, ciudad
    return ZONA_DEFAULT, ""


def zona_de_cp(cp):
    """Zona de cobro del CP; None si no hay CP válido (el caller decide el respaldo)."""
    rango = _rango_de(cp)
    return rango[0] if rango else None


def ciudad_de_cp(cp):
    """Etiqueta de ciudad del rango que cubre el CP (Querétaro, Guadalajara…); "" si el
    rango no la tiene o el CP cae fuera de todo rango; None sin CP válido."""
    rango = _rango_de(cp)
    return rango[1] if rango else None
