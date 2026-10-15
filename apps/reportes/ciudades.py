"""Pedidos por ciudad (Chema 2026-10-15): el mismo universo que "Pedidos por
código postal" (`destinos.acumular`) juntado por ciudad: el municipio del
catálogo de localidades, la Ciudad de México como una sola, y sin catálogo la
ciudad de Shopify limpia (la misma ciudad escrita con y sin acentos o
mayúsculas cae en un solo renglón). Zona = la que lleva más paquetes.

Mesa ve además lo pagado a 99minutos, la tarifa estimada (real de la ciudad;
si no, promedio del estado en esa zona; si no, del estado; si no, vacía) y
"Costo estimado si todo fuera 99" = paquetes × tarifa estimada: lo que
costaría mandar TODO el volumen de la ciudad con 99minutos a la tarifa vista.
"""
from collections import Counter

from . import destinos
from .base import dinero

CLAVE = "ciudades"
TITULO = "Pedidos por ciudad"
DESCRIPCION = (
    "Pedidos creados en el periodo que ya salieron, por ciudad de destino: códigos postales, pedidos y paquetes "
    "(cajas despachadas, sin reposiciones). Mesa ve además lo pagado a 99minutos, la tarifa estimada y lo que "
    "costaría mandar todo el volumen de la ciudad con 99minutos."
)
CON_FECHAS = True
FILTROS = destinos.FILTROS
COLUMNAS = [
    ("Ciudad", "texto"), ("Estado", "texto"), ("Zona", "texto"), ("Códigos postales", "entero"),
    ("Pedidos", "entero"), ("Paquetes", "entero"),
]
COLUMNAS_MESA = [
    ("Paquetes 99minutos", "entero"), ("Costo 99 promedio", "dinero"), ("Costo 99 total", "dinero"),
    ("Tarifa 99 estimada", "dinero"), ("Base de la estimación", "texto"), ("Costo estimado si todo fuera 99", "dinero"),
]


def agrupar(por_cp):
    """{clave: ciudad agregada} desde los acumulados por CP de `destinos.acumular`."""
    grupos = {}
    for a in por_cp.values():
        g = grupos.get(a["clave"])
        if g is None:
            g = grupos[a["clave"]] = {
                "estado": a["estado"], "nombres": Counter(), "catalogo": set(), "zonas": Counter(),
                "cps": 0, "pedidos": 0, "paquetes": 0, "paquetes_99": 0, "costos": [],
            }
        g["nombres"][a["grupo"]] += a["pedidos"]
        if a["de_catalogo"]:
            g["catalogo"].add(a["grupo"])
        g["zonas"][a["zona"]] += a["paquetes"]
        g["cps"] += 1
        for campo in ("pedidos", "paquetes", "paquetes_99"):
            g[campo] += a[campo]
        g["costos"] += a["costos"]
    return grupos


def nombre(g):
    """El nombre del catálogo si algún CP lo tiene; si no, el que más compradores escribieron."""
    candidatos = g["catalogo"] or set(g["nombres"])
    return min(candidatos, key=lambda n: (-g["nombres"][n], n))


def zona_predominante(g):
    """La zona con más paquetes de la ciudad ("" si no hay)."""
    return min(g["zonas"].items(), key=lambda kv: (-kv[1], kv[0]))[0] if g["zonas"] else ""


def generar(cliente, inicio, fin, filtros, es_mesa):
    por_cp = destinos.acumular(cliente, inicio, fin, filtros)
    _por_ciudad, por_estado_zona, por_estado = destinos.promedios_observados(por_cp)
    grupos = agrupar(por_cp)
    filas = []
    total = {"cps": 0, "pedidos": 0, "paquetes": 0, "paquetes_99": 0, "costos": [], "estimado": None}
    for g in sorted(grupos.values(), key=lambda g: (-g["pedidos"], -g["paquetes"], nombre(g))):
        zona = zona_predominante(g)
        fila = [nombre(g), g["estado"], zona, g["cps"], g["pedidos"], g["paquetes"]]
        if es_mesa:
            tarifa, base = destinos.estimar((
                (g["costos"], "real de la ciudad"),
                (por_estado_zona[(g["estado"], zona)] if g["estado"] else [], "promedio del estado y zona"),
                (por_estado[g["estado"]] if g["estado"] else [], "promedio del estado"),
            ))
            estimado = dinero(tarifa * g["paquetes"]) if tarifa is not None else None
            fila += [g["paquetes_99"], destinos.promedio_dinero(g["costos"]), destinos.total_dinero(g["costos"]),
                     tarifa, base, estimado]
            if estimado is not None:
                total["estimado"] = (total["estimado"] or 0) + estimado
        filas.append(fila)
        for campo in ("cps", "pedidos", "paquetes", "paquetes_99"):
            total[campo] += g[campo]
        total["costos"] += g["costos"]
    if filas:
        fila = ["Total", "", "", total["cps"], total["pedidos"], total["paquetes"]]
        if es_mesa:
            fila += [total["paquetes_99"], destinos.promedio_dinero(total["costos"]),
                     destinos.total_dinero(total["costos"]), None, "", total["estimado"]]
        filas.append(fila)
    resumen = [("pedidos", total["pedidos"]), ("paquetes", total["paquetes"]), ("ciudades", len(grupos))]
    if es_mesa:
        resumen += [("pagado a 99minutos", destinos.total_dinero(total["costos"]) or 0),
                    ("costo estimado si todo fuera 99", total["estimado"] or 0)]
    return {"filas": filas, "resumen": resumen, "grupos": []}
