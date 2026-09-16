"""Ventas a nivel línea por fechas.

Una fila por línea de pedido creado en el rango: pedido, canal, estado, SKU,
producto, cantidad, precio unitario e importe (solo cuando la línea trae el
precio real de Shopify; pedidos manuales y hijas de kit van sin importe),
bodega (fija: Torre) y CP destino. Cancelados se listan con su estado y no
suman al importe.
"""
from decimal import Decimal

from apps.pedidos.models import LineaPedido, Pedido

from .base import dinero

CLAVE = "ventas"
TITULO = "Ventas por pedido y SKU"
DESCRIPCION = (
    "Cada línea vendida en el periodo con cantidad, precio unitario real de la tienda e "
    "importe. Sin precio (pedido manual o componente de un kit) la línea cuenta piezas pero "
    "no importe; los cancelados se muestran y no suman."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "sin_cancelados", "etiqueta": "Ocultar cancelados", "tipo": "checkbox", "default": False},
]
COLUMNAS = [
    ("Fecha", "fechahora"), ("Pedido", "texto"), ("Canal", "texto"), ("Estado", "texto"),
    ("SKU", "texto"), ("Producto", "texto"), ("Cantidad", "entero"), ("Precio unitario", "dinero"),
    ("Importe", "dinero"), ("Bodega", "texto"), ("CP destino", "texto"),
]
BODEGA = "Torre"
CANCELADOS = (Pedido.CANCELADO, Pedido.CANCELACION_PENDIENTE)


def generar(cliente, inicio, fin, filtros, es_mesa):
    lineas = (
        LineaPedido.objects.filter(pedido__cliente=cliente, pedido__creado__gte=inicio, pedido__creado__lt=fin)
        .select_related("pedido", "sku", "parte_de_kit").order_by("pedido__creado", "pedido__pk", "pk")
    )
    if filtros.get("sin_cancelados"):
        lineas = lineas.exclude(pedido__estado__in=CANCELADOS)
    filas, pedidos, piezas, importe, sin_precio = [], set(), 0, Decimal(0), 0
    for l in lineas:
        p = l.pedido
        cancelado = p.estado in CANCELADOS
        producto = l.sku.descripcion + (" (componente de kit)" if l.parte_de_kit_id else "")
        total = dinero(l.precio_unitario * l.cantidad) if l.precio_unitario is not None else None
        filas.append([
            p.creado, p.folio, p.get_canal_display(), p.get_estado_display(), l.sku.codigo, producto,
            l.cantidad, dinero(l.precio_unitario), total, BODEGA, p.cp,
        ])
        pedidos.add(p.pk)
        if cancelado:
            continue
        if not l.parte_de_kit_id:
            piezas += l.cantidad
        if total is not None:
            importe += total
        elif not l.parte_de_kit_id:
            sin_precio += 1
    return {
        "filas": filas,
        "resumen": [
            ("pedidos", len(pedidos)), ("piezas", piezas), ("MXN vendidos", dinero(importe)),
            ("líneas sin precio", sin_precio),
        ],
    }
