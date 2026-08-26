"""Motor de finanzas: factura simulada por tarifario vs costos reales del mes.

La regla de oro del pricing (decisión ago-2026): el envío se factura al cliente
POR PEDIDO, por cada bloque de `bloque_kg` (20 kg) según la ZONA DEL DESTINO
(CP), nunca por guía ni por carrier. Así, si Torre divide un pedido en 3
paquetes chicos (PuntoPost) o lo consolida en 1 guía negociada, la factura del
cliente no se mueve — el ruteo interno es problema (y margen) nuestro.

Zonas de facturación (por prefijo de CP del pedido):
  local    → TORRE["CP_LOCAL_PREFIJOS"]  (CDMX + metro hasta Toluca)
  metro    → TORRE["CP_METRO_PREFIJOS"]  (GDL, Puebla, Qro — MTY es nacional)
  nacional → resto del país
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
"""
import math
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
    """Zona de facturación por prefijo de CP; None si no hay CP."""
    cp = str(cp or "").strip()
    if len(cp) < 2:
        return None
    torre = settings.TORRE
    prefijo = cp[:2]
    if prefijo in {str(p).zfill(2) for p in torre["CP_LOCAL_PREFIJOS"]}:
        return "local"
    if prefijo in {str(p).zfill(2) for p in torre["CP_METRO_PREFIJOS"]}:
        return "metro"
    return "nacional"


def zona_de_carrier(carrier):
    if carrier == CARRIER_LOCAL:
        return "local"
    if carrier in CARRIERS_METRO:
        return "metro"
    return "nacional"


def resumen_mes(cliente, inicio, fin):
    """Estado de resultados del cliente en [inicio, fin): ingreso, costo, margen."""
    from django.db.models import Min

    from apps.envios.cotizador import MARGEN_EMPAQUE
    from apps.envios.models import Guia, Paquete

    tarifas = tarifario_de(cliente)
    bloque_kg = float(tarifas.get("bloque_kg") or 20)

    guias = list(
        Guia.objects.filter(pedido__cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .select_related("pedido")
        .order_by("creado")
    )

    costo_carrier = sum((g.costo_preferencial or Decimal("0")) for g in guias)

    por_pedido = defaultdict(list)
    for guia in guias:
        por_pedido[guia.pedido_id].append(guia)

    # Dos queries para todo el mes: primera guía histórica por pedido
    # (detecta reexpediciones) y los paquetes planeados.
    primera_guia = dict(
        Guia.objects.filter(pedido_id__in=por_pedido)
        .values_list("pedido_id")
        .annotate(m=Min("creado"))
        .values_list("pedido_id", "m")
    )
    paquetes_por_pedido = defaultdict(list)
    for paquete in Paquete.objects.filter(pedido_id__in=por_pedido):
        paquetes_por_pedido[paquete.pedido_id].append(paquete)

    bloques = {"local": 0, "metro": 0, "nacional": 0}
    estados = {}
    pedidos_facturables = 0
    paquetes = 0
    guias_reexpedicion = 0
    cancelados = 0
    ingreso_envio = Decimal("0")
    envio_bloque = tarifas["envio_bloque"]

    for pedido_id, guias_pedido in por_pedido.items():
        pedido = guias_pedido[0].pedido
        # Reexpedición: el pedido ya se facturó en un mes anterior. Suma el
        # costo (arriba) y los insumos del re-empaque, nunca ingreso.
        if primera_guia.get(pedido_id) and primera_guia[pedido_id] < inicio:
            guias_reexpedicion += len(guias_pedido)
            continue
        # Cancelado después de generar guía: no se le cobra al cliente.
        if pedido.estado == "CANCELADO":
            cancelados += 1
            continue
        pedidos_facturables += 1
        planes = paquetes_por_pedido.get(pedido_id, [])
        paquetes += len(planes) or 1
        # Peso facturable: báscula si existe completa; si no, el peso planeado
        # sin el +5% de margen de empaque (el cliente no paga nuestro relleno).
        if planes and all(p.peso_real_gr for p in planes):
            peso = sum(p.peso_real_gr for p in planes) / 1000.0
        else:
            peso = float(sum((p.peso_kg for p in planes), Decimal("0"))) / float(MARGEN_EMPAQUE)
        n_bloques = max(1, math.ceil(round(peso, 2) / bloque_kg)) if peso > 0 else 1
        zona = zona_de_cp(pedido.cp) or zona_de_carrier(guias_pedido[0].carrier)
        bloques[zona] += n_bloques
        facturado = n_bloques * Decimal(str(envio_bloque.get(zona, 0)))
        ingreso_envio += facturado

        # Desglose por estado destino (mismos pedidos que se facturan).
        nombre_estado = (
            estado_de_cp(pedido.cp)
            or NOMBRE_ESTADO.get((pedido.direccion or {}).get("province_code", ""))
            or SIN_ESTADO
        )
        fila = estados.setdefault(nombre_estado, {
            "ordenes": 0, "peso": 0.0, "guias": 0, "zona": zona,
            "costo": Decimal("0"), "facturado": Decimal("0"),
        })
        fila["ordenes"] += 1
        fila["peso"] += peso
        fila["guias"] += len(guias_pedido)
        fila["costo"] += sum((g.costo_preferencial or Decimal("0")) for g in guias_pedido)
        fila["facturado"] += facturado

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

    costo_insumos = (paquetes + guias_reexpedicion) * Decimal(str(settings.TORRE["INSUMO_PAQUETE_MXN"]))
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
        "guias": len(guias),
        "reexpediciones": guias_reexpedicion,
        "cancelados": cancelados,
        "tarimas": tarimas_facturadas,
        "bloques": bloques,
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
