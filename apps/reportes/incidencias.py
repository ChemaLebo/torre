"""Incidencias del periodo y la solución de nuestro lado.

Una fila por incidencia abierta en el rango: pedido y SKU afectados, tipo,
origen, prioridad, estado, sus tiempos (primera respuesta, resolución,
cierre), la solución (compensaciones: reposición, reembolso o cupón con
monto y estado), la reclamación al carrier y las fotos adjuntas. Mesa ve
además quién tiene la pelota.
"""
from collections import defaultdict
from decimal import Decimal

from apps.core.models import EvidenciaFoto
from apps.incidencias.models import Compensacion, Incidencia, ReclamacionCarrier

from .base import dinero

CLAVE = "incidencias"
TITULO = "Incidencias y su solución"
DESCRIPCION = (
    "Incidencias abiertas en el periodo con el pedido afectado, sus tiempos y lo que "
    "resolvimos de nuestro lado: reposición, reembolso o código de descuento, y la "
    "reclamación a la paquetería cuando aplica."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "tipo", "etiqueta": "Tipo", "tipo": "select",
     "opciones": [("", "Todos")] + list(Incidencia.TIPOS), "default": ""},
    {"nombre": "abiertas", "etiqueta": "Solo abiertas", "tipo": "checkbox", "default": False},
]
COLUMNAS = [
    ("Folio", "texto"), ("Apertura", "fechahora"), ("Pedido", "texto"), ("SKU", "texto"),
    ("Tipo", "texto"), ("Origen", "texto"), ("Prioridad", "texto"), ("Estado", "texto"),
    ("Primera respuesta", "fechahora"), ("Resolución", "fechahora"), ("Cierre", "fechahora"),
    ("Solución", "texto"), ("Compensación MXN", "dinero"), ("Reclamación al carrier", "texto"),
    ("Fotos", "entero"),
]
COLUMNAS_MESA = [("Dueño", "texto")]


def compensaciones_por_incidencia(incidencias):
    """{incidencia_id: [Compensacion]}."""
    por = defaultdict(list)
    for c in Compensacion.objects.filter(incidencia__in=incidencias).order_by("pk"):
        por[c.incidencia_id].append(c)
    return por


def reclamaciones_por_incidencia(incidencias):
    por = defaultdict(list)
    for r in ReclamacionCarrier.objects.filter(incidencia__in=incidencias).order_by("pk"):
        por[r.incidencia_id].append(r)
    return por


def fotos_por_incidencia(incidencias):
    """{folio o pk: n fotos} (la captura liga por folio o por pk según el punto)."""
    claves = []
    for inc in incidencias:
        claves += [inc.folio, str(inc.pk)]
    conteo = defaultdict(int)
    if claves:
        for f in EvidenciaFoto.objects.filter(entidad="incidencia", entidad_id__in=claves):
            conteo[f.entidad_id] += 1
    return {inc.pk: conteo.get(inc.folio, 0) + conteo.get(str(inc.pk), 0) for inc in incidencias}


def texto_solucion(compensaciones):
    """"reposición $300.00 (aprobada); cupón $50.00 (pagada)" o "sin compensación"."""
    if not compensaciones:
        return "sin compensación"
    return "; ".join(
        f"{c.get_tipo_display().lower()} ${c.monto:,.2f} ({c.get_estado_display().lower()})"
        for c in compensaciones
    )


def texto_reclamacion(reclamaciones):
    if not reclamaciones:
        return ""
    return "; ".join(
        f"{r.carrier} ${r.monto_reclamado:,.2f} ({r.get_estado_display().lower()})"
        for r in reclamaciones
    )


def generar(cliente, inicio, fin, filtros, es_mesa):
    qs = (
        Incidencia.objects.filter(cliente=cliente, ts_apertura__gte=inicio, ts_apertura__lt=fin)
        .select_related("pedido", "sku").order_by("ts_apertura", "pk")
    )
    if filtros.get("tipo"):
        qs = qs.filter(tipo=filtros["tipo"])
    if filtros.get("abiertas"):
        qs = qs.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
    incidencias = list(qs)
    comp = compensaciones_por_incidencia(incidencias)
    recl = reclamaciones_por_incidencia(incidencias)
    fotos = fotos_por_incidencia(incidencias)
    filas, por_tipo, compensado, con_comp = [], defaultdict(int), Decimal(0), 0
    for inc in incidencias:
        monto = sum((c.monto for c in comp.get(inc.pk, [])), Decimal(0))
        if comp.get(inc.pk):
            con_comp += 1
        compensado += monto
        por_tipo[inc.get_tipo_display()] += 1
        fila = [
            inc.folio, inc.ts_apertura, inc.pedido.folio if inc.pedido_id else "",
            inc.sku.codigo if inc.sku_id else "", inc.get_tipo_display(), inc.get_origen_display(),
            inc.get_prioridad_display(), inc.get_estado_display(), inc.ts_primera_respuesta,
            inc.ts_resolucion, inc.ts_cierre, texto_solucion(comp.get(inc.pk, [])),
            dinero(monto) if comp.get(inc.pk) else None, texto_reclamacion(recl.get(inc.pk, [])),
            fotos.get(inc.pk, 0),
        ]
        if es_mesa:
            fila.append(inc.dueno)
        filas.append(fila)
    abiertas = sum(1 for inc in incidencias if inc.estado in Incidencia.ESTADOS_ABIERTOS)
    resumen = [
        ("incidencias", len(incidencias)), ("abiertas", abiertas),
        ("con compensación", con_comp), ("MXN compensados", dinero(compensado)),
    ] + [(tipo.lower(), n) for tipo, n in sorted(por_tipo.items())]
    return {"filas": filas, "resumen": resumen}
