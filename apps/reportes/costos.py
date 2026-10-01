"""Costo por entrega: lo que el cliente paga por cada caja. El envío se cobra
POR GUÍA, a la tarifa de la zona del CP destino (Chema 2026-09-28: los
bloques de 20 kg quedaron atrás); picking y empaque se cobran UNA vez por
pedido, cada uno solo si se hizo (Chema 2026-10-01), por eso en las demás
cajas del pedido salen en 0. Una caja que se pickeó y se canceló antes de
empacar aparece sin guía, con picking y sin empaque. Mesa ve además el costo
real de cada guía, los insumos y el margen; el portal jamás ve el costo real.
Misma fuente que finanzas.resumen_mes (finanzas.facturar_guias): guías
creadas en el periodo y cajas planeadas en el periodo que no tuvieron guía;
guías canceladas (se reembolsan: cobro y costo 0), reexpediciones (guía
anterior al periodo), pedidos cancelados y cajas de reposición no pagan
envío. El almacenaje mensual va aparte.
"""
from collections import defaultdict
from decimal import Decimal

from .base import dinero

CLAVE = "costos"
TITULO = "Costo por entrega"
DESCRIPCION = (
    "Una fila por caja: el envío de cada guía con fecha en el periodo, según la zona del destino (Local, "
    "Metro o Nacional, según tu tarifario), y picking y empaque una vez por pedido, cada uno solo si se "
    "hizo (en las demás cajas del pedido van en 0). Guías canceladas, reexpediciones, cancelados y "
    "reposiciones no pagan envío. El almacenaje mensual va aparte."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "select", "default": "",
     "opciones": [("", "Todas"), ("local", "Local"), ("metro", "Metro"), ("nacional", "Nacional")]},
]
COLUMNAS = [
    ("Pedido", "texto"), ("Guía", "texto"), ("Caja", "texto"), ("Fecha", "fechahora"), ("Paquetería", "texto"),
    ("Zona", "texto"), ("Estado destino", "texto"), ("Peso kg", "decimal"),
    ("Transporte MXN", "dinero"), ("Picking MXN", "dinero"), ("Empaque MXN", "dinero"), ("Total MXN", "dinero"),
    ("Nota", "texto"),
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
        g, caja, pedido = f["guia"], f["caja"], f["pedido"]
        total = f["transporte"] + f["picking"] + f["empaque"]
        if f["cobra_envio"]:
            guias_facturables += 1
        if total:
            pedidos_facturables.add(pedido.pk)
        fila = [
            pedido.folio, g.numero if g is not None else "", str(caja.numero) if caja is not None else "", f["ts"],
            f["carrier"], f["zona"], f["estado"], Decimal(str(f["peso"])),
            dinero(f["transporte"]), dinero(f["picking"]), dinero(f["empaque"]), dinero(total), f["nota"],
        ]
        if es_mesa:
            fila += [dinero(f["costo"]), dinero(f["insumo"]), dinero(total - f["costo"] - f["insumo"])]
        filas.append(fila)
        totales["transporte"] += f["transporte"]
        totales["picking"] += f["picking"]
        totales["empaque"] += f["empaque"]
        totales["real"] += f["costo"]
        totales["insumos"] += f["insumo"]
    cobrado = totales["transporte"] + totales["picking"] + totales["empaque"]
    resumen = [
        ("guías facturables", guias_facturables),
        ("pedidos facturables", len(pedidos_facturables)),
        ("transporte MXN", dinero(totales["transporte"])), ("picking MXN", dinero(totales["picking"])),
        ("empaque MXN", dinero(totales["empaque"])), ("total MXN", dinero(cobrado)),
        ("almacenaje mensual aparte MXN", dinero(facturacion["tarifas"]["almacenaje_mes"])),
    ]
    if es_mesa:
        resumen += [
            ("costo real guías MXN", dinero(totales["real"])),
            ("margen MXN", dinero(cobrado - totales["real"] - totales["insumos"])),
        ]
    return {"filas": filas, "resumen": resumen}
