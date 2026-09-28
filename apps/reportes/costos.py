"""Costo por entrega: lo que el cliente paga por cada guía (Chema 2026-09-28:
el cobro es por guía, a la tarifa de la zona del CP destino; los bloques de
20 kg quedaron atrás) más el almacén por pedido (alistamiento + empaque, una
vez por pedido). Mesa ve además el costo real de cada guía, los insumos y el
margen; el portal jamás ve el costo real. Misma fuente que finanzas.resumen_mes
(finanzas.facturar_guias): guías creadas en el periodo; guías canceladas,
reexpediciones (guía anterior al periodo), pedidos cancelados y cajas de
reposición se listan sin cargo. El almacenaje mensual va aparte.
"""
from collections import defaultdict
from decimal import Decimal

from .base import dinero

CLAVE = "costos"
TITULO = "Costo por entrega"
DESCRIPCION = (
    "Una fila por guía con fecha en el periodo: transporte según la zona del destino (Local, Metro o "
    "Nacional, según tu tarifario) y almacén (alistamiento y empaque, una vez por pedido). Guías "
    "canceladas, reexpediciones, cancelados y reposiciones se muestran sin cargo. El almacenaje "
    "mensual va aparte."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "select", "default": "",
     "opciones": [("", "Todas"), ("local", "Local"), ("metro", "Metro"), ("nacional", "Nacional")]},
]
COLUMNAS = [
    ("Pedido", "texto"), ("Guía", "texto"), ("Caja", "texto"), ("Fecha guía", "fechahora"), ("Paquetería", "texto"),
    ("Zona", "texto"), ("Estado destino", "texto"), ("Peso kg", "decimal"),
    ("Transporte MXN", "dinero"), ("Almacén MXN", "dinero"), ("Total MXN", "dinero"), ("Nota", "texto"),
]
COLUMNAS_MESA = [("Costo real guía MXN", "dinero"), ("Insumos MXN", "dinero"), ("Margen MXN", "dinero")]


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.mesa.finanzas import facturar_guias  # lazy por contrato

    facturacion = facturar_guias(cliente, inicio, fin)
    zona_filtro = (filtros or {}).get("zona") or ""
    filas, totales = [], defaultdict(Decimal)
    guias_facturables, pedidos_facturables = 0, set()
    for f in facturacion["filas"]:
        if zona_filtro and f["zona"] != zona_filtro:
            continue
        g, pedido = f["guia"], f["pedido"]
        total = f["transporte"] + f["almacen"]
        if not f["nota"]:
            guias_facturables += 1
            pedidos_facturables.add(pedido.pk)
        fila = [
            pedido.folio, g.numero, str(g.paquete.numero) if g.paquete_id else "", g.creado, g.carrier, f["zona"],
            f["estado"], Decimal(str(f["peso"])), dinero(f["transporte"]), dinero(f["almacen"]), dinero(total), f["nota"],
        ]
        if es_mesa:
            fila += [dinero(f["costo"]), dinero(f["insumo"]), dinero(total - f["costo"] - f["insumo"])]
        filas.append(fila)
        totales["transporte"] += f["transporte"]
        totales["almacen"] += f["almacen"]
        totales["real"] += f["costo"]
        totales["insumos"] += f["insumo"]
    resumen = [
        ("guías facturables", guias_facturables),
        ("pedidos facturables", len(pedidos_facturables)),
        ("transporte MXN", dinero(totales["transporte"])), ("almacén MXN", dinero(totales["almacen"])),
        ("total MXN", dinero(totales["transporte"] + totales["almacen"])),
        ("almacenaje mensual aparte MXN", dinero(facturacion["tarifas"]["almacenaje_mes"])),
    ]
    if es_mesa:
        resumen += [
            ("costo real guías MXN", dinero(totales["real"])),
            ("margen MXN", dinero(totales["transporte"] + totales["almacen"] - totales["real"] - totales["insumos"])),
        ]
    return {"filas": filas, "resumen": resumen}
