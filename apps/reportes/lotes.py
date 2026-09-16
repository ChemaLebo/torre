"""Lotes y fecha de caducidad por producto en bodega: la proyección por lote
de existencias.py, ordenada por caducidad, con filtro de próximos a caducar."""
from .existencias import filas_existencias

CLAVE = "lotes"
TITULO = "Lotes y caducidad por producto"
DESCRIPCION = (
    "Cada lote en bodega con su fecha de caducidad, los días que le quedan y sus piezas "
    "por estado. Filtra los que caducan en los próximos N días."
)
CON_FECHAS = False
FILTROS = [
    {"nombre": "caduca_dias", "etiqueta": "Caduca en (días)", "tipo": "number", "default": None},
    {"nombre": "con_stock", "etiqueta": "Solo con existencia", "tipo": "checkbox", "default": True},
]
COLUMNAS = [
    ("SKU", "texto"), ("Producto", "texto"), ("Lote", "texto"), ("Caducidad", "fecha"),
    ("Días para caducar", "entero"), ("Vendible", "entero"), ("Apartado", "entero"),
    ("En empaque", "entero"), ("En recepción", "entero"), ("Cuarentena", "entero"), ("Físico", "entero"),
]


def generar(cliente, inicio, fin, filtros, es_mesa):
    filas = filas_existencias(cliente, inicio, fin, filtros, solo_con_lote=True)
    filas.sort(key=lambda f: (f["caducidad"] is None, f["caducidad"] or "", f["sku"].codigo, f["lote"].codigo))
    hoy_30 = sum(1 for f in filas if f["dias"] is not None and f["dias"] <= 30)
    vencidos = sum(1 for f in filas if f["dias"] is not None and f["dias"] < 0)
    return {
        "filas": [
            [f["sku"].codigo, f["sku"].descripcion, f["lote"].codigo, f["caducidad"], f["dias"],
             f["vendible"], f["apartado"], f["en_empaque"], f["en_recepcion"], f["cuarentena"], f["fisico"]]
            for f in filas
        ],
        "resumen": [
            ("lotes", len(filas)), ("piezas físicas", sum(f["fisico"] for f in filas)),
            ("caducan en 30 días", hoy_30), ("vencidos", vencidos),
        ],
    }
