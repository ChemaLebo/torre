"""Costo por entrega: lo que el cliente paga por cada pedido, separado en
transporte (envío por bloque de peso y zona del destino, la regla de oro del
tarifario) y almacén (alistamiento + empaque). Mesa ve además el costo real
de las guías, los insumos y el margen; el portal jamás ve el costo real.

Misma lógica que finanzas.resumen_mes pero por pedido: entran los pedidos
con guía creada en el periodo; reexpediciones (guía anterior al periodo) y
cancelados se listan sin cargo. El almacenaje mensual no es por pedido y va
en el resumen como dato aparte.
"""
import math
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db.models import Min

from apps.envios.models import Guia, Paquete

from .base import dinero

CLAVE = "costos"
TITULO = "Costo por entrega"
DESCRIPCION = (
    "Por pedido con guía en el periodo: transporte (bloques de peso por zona del destino, según "
    "tu tarifario) y almacén (alistamiento y empaque). Reexpediciones y cancelados se muestran "
    "sin cargo. El almacenaje mensual va aparte."
)
CON_FECHAS = True
FILTROS = []
COLUMNAS = [
    ("Pedido", "texto"), ("Fecha guía", "fechahora"), ("Paquetería", "texto"), ("Zona", "texto"),
    ("Estado destino", "texto"), ("Cajas", "entero"), ("Peso facturable kg", "decimal"), ("Bloques", "entero"),
    ("Transporte MXN", "dinero"), ("Almacén MXN", "dinero"), ("Total MXN", "dinero"), ("Nota", "texto"),
]
COLUMNAS_MESA = [("Costo real guías MXN", "dinero"), ("Insumos MXN", "dinero"), ("Margen MXN", "dinero")]


def peso_facturable(paquetes):
    """Kg que paga el cliente: báscula si todas las cajas la tienen; si no, el
    plan sin el margen de empaque (nuestro relleno no se cobra)."""
    from apps.envios.cotizador import MARGEN_EMPAQUE  # lazy por contrato

    if paquetes and all(p.peso_real_gr for p in paquetes):
        return round(sum(p.peso_real_gr for p in paquetes) / 1000.0, 2)
    return round(float(sum((p.peso_kg for p in paquetes), Decimal(0))) / float(MARGEN_EMPAQUE), 2)


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.mesa.finanzas import (  # lazy por contrato
        SIN_ESTADO,
        estado_de_cp,
        tarifario_de,
        zona_de_carrier,
        zona_de_cp,
    )

    tarifas = tarifario_de(cliente)
    bloque_kg = float(tarifas.get("bloque_kg") or 20)
    envio_bloque = tarifas["envio_bloque"]
    almacen_pedido = Decimal(str(tarifas["alistamiento_pedido"])) + Decimal(str(tarifas["empaque_pedido"]))
    insumo = Decimal(str(settings.TORRE.get("INSUMO_PAQUETE_MXN", 0)))

    guias = list(
        Guia.objects.filter(pedido__cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .select_related("pedido").order_by("creado", "pk")
    )
    por_pedido = defaultdict(list)
    for g in guias:
        por_pedido[g.pedido_id].append(g)
    primera_guia = dict(
        Guia.objects.filter(pedido_id__in=por_pedido).values_list("pedido_id")
        .annotate(m=Min("creado")).values_list("pedido_id", "m")
    )
    paquetes = defaultdict(list)
    for p in Paquete.objects.filter(pedido_id__in=por_pedido).order_by("numero"):
        paquetes[p.pedido_id].append(p)

    filas, totales = [], defaultdict(Decimal)
    facturables = 0
    for pedido_id, guias_pedido in por_pedido.items():
        pedido = guias_pedido[0].pedido
        planes = paquetes.get(pedido_id, [])
        peso = peso_facturable(planes)
        zona = zona_de_cp(pedido.cp) or zona_de_carrier(guias_pedido[0].carrier)
        costo_real = sum((g.costo_preferencial or Decimal(0)) for g in guias_pedido)
        insumos = (len(planes) or 1) * insumo
        nota, transporte, almacen, bloques = "", Decimal(0), Decimal(0), 0
        if primera_guia.get(pedido_id) and primera_guia[pedido_id] < inicio:
            nota = "reexpedición (sin cargo)"
        elif pedido.estado == "CANCELADO":
            nota = "cancelado (sin cargo)"
        else:
            facturables += 1
            bloques = max(1, math.ceil(round(peso, 2) / bloque_kg)) if peso > 0 else 1
            transporte = bloques * Decimal(str(envio_bloque.get(zona, 0)))
            almacen = almacen_pedido
        total = transporte + almacen
        fila = [
            pedido.folio, guias_pedido[0].creado, guias_pedido[-1].carrier, zona,
            estado_de_cp(pedido.cp) or SIN_ESTADO, len(planes) or 1, Decimal(str(peso)), bloques,
            dinero(transporte), dinero(almacen), dinero(total), nota,
        ]
        if es_mesa:
            fila += [dinero(costo_real), dinero(insumos), dinero(total - costo_real - insumos)]
        filas.append(fila)
        totales["transporte"] += transporte
        totales["almacen"] += almacen
        totales["real"] += costo_real
        totales["insumos"] += insumos
    resumen = [
        ("pedidos facturables", facturables),
        ("transporte MXN", dinero(totales["transporte"])), ("almacén MXN", dinero(totales["almacen"])),
        ("total MXN", dinero(totales["transporte"] + totales["almacen"])),
        ("almacenaje mensual aparte MXN", dinero(tarifas["almacenaje_mes"])),
    ]
    if es_mesa:
        resumen += [
            ("costo real guías MXN", dinero(totales["real"])),
            ("margen MXN", dinero(totales["transporte"] + totales["almacen"] - totales["real"] - totales["insumos"])),
        ]
    return {"filas": filas, "resumen": resumen}
