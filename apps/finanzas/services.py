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

Picking (alistamiento) y empaque van UNA vez por pedido, cada uno solo si
se hizo (Chema 2026-10-01): un pedido pickeado y cancelado antes de empacar
paga picking y no empaque; el reporte lo muestra por caja.

Reglas de honestidad:
  - Pedidos CANCELADOS no pagan envío; el costo de sus guías vivas sí cuenta.
  - Guías CANCELADAS no cuestan: el carrier las reembolsa.
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


def peso_facturable(caja, planes):
    """Kg que paga el cliente por esa fila: báscula de su caja si existe, si
    no su plan sin el +5% de relleno; una guía sin caja (pedido sin plan)
    carga el peso de todo el pedido."""
    from apps.envios.cotizador import MARGEN_EMPAQUE

    cajas = [caja] if caja is not None else planes
    if cajas and all(p.peso_real_gr for p in cajas):
        return round(sum(p.peso_real_gr for p in cajas) / 1000.0, 2)
    return round(float(sum((p.peso_kg for p in cajas), Decimal("0"))) / float(MARGEN_EMPAQUE), 2)


def facturar_guias(cliente, inicio, fin):
    """Una fila por caja con actividad en [inicio, fin): cada guía creada en
    el periodo (una fila por guía) y cada caja planeada en el periodo que
    nunca tuvo guía (se pickeó y se canceló antes de empacar, o va en curso).
    Chema 2026-10-01: el ENVÍO se cobra por guía, a la tarifa de la zona del
    CP del pedido; PICKING y EMPAQUE se cobran UNA vez por pedido, cada uno
    solo si se hizo (picking: el pedido entró a picking; empaque: alguna
    caja se cerró; una guía es evidencia de ambos, una guía cancelada solo
    del picking), en la primera fila con envío del pedido o, si ninguna
    cobra envío, en la primera. Regresa {"filas": [...], "tarifas"}; cada
    fila: guia (None en caja sin guía), caja, pedido, ts, carrier, zona,
    estado, peso, transporte, picking, empaque, insumo, costo, nota,
    cobra_envio, bulto. Sin cargo de envío: guía CANCELADA (y su costo real
    es 0: se reembolsa), reexpedición (pedido con guía anterior al periodo:
    tampoco paga picking ni empaque, ya se facturaron), pedido CANCELADO y
    caja de reposición."""
    from django.db.models import Min

    from apps.envios.models import Guia, Paquete, PaqueteLinea

    tarifas = tarifario_de(cliente)
    envio_zona = tarifas["envio_bloque"]
    tarifa_picking = Decimal(str(tarifas["alistamiento_pedido"]))
    tarifa_empaque = Decimal(str(tarifas["empaque_pedido"]))
    insumo = Decimal(str(settings.TORRE.get("INSUMO_PAQUETE_MXN", 0)))
    guias = list(
        Guia.objects.filter(pedido__cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .select_related("pedido", "paquete").order_by("creado", "pk")
    )
    sin_guia = list(
        Paquete.objects.filter(pedido__cliente=cliente, creado__gte=inicio, creado__lt=fin, guias__isnull=True)
        .select_related("pedido").order_by("creado", "pk")
    )
    pedidos_ids = {g.pedido_id for g in guias} | {p.pedido_id for p in sin_guia}
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
    # Evidencia por pedido de lo que sí se hizo en piso.
    con_guia, con_guia_viva = set(), set()
    for pedido_id, estado in Guia.objects.filter(pedido_id__in=pedidos_ids).values_list("pedido_id", "estado"):
        con_guia.add(pedido_id)
        if estado != Guia.CANCELADA:
            con_guia_viva.add(pedido_id)
    con_caja_cerrada = {p.pedido_id for cajas in planes.values() for p in cajas if p.ts_cierre}

    def _pickeado(pedido):
        return bool(pedido.ts_picking or pedido.ts_empacado) or pedido.pk in con_guia

    def _empacado(pedido):
        return bool(pedido.ts_empacado) or pedido.pk in con_caja_cerrada or pedido.pk in con_guia_viva

    items = [(g.creado, g.pk, g, g.paquete) for g in guias] + [(p.creado, p.pk, None, p) for p in sin_guia]
    items.sort(key=lambda i: (i[0], i[1]))
    filas = []
    for ts, _pk, g, caja in items:
        pedido = g.pedido if g is not None else caja.pedido
        carrier = g.carrier if g is not None else caja.carrier
        zona = zona_de_cp(pedido.cp) or zona_de_carrier(carrier)
        notas = []
        reexpedicion = bool(primera_guia.get(pedido.pk) and primera_guia[pedido.pk] < inicio)
        if g is None:
            notas.append("sin guía")
        elif g.estado == Guia.CANCELADA:
            notas.append("guía cancelada (sin cargo)")
        if reexpedicion:
            notas.append("reexpedición (sin cargo)")
        elif pedido.estado == "CANCELADO":
            notas.append("cancelado")
        elif caja is not None and caja.pk in reposiciones:
            notas.append("reposición (sin cargo)")
        cobra_envio = g is not None and g.estado != Guia.CANCELADA and not notas
        guia_viva = g is not None and g.estado != Guia.CANCELADA
        bulto = guia_viva or (g is None and bool(caja.ts_cierre))
        filas.append({
            "guia": g, "caja": caja, "pedido": pedido, "ts": ts, "carrier": carrier, "zona": zona,
            "estado": estado_de_cp(pedido.cp) or NOMBRE_ESTADO.get((pedido.direccion or {}).get("province_code", "")) or SIN_ESTADO,
            "peso": peso_facturable(caja, planes.get(pedido.pk, [])),
            "transporte": Decimal(str(envio_zona.get(zona, 0))) if cobra_envio else Decimal("0"),
            "picking": Decimal("0"), "empaque": Decimal("0"), "nota": notas,
            "insumo": insumo if bulto else Decimal("0"),
            "costo": (g.costo_preferencial or Decimal("0")) if guia_viva else Decimal("0"),
            "cobra_envio": cobra_envio, "bulto": bulto, "reexpedicion": reexpedicion,
        })
    # Picking y empaque: una vez por pedido, en su primera fila con envío (o la primera).
    fila_del_pedido = {}
    for f in filas:
        actual = fila_del_pedido.get(f["pedido"].pk)
        if actual is None or (f["cobra_envio"] and not actual["cobra_envio"]):
            fila_del_pedido[f["pedido"].pk] = f
    for f in fila_del_pedido.values():
        pedido = f["pedido"]
        if f["reexpedicion"]:
            continue
        pickeado, empacado = _pickeado(pedido), _empacado(pedido)
        f["picking"] = tarifa_picking if pickeado else Decimal("0")
        f["empaque"] = tarifa_empaque if empacado else Decimal("0")
        if pickeado and not empacado:
            f["nota"].append("pickeado sin empacar")
    for f in filas:
        f["nota"] = " · ".join(f["nota"])
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
    ingreso_alistamiento = sum((f["picking"] for f in filas_guias), Decimal("0"))
    ingreso_empaque = sum((f["empaque"] for f in filas_guias), Decimal("0"))
    for f in filas_guias:
        if f["reexpedicion"] and f["guia"] is not None:
            guias_reexpedicion += 1
        if f["pedido"].estado == "CANCELADO":
            cancelados.add(f["pedido"].pk)
        if f["bulto"]:
            paquetes += 1  # cada bulto empacado gasta insumos
        if f["picking"] or f["empaque"]:
            pedidos_facturables.add(f["pedido"].pk)
        if not f["cobra_envio"]:
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
        "guias": sum(1 for f in filas_guias if f["guia"] is not None),
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
