"""Fechas capturadas a mano en CSV (caducidades de ASN y de conteo).

El sistema escribe AAAA-MM-DD, pero Excel en es-MX guarda las celdas de fecha
como DD/MM/AAAA y la gente teclea DD-MM-AAAA: se aceptan las tres. El texto
PLACEHOLDER_FECHA es el que viaja prellenado en las plantillas descargables
para enseñar el formato; si lo dejan sin tocar cuenta como vacío.
"""
import re
from datetime import date

PLACEHOLDER_FECHA = "AAAA-MM-DD"
FORMATOS_LEGIBLES = "AAAA-MM-DD o DD/MM/AAAA"
_DIA_MES_ANIO = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")


def parsear_fecha_csv(texto):
    """date, o None si viene vacío o con el placeholder. ValueError si no es
    AAAA-MM-DD, DD/MM/AAAA ni DD-MM-AAAA."""
    crudo = (texto or "").strip()
    if not crudo or crudo.upper() == PLACEHOLDER_FECHA:
        return None
    try:
        return date.fromisoformat(crudo)
    except ValueError:
        pass
    partes = _DIA_MES_ANIO.match(crudo)
    if partes:
        dia, mes, anio = (int(p) for p in partes.groups())
        try:
            return date(anio, mes, dia)
        except ValueError:
            pass
    raise ValueError(f"'{crudo}' no es una fecha {FORMATOS_LEGIBLES}.")
