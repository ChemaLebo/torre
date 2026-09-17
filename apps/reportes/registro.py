"""Catálogo de reportes disponibles, en el orden del índice."""
from importlib import import_module

CLAVES = [
    "existencias", "lotes", "incidencias", "danos", "ventas", "tiempos", "costos", "inventario",
    "reacomodo",
]


def modulos():
    """[(clave, módulo)] de los reportes ya implementados (los que faltan se saltan)."""
    resultado = []
    for clave in CLAVES:
        try:
            resultado.append((clave, import_module(f"apps.reportes.{clave}")))
        except ModuleNotFoundError as exc:
            if exc.name != f"apps.reportes.{clave}":
                raise
    return resultado


def modulo(clave):
    """Módulo del reporte o None si la clave no existe."""
    return dict(modulos()).get(clave)
