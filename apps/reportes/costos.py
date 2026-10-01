"""Costo por entrega: lo que el cliente paga por cada caja, con la misma lógica
que los estados de cuenta de finanzas (finanzas.facturar_guias es la única
fuente). El envío se cobra POR GUÍA, a la tarifa de la zona del CP destino
(Chema 2026-09-28: los bloques de 20 kg quedaron atrás); TODA guía comprada
se cobra (reposición, reexpedición y cancelado incluidos) y todo reembolso de
la paquetería se descuenta en la fecha en que llegó: en la fila de la guía si
cae en el periodo, o como fila propia al final ("reembolso de corte
anterior") si la guía se cobró antes (Chema 2026-10-01). Picking y empaque
se cobran UNA vez por pedido, cada uno solo si se hizo, por eso en las demás
cajas del pedido salen en 0; una caja que se pickeó y se canceló antes de
empacar aparece sin guía, con picking y sin empaque. Las tarifas van sin
IVA; el resumen trae el subtotal, el IVA y el total. Mesa ve además el costo
real de cada guía, los insumos y el margen; el portal jamás ve el costo real.
El almacenaje va aparte, por corte.
"""
from collections import defaultdict
from decimal import Decimal

from .base import dinero

CLAVE = "costos"
TITULO = "Costo por entrega"
DESCRIPCION = (
    "Una fila por caja: el envío de cada guía con fecha en el periodo, según la zona del destino (Local, "
    "Metro o Nacional, según tu tarifario), menos lo que la paquetería reembolsó; picking y empaque una vez "
    "por pedido, cada uno solo si se hizo (en las demás cajas del pedido van en 0). Los reembolsos de guías "
    "cobradas en periodos anteriores van al final. Tarifas sin IVA; el almacenaje va aparte."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "select", "default": "",
     "opciones": [("", "Todas"), ("local", "Local"), ("metro", "Metro"), ("nacional", "Nacional")]},
]
COLUMNAS = [
    ("Pedido", "texto"), ("Guía", "texto"), ("Caja", "texto"), ("Fecha", "fechahora"), ("Paquetería", "texto"),
    ("Zona", "texto"), ("Estado destino", "texto"), ("Peso kg", "decimal"),
    ("Transporte MXN", "dinero"), ("Reembolso MXN", "dinero"), ("Picking MXN", "dinero"), ("Empaque MXN", "dinero"),
    ("Total MXN", "dinero"), ("Nota", "texto"),
]
COLUMNAS_MESA = [("Costo real guía MXN", "dinero"), ("Insumos MXN", "dinero"), ("Margen MXN", "dinero")]


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.finanzas.cortes import corte_de  # lazy por contrato
    from apps.finanzas.services import NOMBRE_ESTADO, SIN_ESTADO, con_iva, estado_de_cp, facturar_guias, zona_de_carrier, zona_de_cp

    facturacion = facturar_guias(cliente, inicio, fin)
    zona_filtro = (filtros or {}).get("zona") or ""
    filas, totales = [], defaultdict(Decimal)
    guias_facturables, pedidos_facturables = 0, set()
    for f in facturacion["filas"]:
        if zona_filtro and f["zona"] != zona_filtro:
            continue
        g, caja, pedido = f["guia"], f["caja"], f["pedido"]
        total = f["transporte"] - f["reembolso"] + f["picking"] + f["empaque"]
        if f["cobra_envio"]:
            guias_facturables += 1
        if total:
            pedidos_facturables.add(pedido.pk)
        fila = [
            pedido.folio, g.numero if g is not None else "", str(caja.numero) if caja is not None else "", f["ts"],
            f["carrier"], f["zona"], f["estado"], Decimal(str(f["peso"])),
            dinero(f["transporte"]), dinero(f["reembolso"]), dinero(f["picking"]), dinero(f["empaque"]), dinero(total), f["nota"],
        ]
        if es_mesa:
            fila += [dinero(f["costo"]), dinero(f["insumo"]), dinero(total - f["costo"] - f["insumo"])]
        filas.append(fila)
        totales["transporte"] += f["transporte"]
        totales["reembolsos"] += f["reembolso"]
        totales["picking"] += f["picking"]
        totales["empaque"] += f["empaque"]
        totales["real"] += f["costo"]
        totales["insumos"] += f["insumo"]
    # Reembolsos de guías cobradas en periodos anteriores: restan en este, al final.
    for r in facturacion["reembolsos_previos"]:
        g, pedido = r.guia, r.guia.pedido
        zona = zona_de_cp(pedido.cp) or zona_de_carrier(g.carrier)
        if zona_filtro and zona != zona_filtro:
            continue
        pedidos_facturables.add(pedido.pk)
        fila = [
            pedido.folio, g.numero, str(g.paquete.numero) if g.paquete_id else "", r.fecha, g.carrier, zona,
            estado_de_cp(pedido.cp) or NOMBRE_ESTADO.get((pedido.direccion or {}).get("province_code", "")) or SIN_ESTADO,
            Decimal("0"), dinero(0), dinero(r.monto), dinero(0), dinero(0), dinero(-r.monto),
            f"reembolso de corte anterior (guía cobrada el {corte_de(g.creado).etiqueta_corta}) · {r.get_origen_display().lower()}",
        ]
        if es_mesa:
            fila += [dinero(0), dinero(0), dinero(-r.monto)]
        filas.append(fila)
        totales["reembolsos"] += r.monto
    subtotal = totales["transporte"] - totales["reembolsos"] + totales["picking"] + totales["empaque"]
    monto_iva, total_con_iva = con_iva(subtotal)
    resumen = [
        ("guías facturables", guias_facturables),
        ("pedidos facturables", len(pedidos_facturables)),
        ("transporte MXN", dinero(totales["transporte"])), ("reembolsos MXN", dinero(-totales["reembolsos"])),
        ("picking MXN", dinero(totales["picking"])), ("empaque MXN", dinero(totales["empaque"])),
        ("subtotal sin IVA MXN", dinero(subtotal)), ("IVA MXN", dinero(monto_iva)), ("total con IVA MXN", dinero(total_con_iva)),
        ("almacenaje aparte, al mes MXN", dinero(facturacion["tarifas"]["almacenaje_mes"])),
    ]
    if es_mesa:
        resumen += [
            ("costo real guías MXN", dinero(totales["real"])),
            ("margen MXN", dinero(subtotal - totales["real"] - totales["insumos"])),
        ]
    return {"filas": filas, "resumen": resumen}
