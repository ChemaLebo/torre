"""Incidencias por paquetería y zona (Chema 2026-09-30): para negociar con los
carriers con números.

Tabla principal por paquetería × zona (local, metro, nacional según el CP del
pedido): guías creadas en el periodo, entregadas, pedidos con incidencia y de
qué tipo (daño, retraso, no entregado, otras), guías sustituidas por una
reposición, entregas duplicadas (la guía sustituida acabó entregándose) y el
porcentaje de guías con incidencia. Grupo con cada incidencia y su guía.
"""
from collections import defaultdict

from apps.core.models import EventoAuditoria
from apps.envios.models import Guia
from apps.incidencias.models import Incidencia

CLAVE = "carriers"
TITULO = "Incidencias por paquetería y zona"
DESCRIPCION = (
    "Guías creadas en el periodo por paquetería y zona del destino, cuántas se entregaron y cuántas "
    "acabaron en incidencia (daño, retraso, no entregado u otra), más las guías sustituidas por una "
    "reposición y las entregas duplicadas. Para hablar con cada paquetería con números."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "select", "default": "",
     "opciones": [("", "Todas"), ("local", "Local"), ("metro", "Metro"), ("nacional", "Nacional")]},
]
COLUMNAS = [
    ("Paquetería", "texto"), ("Zona", "texto"), ("Guías", "entero"), ("Entregadas", "entero"),
    ("Con incidencia", "entero"), ("% con incidencia", "pct"), ("Daño", "entero"), ("Retraso", "entero"),
    ("No entregado", "entero"), ("Otras", "entero"), ("Sustituidas", "entero"), ("Entregas duplicadas", "entero"),
]
COLUMNAS_DETALLE = [
    ("Incidencia", "texto"), ("Abierta", "fechahora"), ("Pedido", "texto"), ("Paquetería", "texto"),
    ("Zona", "texto"), ("Guía", "texto"), ("Tipo", "texto"), ("Origen", "texto"), ("Estado", "texto"),
    ("Guía sustituida", "texto"),
]

_TIPOS = {
    Incidencia.TIPO_DAN: "dano",
    Incidencia.TIPO_RET: "retraso",
    Incidencia.TIPO_RF: "no_entregado",
}


def _zona(pedido, carrier):
    from apps.mesa.finanzas import zona_de_cp, zona_de_carrier  # lazy por contrato

    return zona_de_cp(pedido.cp) or zona_de_carrier(carrier)


def _fila_vacia():
    return {"guias": 0, "entregadas": 0, "con_incidencia": set(), "dano": 0, "retraso": 0,
            "no_entregado": 0, "otras": 0, "sustituidas": 0, "duplicadas": 0}


def generar(cliente, inicio, fin, filtros, es_mesa):
    zona_filtro = (filtros or {}).get("zona") or ""
    guias = list(
        Guia.objects.filter(pedido__cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .exclude(estado=Guia.CANCELADA).select_related("pedido").order_by("creado", "pk")
    )
    duplicadas = set(
        EventoAuditoria.objects.filter(
            entidad="guia", accion="entrega_duplicada", entidad_id__in=[str(g.pk) for g in guias],
        ).values_list("entidad_id", flat=True)
    )
    celdas = defaultdict(_fila_vacia)
    guia_por_pedido = {}
    for g in guias:
        zona = _zona(g.pedido, g.carrier)
        if zona_filtro and zona != zona_filtro:
            continue
        clave = (g.carrier, zona)
        celda = celdas[clave]
        celda["guias"] += 1
        celda["entregadas"] += g.estado == Guia.ENTREGADO
        celda["sustituidas"] += bool(g.sustituida_motivo)
        celda["duplicadas"] += str(g.pk) in duplicadas
        # La incidencia del pedido se atribuye a la guía viva más reciente de ese pedido.
        if g.es_activa or g.pedido_id not in guia_por_pedido:
            guia_por_pedido[g.pedido_id] = (g, clave)
    qs = Incidencia.objects.filter(
        cliente=cliente, pedido_id__in=list(guia_por_pedido), ts_apertura__gte=inicio, ts_apertura__lt=fin,
    ).select_related("pedido").order_by("ts_apertura", "pk")
    if not es_mesa:
        qs = qs.filter(interna=False)
    detalle = []
    for inc in qs:
        g, clave = guia_por_pedido[inc.pedido_id]
        celda = celdas[clave]
        celda["con_incidencia"].add(inc.pedido_id)
        celda[_TIPOS.get(inc.tipo, "otras")] += 1
        detalle.append([
            inc.folio, inc.ts_apertura, inc.pedido.folio, clave[0], clave[1], g.numero, inc.get_tipo_display(),
            inc.get_origen_display(), inc.get_estado_display(),
            g.get_sustituida_motivo_display().lower() if g.sustituida_motivo else "",
        ])
    filas, totales = [], _fila_vacia()
    for (carrier, zona), c in sorted(celdas.items()):
        k = len(c["con_incidencia"])
        filas.append([
            carrier, zona, c["guias"], c["entregadas"], k, round(k * 100 / c["guias"], 1) if c["guias"] else None,
            c["dano"], c["retraso"], c["no_entregado"], c["otras"], c["sustituidas"], c["duplicadas"],
        ])
        for campo in ("guias", "entregadas", "dano", "retraso", "no_entregado", "otras", "sustituidas", "duplicadas"):
            totales[campo] += c[campo]
        totales["con_incidencia"] |= c["con_incidencia"]
    if filas:
        k = len(totales["con_incidencia"])
        filas.append([
            "Total", "", totales["guias"], totales["entregadas"], k,
            round(k * 100 / totales["guias"], 1) if totales["guias"] else None,
            totales["dano"], totales["retraso"], totales["no_entregado"], totales["otras"],
            totales["sustituidas"], totales["duplicadas"],
        ])
    return {
        "filas": filas,
        "resumen": [("guías", totales["guias"]), ("entregadas", totales["entregadas"]),
                    ("pedidos con incidencia", len(totales["con_incidencia"])),
                    ("sustituidas", totales["sustituidas"]), ("entregas duplicadas", totales["duplicadas"])],
        "grupos": [{"titulo": "Incidencias del periodo", "columnas": COLUMNAS_DETALLE, "filas": detalle}],
    }
