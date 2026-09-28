"""Motor de finanzas: factura simulada por tarifario vs costos reales del mes.

La regla del pricing (Chema 2026-09-28, sustituye a los bloques de 20 kg de
ago-2026): el envío se factura al cliente POR GUÍA, a la tarifa de la ZONA
DEL DESTINO (CP del pedido). Cada guía comprada es un cobro, alineado con lo
que el carrier le cobra a WOP; `bloque_kg` queda solo como tope informativo
por caja. `facturar_guias` es la única fuente: el reporte "Costo por
entrega" (Mesa y portal) y el resumen mensual salen de ahí.

Zonas de facturación (por CP del pedido): rangos de config/zonas_cp.csv,
armados con las bandas de 99minutos (mesa.zonas); fuera de todo rango es
nacional. Aplica a toda guía, la haya llevado quien sea.
Fallback sin CP: se infiere del carrier de la primera guía (local/puntopost).

El peso facturable descuenta el margen de empaque (+5% de cotizador): el
cliente paga por lo que vendió, no por nuestro relleno. Si todos los bultos
tienen peso real de báscula, se usa ese.

Reglas de honestidad:
  - Pedidos CANCELADOS no se facturan; el costo de sus guías sí cuenta.
  - Reexpediciones (pedido con guía anterior al mes) suman costo de carrier e
    insumos del re-empaque, nunca ingreso.
  - El almacenaje se factura a clientes activos en meses ya iniciados.

Modelo B (clientes generales): recepción a $X/tarima por cada OrdenEntrada
descargada en el mes (lo que contó el piso manda; si no se contó, lo anunciado)
y mínimo mensual — si la factura no llega al piso pactado se agrega una línea
de ajuste al total, sin inflar fulfillment ni envío. Con los defaults en 0
(Modelo A, Colima) nada de esto se cobra.

Costos: carrier = Guia.costo_preferencial real (incluye flota local LOCAL-*);
insumos = TORRE["INSUMO_PAQUETE_MXN"] por bulto; fijos = globales, en la vista.
Sin cargo: guías canceladas, reexpediciones (pedido con guía anterior al
periodo), pedidos cancelados y cajas de reposición (compensación).
"""
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

CARRIERS_METRO = {"puntopost"}
CARRIER_LOCAL = "local"

# Nombre legible por código de estado (cotizador.CP_ESTADO, vocabulario
# code_shopify de envia).
NOMBRE_ESTADO = {
    "DF": "Ciudad de México", "AGS": "Aguascalientes", "BC": "Baja California",
    "BCS": "Baja California Sur", "CAMP": "Campeche", "COAH": "Coahuila",
    "COL": "Colima", "CHIS": "Chiapas", "CHIH": "Chihuahua", "DGO": "Durango",
    "GTO": "Guanajuato", "GRO": "Guerrero", "HGO": "Hidalgo", "JAL": "Jalisco",
    "MEX": "Estado de México", "MICH": "Michoacán", "MOR": "Morelos",
    "NAY": "Nayarit", "NL": "Nuevo León", "OAX": "Oaxaca", "PUE": "Puebla",
    "QRO": "Querétaro", "Q ROO": "Quintana Roo", "SLP": "San Luis Potosí",
    "SIN": "Sinaloa", "SON": "Sonora", "TAB": "Tabasco", "TAMPS": "Tamaulipas",
    "TLAX": "Tlaxcala", "VER": "Veracruz", "YUC": "Yucatán", "ZAC": "Zacatecas",
}
SIN_ESTADO = "Sin estado (no capturado)"


def estado_de_cp(cp):
    """Nombre del estado destino a partir del CP; None si no se puede inferir."""
    from apps.envios.cotizador import CP_ESTADO

    cp = str(cp or "").strip()
    codigo = CP_ESTADO.get(cp[:2]) if len(cp) >= 2 else None
    return NOMBRE_ESTADO.get(codigo) if codigo else None


def tarifario_de(cliente):
    """Tarifario efectivo: default de settings + override JSON del cliente."""
    base = dict(settings.TORRE["TARIFARIO_DEFAULT"])
    base["envio_bloque"] = dict(base.get("envio_bloque", {}))
    for clave, valor in (cliente.tarifario or {}).items():
        if clave == "envio_bloque":
            base["envio_bloque"].update(valor or {})
        else:
            base[clave] = valor
    return base


def zona_de_cp(cp):
    """Zona de cobro por CP destino según config/zonas_cp.csv (bandas de
    99minutos, Chema 2026-09-28); None sin CP válido."""
    from .zonas import zona_de_cp as _zona  # lazy: lee el archivo una vez

    return _zona(cp)


def zona_de_carrier(carrier):
    if carrier == CARRIER_LOCAL:
        return "local"
    if carrier in CARRIERS_METRO:
        return "metro"
    return "nacional"


def peso_de_guia(guia, planes):
    """Kg que paga el cliente por esa guía: báscula de su caja si existe, si
    no su plan sin el +5% de relleno; una guía sin caja (pedido sin plan)
    carga el peso de todo el pedido."""
    from apps.envios.cotizador import MARGEN_EMPAQUE

    cajas = [guia.paquete] if guia.paquete_id else planes
    if cajas and all(p.peso_real_gr for p in cajas):
        return round(sum(p.peso_real_gr for p in cajas) / 1000.0, 2)
    return round(float(sum((p.peso_kg for p in cajas), Decimal("0"))) / float(MARGEN_EMPAQUE), 2)


def facturar_guias(cliente, inicio, fin):
    """Una fila por guía creada en [inicio, fin) con lo que se le cobra al
    cliente y lo que costó (Chema 2026-09-28: el cobro es por guía, a la
    tarifa de la zona del CP del pedido). Regresa {"filas": [...],
    "tarifas"}; cada fila: guia, pedido, zona, estado, peso, transporte,
    almacen (alistamiento + empaque UNA vez por pedido facturable, en su
    primera guía del periodo), insumo, costo, nota ("" = facturable)."""
    from django.db.models import Min

    from apps.envios.models import Guia, Paquete, PaqueteLinea

    tarifas = tarifario_de(cliente)
    envio_zona = tarifas["envio_bloque"]
    almacen_pedido = Decimal(str(tarifas["alistamiento_pedido"])) + Decimal(str(tarifas["empaque_pedido"]))
    insumo = Decimal(str(settings.TORRE.get("INSUMO_PAQUETE_MXN", 0)))
    guias = list(
        Guia.objects.filter(pedido__cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .select_related("pedido", "paquete").order_by("creado", "pk")
    )
    pedidos_ids = {g.pedido_id for g in guias}
    primera_guia = dict(
        Guia.objects.filter(pedido_id__in=pedidos_ids).values_list("pedido_id")
        .annotate(m=Min("creado")).values_list("pedido_id", "m")
    )
    planes = defaultdict(list)
    for p in Paquete.objects.filter(pedido_id__in=pedidos_ids).order_by("numero"):
        planes[p.pedido_id].append(p)
    # Cajas de reposición (todas sus líneas reponen otra): compensación, sin cargo.
    con_lineas, con_originales = set(), set()
    for paquete_id, reposicion in PaqueteLinea.objects.filter(paquete__pedido_id__in=pedidos_ids).values_list(
        "paquete_id", "linea_pedido__reposicion_de_id",
    ):
        con_lineas.add(paquete_id)
        if reposicion is None:
            con_originales.add(paquete_id)
    reposiciones = con_lineas - con_originales
    con_almacen = set()
    filas = []
    for g in guias:
        pedido = g.pedido
        zona = zona_de_cp(pedido.cp) or zona_de_carrier(g.carrier)
        nota = ""
        if g.estado == Guia.CANCELADA:
            nota = "guía cancelada (sin cargo)"
        elif primera_guia.get(pedido.pk) and primera_guia[pedido.pk] < inicio:
            nota = "reexpedición (sin cargo)"
        elif pedido.estado == "CANCELADO":
            nota = "cancelado (sin cargo)"
        elif g.paquete_id and g.paquete_id in reposiciones:
            nota = "reposición (sin cargo)"
        cobra = not nota
        transporte = Decimal(str(envio_zona.get(zona, 0))) if cobra else Decimal("0")
        almacen = Decimal("0")
        if cobra and pedido.pk not in con_almacen:
            con_almacen.add(pedido.pk)
            almacen = almacen_pedido
        filas.append({
            "guia": g, "pedido": pedido, "zona": zona,
            "estado": estado_de_cp(pedido.cp) or NOMBRE_ESTADO.get((pedido.direccion or {}).get("province_code", "")) or SIN_ESTADO,
            "peso": peso_de_guia(g, planes.get(pedido.pk, [])),
            "transporte": transporte, "almacen": almacen, "nota": nota,
            "insumo": insumo if g.estado != Guia.CANCELADA else Decimal("0"),
            "costo": g.costo_preferencial or Decimal("0"),
        })
    return {"filas": filas, "tarifas": tarifas}


def resumen_mes(cliente, inicio, fin):
    """Estado de resultados del cliente en [inicio, fin): ingreso, costo, margen."""
    facturacion = facturar_guias(cliente, inicio, fin)
    tarifas = facturacion["tarifas"]
    filas_guias = facturacion["filas"]
    costo_carrier = sum((f["costo"] for f in filas_guias), Decimal("0"))
    guias_zona = {"local": 0, "metro": 0, "nacional": 0}
    estados = {}
    pedidos_facturables = set()
    paquetes = 0
    guias_reexpedicion = 0
    cancelados = set()
    ingreso_envio = Decimal("0")
    for f in filas_guias:
        if f["nota"].startswith("reexpedición"):
            guias_reexpedicion += 1
        if f["nota"].startswith("cancelado"):
            cancelados.add(f["pedido"].pk)
        if f["guia"].estado != "CANCELADA":
            paquetes += 1  # cada guía es un bulto: insumos
        if f["nota"]:
            continue
        pedidos_facturables.add(f["pedido"].pk)
        guias_zona[f["zona"]] += 1
        ingreso_envio += f["transporte"]
        fila = estados.setdefault(f["estado"], {
            "pedidos": set(), "ordenes": 0, "peso": 0.0, "guias": 0, "zona": f["zona"],
            "costo": Decimal("0"), "facturado": Decimal("0"),
        })
        fila["pedidos"].add(f["pedido"].pk)
        fila["ordenes"] = len(fila["pedidos"])
        fila["peso"] += f["peso"]
        fila["guias"] += 1
        fila["costo"] += f["costo"]
        fila["facturado"] += f["transporte"]
    for fila in estados.values():
        fila.pop("pedidos", None)
    pedidos_facturables = len(pedidos_facturables)
    cancelados = len(cancelados)

    # Recepción por tarima (Modelo B): entradas descargadas dentro del mes.
    # Lo que contó el piso al cerrar la entrada manda (tarimas_recibidas);
    # si no se contó, vale lo anunciado por el cliente (tarimas).
    from apps.inventario.models import OrdenEntrada  # lazy por contrato

    tarifa_recepcion = Decimal(str(tarifas.get("recepcion_tarima", 0)))
    tarimas_facturadas = sum(
        (recibidas or anunciadas)
        for recibidas, anunciadas in OrdenEntrada.objects.filter(
            cliente=cliente,
            ts_descarga_fin__gte=inicio,
            ts_descarga_fin__lt=fin,
            estado__in=(OrdenEntrada.RECIBIDA, OrdenEntrada.CERRADA),
        ).values_list("tarimas_recibidas", "tarimas")
    )
    ingreso_recepcion = tarimas_facturadas * tarifa_recepcion

    cobra_almacenaje = cliente.activo and inicio <= timezone.now()
    ingreso_almacenaje = Decimal(str(tarifas["almacenaje_mes"])) if cobra_almacenaje else Decimal("0")
    ingreso_alistamiento = pedidos_facturables * Decimal(str(tarifas["alistamiento_pedido"]))
    ingreso_empaque = pedidos_facturables * Decimal(str(tarifas["empaque_pedido"]))
    ingreso_fulfillment = ingreso_almacenaje + ingreso_alistamiento + ingreso_empaque + ingreso_recepcion
    ingreso_total = ingreso_fulfillment + ingreso_envio

    # Mínimo mensual (Modelo B): si la factura no llega al piso pactado, se
    # agrega una línea de ajuste al total (línea aparte del CFDI) — nunca
    # infla fulfillment ni envío.
    minimo_mes = Decimal(str(tarifas.get("minimo_mes", 0)))
    ajuste_minimo = Decimal("0")
    if minimo_mes > 0 and ingreso_total < minimo_mes:
        ajuste_minimo = minimo_mes - ingreso_total
        ingreso_total = minimo_mes

    costo_insumos = paquetes * Decimal(str(settings.TORRE["INSUMO_PAQUETE_MXN"]))
    costo_total = costo_carrier + costo_insumos
    margen_bruto = ingreso_total - costo_total

    # El benchmark por pedido de Melonn ya trae su bodegaje prorrateado, así
    # que se compara contra el ingreso total (almacenaje incluido y con el
    # mínimo mensual ya aplicado — es lo que el cliente realmente paga).
    benchmark = pedidos_facturables * Decimal(str(settings.TORRE["BENCHMARK_PEDIDO_MXN"]))
    ahorro_pct = None
    if benchmark:
        ahorro_pct = round(float((benchmark - ingreso_total) / benchmark) * 100, 1)

    return {
        "cliente": cliente,
        "tarifario": tarifas,
        "pedidos": pedidos_facturables,
        "paquetes": paquetes,
        "guias": len(filas_guias),
        "reexpediciones": guias_reexpedicion,
        "cancelados": cancelados,
        "tarimas": tarimas_facturadas,
        "guias_zona": guias_zona,
        "estados": estados,
        "ingresos": {
            "almacenaje": ingreso_almacenaje,
            "alistamiento": ingreso_alistamiento,
            "empaque": ingreso_empaque,
            "recepcion": ingreso_recepcion,
            "fulfillment": ingreso_fulfillment,
            "envio": ingreso_envio,
            "ajuste_minimo": ajuste_minimo,
            "total": ingreso_total,
        },
        "costos": {
            "carrier": costo_carrier,
            "insumos": costo_insumos,
            "total": costo_total,
        },
        "margen_bruto": margen_bruto,
        "benchmark": benchmark,
        "ahorro_pct": ahorro_pct,
    }
