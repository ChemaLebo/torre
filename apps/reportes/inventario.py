"""Inventario por bodega: por SKU, disponible (vendible − apartado − buffer),
piezas por estado, en tránsito hacia la bodega (anunciado en ASN aún no
recibido) y alerta de punto de reorden. Una sola bodega (Torre) hasta que
exista otra; la columna "en búsqueda" que pidió Colima queda pendiente de
que definan el término.
"""
from collections import defaultdict

from apps.catalogo.models import SKU
from apps.inventario.models import LineaASN, OrdenEntrada

CLAVE = "inventario"
TITULO = "Inventario por bodega"
DESCRIPCION = (
    "Por producto: disponible para vender, piezas por estado en bodega, lo que viene en camino "
    "(anunciado en entradas que aún no llegan) y la alerta de punto de reorden."
)
CON_FECHAS = False
FILTROS = [
    {"nombre": "solo_alertas", "etiqueta": "Solo con alerta", "tipo": "checkbox", "default": False},
]
COLUMNAS = [
    ("SKU", "texto"), ("Producto", "texto"), ("Bodega", "texto"), ("Disponible", "entero"),
    ("Vendible", "entero"), ("Apartado", "entero"), ("En empaque", "entero"), ("En recepción", "entero"),
    ("Cuarentena", "entero"), ("En tránsito", "entero"), ("Buffer", "entero"), ("Punto de reorden", "entero"),
    ("Alerta", "texto"),
]
BODEGA = "Torre"


def en_transito_por_sku(cliente):
    """{sku_id: piezas anunciadas en ASN que aún no se reciben}."""
    pendiente = defaultdict(int)
    lineas = LineaASN.objects.filter(
        orden__cliente=cliente, orden__tipo=OrdenEntrada.TIPO_ASN,
        orden__estado__in=(OrdenEntrada.ANUNCIADA, OrdenEntrada.EN_RECEPCION),
    )
    for l in lineas:
        pendiente[l.sku_id] += max(l.cantidad_anunciada - l.cantidad_recibida - l.cantidad_danada, 0)
    return pendiente


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.inventario.services import resumen_sku  # lazy por contrato

    transito = en_transito_por_sku(cliente)
    filas, alertas = [], 0
    for sku in SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo"):
        r = resumen_sku(sku)
        if sku.punto_reorden and r["disponible"] <= sku.punto_reorden:
            alerta = "bajo punto de reorden"
        elif r["fisico"] == 0 and not transito.get(sku.pk):
            alerta = "sin existencia"
        else:
            alerta = ""
        if filtros.get("solo_alertas") and not alerta:
            continue
        alertas += bool(alerta)
        filas.append([
            sku.codigo, sku.descripcion, BODEGA, r["disponible"], r["vendible"], r["apartado"],
            r["en_empaque"], r["en_recepcion"], r["cuarentena"], transito.get(sku.pk, 0),
            cliente.buffer_stock or 0, sku.punto_reorden or 0, alerta,
        ])
    return {
        "filas": filas,
        "resumen": [
            ("SKUs", len(filas)), ("disponibles", sum(f[3] for f in filas)),
            ("en tránsito", sum(f[9] for f in filas)), ("con alerta", alertas),
        ],
    }
