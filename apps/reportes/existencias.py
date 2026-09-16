"""Existencias por SKU y lote, con faltantes/sobrantes.

Una fila por (SKU, lote) con las piezas por estado (Saldo), la caducidad y
los días que faltan, la diferencia del último conteo del SKU (contado −
esperado) y la diferencia de recepción del periodo (recibido + dañado −
anunciado en las ASN descargadas entre las fechas). Sin lote = "sin lote".
"""
from collections import defaultdict

from django.db.models import Sum
from django.utils import timezone

from apps.catalogo.models import SKU, Lote
from apps.inventario.models import Conteo, LineaASN, OrdenEntrada, Saldo

CLAVE = "existencias"
TITULO = "Existencias por SKU y lote"
DESCRIPCION = (
    "Piezas por estado de cada SKU y lote, con la caducidad, la diferencia del último "
    "conteo (faltante negativo, sobrante positivo) y la diferencia entre lo anunciado y "
    "lo recibido en las entradas del periodo."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "caduca_dias", "etiqueta": "Caduca en (días)", "tipo": "number", "default": None},
    {"nombre": "con_stock", "etiqueta": "Solo con existencia", "tipo": "checkbox", "default": True},
]
COLUMNAS = [
    ("SKU", "texto"), ("Producto", "texto"), ("Lote", "texto"), ("Caducidad", "fecha"),
    ("Días para caducar", "entero"), ("Vendible", "entero"), ("Apartado", "entero"),
    ("En empaque", "entero"), ("En recepción", "entero"), ("Cuarentena", "entero"),
    ("Físico", "entero"), ("Último conteo", "fechahora"), ("Dif. conteo", "entero"),
    ("Dif. recepción (periodo)", "entero"),
]
SIN_LOTE = "sin lote"


def saldos_por_lote(cliente):
    """{(sku_id, lote_id): {estado: piezas}} del cliente (lote_id None = sin lote)."""
    saldos = defaultdict(dict)
    filas = (
        Saldo.objects.filter(sku__cliente=cliente)
        .values("sku_id", "lote_id", "estado").annotate(t=Sum("cantidad"))
    )
    for f in filas:
        saldos[(f["sku_id"], f["lote_id"])][f["estado"]] = f["t"] or 0
    return saldos


def ultimo_conteo_por_sku(cliente):
    """{sku_id: Conteo más reciente}."""
    conteos = {}
    for c in Conteo.objects.filter(sku__cliente=cliente).order_by("sku_id", "-ts", "-pk"):
        conteos.setdefault(c.sku_id, c)
    return conteos


def diferencias_recepcion(cliente, inicio, fin):
    """{(sku_id, lote_codigo): recibido + dañado − anunciado} de las entradas
    (ASN y reingresos) descargadas o cerradas en [inicio, fin)."""
    dif = defaultdict(int)
    lineas = LineaASN.objects.filter(
        orden__cliente=cliente, orden__estado__in=(OrdenEntrada.RECIBIDA, OrdenEntrada.CERRADA),
        orden__creado__gte=inicio, orden__creado__lt=fin,
    )
    for l in lineas:
        dif[(l.sku_id, (l.lote_codigo or "").strip())] += (
            l.cantidad_recibida + l.cantidad_danada - l.cantidad_anunciada
        )
    return dif


def filas_existencias(cliente, inicio, fin, filtros, solo_con_lote=False):
    """Diccionarios por (SKU, lote) ya filtrados; los reportes los proyectan a columnas."""
    hoy = timezone.localdate()
    saldos = saldos_por_lote(cliente)
    conteos = ultimo_conteo_por_sku(cliente)
    dif_rec = diferencias_recepcion(cliente, inicio, fin)
    lotes = {l.pk: l for l in Lote.objects.filter(sku__cliente=cliente)}
    claves = set(saldos) | {(l.sku_id, l.pk) for l in lotes.values()}
    filas = []
    for sku in SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo"):
        propias = sorted(
            (k for k in claves if k[0] == sku.pk),
            key=lambda k: (k[1] is not None, lotes[k[1]].fecha_caducidad or hoy.max, lotes[k[1]].codigo) if k[1] else (False, hoy, ""),
        )
        for _sku_id, lote_id in propias:
            lote = lotes.get(lote_id)
            if solo_con_lote and lote is None:
                continue
            s = saldos.get((sku.pk, lote_id), {})
            piezas = {
                "vendible": s.get(Saldo.UBICADO_VENDIBLE, 0), "apartado": s.get(Saldo.RESERVADO, 0),
                "en_empaque": s.get(Saldo.EN_EMPAQUE, 0), "en_recepcion": s.get(Saldo.EN_PUTAWAY, 0),
                "cuarentena": s.get(Saldo.CUARENTENA, 0),
            }
            fisico = sum(piezas.values())
            if filtros.get("con_stock") and fisico <= 0:
                continue
            caducidad = lote.fecha_caducidad if lote else None
            dias = (caducidad - hoy).days if caducidad else None
            if filtros.get("caduca_dias") is not None and (dias is None or dias > filtros["caduca_dias"]):
                continue
            conteo = conteos.get(sku.pk)
            filas.append({
                "sku": sku, "lote": lote, "caducidad": caducidad, "dias": dias, **piezas,
                "fisico": fisico,
                "ultimo_conteo": conteo.ts if conteo else None,
                "dif_conteo": conteo.diferencia if conteo else None,
                "dif_recepcion": dif_rec.get((sku.pk, lote.codigo if lote else ""), 0) or None,
            })
    return filas


def generar(cliente, inicio, fin, filtros, es_mesa):
    filas = filas_existencias(cliente, inicio, fin, filtros)
    faltante = sum(f["dif_conteo"] for f in filas if (f["dif_conteo"] or 0) < 0)
    sobrante = sum(f["dif_conteo"] for f in filas if (f["dif_conteo"] or 0) > 0)
    return {
        "filas": [
            [f["sku"].codigo, f["sku"].descripcion, f["lote"].codigo if f["lote"] else SIN_LOTE,
             f["caducidad"], f["dias"], f["vendible"], f["apartado"], f["en_empaque"],
             f["en_recepcion"], f["cuarentena"], f["fisico"], f["ultimo_conteo"],
             f["dif_conteo"], f["dif_recepcion"]]
            for f in filas
        ],
        "resumen": [
            ("SKUs", len({f["sku"].pk for f in filas})),
            ("piezas físicas", sum(f["fisico"] for f in filas)),
            ("faltante en conteos", faltante), ("sobrante en conteos", sobrante),
        ],
    }
