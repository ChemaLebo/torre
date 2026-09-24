"""Daños en la entrega: control del servicio ofrecido.

Tabla principal por paquetería: pedidos entregados en el periodo (fecha de
entrega), cuántos tuvieron incidencia de daño (DAN) y el porcentaje. Grupo
con cada pedido dañado: cuándo se entregó, cuándo se reportó y cuántas
horas después, origen, estado, fotos y compensación.
"""
from collections import defaultdict
from decimal import Decimal

from apps.incidencias.models import Incidencia
from apps.pedidos.models import Pedido

from .base import dinero, horas
from .incidencias import compensaciones_por_incidencia, fotos_por_incidencia

CLAVE = "danos"
TITULO = "Daños en la entrega"
DESCRIPCION = (
    "Pedidos entregados en el periodo y cuántos llegaron con daño (incidencias de daño o "
    "rotura), por paquetería y con el porcentaje de servicio; abajo, cada pedido dañado "
    "con su incidencia."
)
CON_FECHAS = True
FILTROS = []
COLUMNAS = [
    ("Paquetería", "texto"), ("Entregados", "entero"), ("Con daño", "entero"),
    ("% con daño", "pct"), ("Compensado MXN", "dinero"),
]
COLUMNAS_DETALLE = [
    ("Pedido", "texto"), ("Entregado", "fechahora"), ("Paquetería", "texto"), ("Incidencia", "texto"),
    ("Reportada", "fechahora"), ("Horas tras la entrega", "horas"), ("Origen", "texto"),
    ("Estado", "texto"), ("Fotos", "entero"), ("Compensación MXN", "dinero"),
]


def carrier_de(pedido):
    """Carrier de la guía activa (la última si hay varias); "local" o "—" sin guía."""
    activas = [g for g in pedido.guias.all() if g.es_activa]
    return activas[-1].carrier if activas else "—"


def generar(cliente, inicio, fin, filtros, es_mesa):
    entregados = list(
        Pedido.objects.filter(cliente=cliente, ts_entregado__gte=inicio, ts_entregado__lt=fin)
        .prefetch_related("guias").order_by("ts_entregado")
    )
    por_pk = {p.pk: p for p in entregados}
    danos = list(
        Incidencia.objects.filter(tipo=Incidencia.TIPO_DAN, pedido_id__in=list(por_pk), interna=False)
        .order_by("ts_apertura")
    )
    comp = compensaciones_por_incidencia(danos)
    fotos = fotos_por_incidencia(danos)
    por_carrier = defaultdict(lambda: {"entregados": 0, "danados": set(), "compensado": Decimal(0)})
    for p in entregados:
        por_carrier[carrier_de(p)]["entregados"] += 1
    detalle = []
    for inc in danos:
        pedido = por_pk[inc.pedido_id]
        carrier = carrier_de(pedido)
        monto = sum((c.monto for c in comp.get(inc.pk, [])), Decimal(0))
        por_carrier[carrier]["danados"].add(pedido.pk)
        por_carrier[carrier]["compensado"] += monto
        detalle.append([
            pedido.folio, pedido.ts_entregado, carrier, inc.folio, inc.ts_apertura,
            horas(pedido.ts_entregado, inc.ts_apertura), inc.get_origen_display(),
            inc.get_estado_display(), fotos.get(inc.pk, 0), dinero(monto) if comp.get(inc.pk) else None,
        ])
    filas = []
    for carrier, d in sorted(por_carrier.items()):
        n, k = d["entregados"], len(d["danados"])
        filas.append([carrier, n, k, round(k * 100 / n, 1) if n else None, dinero(d["compensado"])])
    total_n = len(entregados)
    total_k = len({inc.pedido_id for inc in danos})
    if filas:
        filas.append([
            "Total", total_n, total_k, round(total_k * 100 / total_n, 1) if total_n else None,
            dinero(sum((d["compensado"] for d in por_carrier.values()), Decimal(0))),
        ])
    return {
        "filas": filas,
        "resumen": [("entregados", total_n), ("con daño", total_k)],
        "grupos": [{"titulo": "Pedidos con daño", "columnas": COLUMNAS_DETALLE, "filas": detalle}],
    }
