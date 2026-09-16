"""Tiempos logísticos: cada hora del pedido desde que se creó hasta que se
entregó, con las duraciones entre pasos, y promedios por estado destino,
zona de facturación y paquetería.

Las horas de bodega son las del pedido (ts_* que estampa transicionar); las
del carrier salen de EventoGuia (hora reportada por la paquetería): primer
evento del carrier, "en ruta" y la entrega. Región = estado por CP
(finanzas.estado_de_cp) y zona local/metro/nacional (finanzas.zona_de_cp).
"""
from collections import defaultdict

from apps.envios.models import Guia
from apps.pedidos.models import Pedido

from .base import horas, promedio

CLAVE = "tiempos"
TITULO = "Tiempos logísticos"
DESCRIPCION = (
    "Horarios de cada paso de los pedidos creados en el periodo (recibido, picking, empacado, "
    "guía, salida, paquetería, en ruta, entrega) y las horas entre ellos; abajo, los promedios "
    "por estado destino, zona y paquetería."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "solo_entregados", "etiqueta": "Solo entregados", "tipo": "checkbox", "default": False},
]
COLUMNAS = [
    ("Pedido", "texto"), ("Paquetería", "texto"), ("Estado destino", "texto"), ("Zona", "texto"),
    ("Recibido", "fechahora"), ("Picking", "fechahora"), ("Empacado", "fechahora"), ("Guía", "fechahora"),
    ("Salida de bodega", "fechahora"), ("1er evento paquetería", "fechahora"), ("En tránsito", "fechahora"),
    ("En ruta", "fechahora"), ("Entregado", "fechahora"), ("Horas en bodega", "horas"),
    ("Horas en tránsito", "horas"), ("Horas totales", "horas"),
]
COLUMNAS_PROMEDIO = [
    ("Grupo", "texto"), ("Pedidos", "entero"), ("Entregados", "entero"), ("Prom. horas en bodega", "horas"),
    ("Prom. horas en tránsito", "horas"), ("Prom. horas totales", "horas"),
]


def _eventos_carrier(pedido):
    """(primer evento, primer EN_RUTA) con hora del carrier entre las guías activas."""
    primero, en_ruta = None, None
    for guia in pedido.guias.all():
        if not guia.es_activa:
            continue
        for e in guia.eventos.all():
            if e.ts_carrier is None:
                continue
            if primero is None or e.ts_carrier < primero:
                primero = e.ts_carrier
            if e.estado == Guia.EN_RUTA and (en_ruta is None or e.ts_carrier < en_ruta):
                en_ruta = e.ts_carrier
    return primero, en_ruta


def _promedios(titulo, grupos):
    filas = []
    for nombre, pedidos in sorted(grupos.items()):
        filas.append([
            nombre, len(pedidos), sum(1 for p in pedidos if p["entregado"]),
            promedio([p["bodega"] for p in pedidos]), promedio([p["transito"] for p in pedidos]),
            promedio([p["total"] for p in pedidos]),
        ])
    return {"titulo": titulo, "columnas": COLUMNAS_PROMEDIO, "filas": filas}


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.mesa.finanzas import (  # lazy por contrato
        SIN_ESTADO,
        estado_de_cp,
        zona_de_cp,
    )

    qs = (
        Pedido.objects.filter(cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .exclude(estado__in=(Pedido.CANCELADO, Pedido.CANCELACION_PENDIENTE))
        .prefetch_related("guias__eventos").order_by("creado")
    )
    if filtros.get("solo_entregados"):
        qs = qs.filter(ts_entregado__isnull=False)
    filas, por_estado, por_zona, por_carrier = [], defaultdict(list), defaultdict(list), defaultdict(list)
    for p in qs:
        activas = [g for g in p.guias.all() if g.es_activa]
        carrier = activas[-1].carrier if activas else "—"
        estado = estado_de_cp(p.cp) or SIN_ESTADO
        zona = zona_de_cp(p.cp) or "—"
        primero, en_ruta = _eventos_carrier(p)
        medidas = {
            "bodega": horas(p.creado, p.ts_recolectado), "transito": horas(p.ts_recolectado, p.ts_entregado),
            "total": horas(p.creado, p.ts_entregado), "entregado": p.ts_entregado is not None,
        }
        filas.append([
            p.folio, carrier, estado, zona, p.creado, p.ts_picking, p.ts_empacado, p.ts_guia,
            p.ts_recolectado, primero, p.ts_en_transito, en_ruta, p.ts_entregado,
            medidas["bodega"], medidas["transito"], medidas["total"],
        ])
        por_estado[estado].append(medidas)
        por_zona[zona].append(medidas)
        por_carrier[carrier].append(medidas)
    entregados = sum(1 for f in filas if f[12] is not None)
    return {
        "filas": filas,
        "resumen": [
            ("pedidos", len(filas)), ("entregados", entregados),
            ("prom. horas en bodega", promedio([f[13] for f in filas]) or 0),
            ("prom. horas totales", promedio([f[15] for f in filas]) or 0),
        ],
        "grupos": [
            _promedios("Promedios por estado destino", por_estado),
            _promedios("Promedios por zona", por_zona),
            _promedios("Promedios por paquetería", por_carrier),
        ],
    }
