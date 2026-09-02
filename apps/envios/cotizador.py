"""Cotización por lane y planificación de división de envíos.

Reglas de negocio (CONVENTIONS-ENVIOS.md, medidas contra la API real 2026-08-03):
- Tope duro: ningún paquete pasa de settings.TORRE["MAX_PESO_ENVIO_KG"].
- Meta: settings.TORRE["TARIFA_OBJETIVO_MXN"] por envío; si la mejor tarifa la
  excede se marca fuera_de_meta (jamás se bloquea el envío).
- La división se decide COTIZANDO particiones reales y comparando totales:
  puntopost ($86/$91, solo ≤10 kg y cobertura parcial) hace que 16 kg dividido
  en 2×8 cueste $182 vs $224 entero por estafeta.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.services import registrar_evento

from .models import CotizacionCache, Paquete, PaqueteLinea

# CP mexicano (2 primeros dígitos) → código de estado de envia.com, columna
# code_shopify de GET /state?country_code=MX — el vocabulario que
# /ship/generate/ VALIDA (FAQ de envia: jamás ISO/2-dígitos; el rate es laxo
# y aceptaba cualquier cosa, por eso la tabla vieja con CL/JA/GJ durmió hasta
# la primera guía foránea, error 1129 "State code not founded").
CP_ESTADO = {
    "00": "DF", "01": "DF", "02": "DF", "03": "DF", "04": "DF", "05": "DF", "06": "DF",
    "07": "DF", "08": "DF", "09": "DF", "10": "DF", "11": "DF", "12": "DF", "13": "DF",
    "14": "DF", "15": "DF", "16": "DF",
    "20": "AGS", "21": "BC", "22": "BC", "23": "BCS", "24": "CAMP", "25": "COAH",
    "26": "COAH", "27": "COAH", "28": "COL", "29": "CHIS", "30": "CHIS", "31": "CHIH",
    "32": "CHIH", "33": "CHIH", "34": "DGO", "35": "DGO", "36": "GTO", "37": "GTO",
    "38": "GTO", "39": "GRO", "40": "GRO", "41": "GRO", "42": "HGO", "43": "HGO",
    "44": "JAL", "45": "JAL", "46": "JAL", "47": "JAL", "48": "JAL", "49": "JAL",
    "50": "MEX", "51": "MEX", "52": "MEX", "53": "MEX", "54": "MEX", "55": "MEX",
    "56": "MEX", "57": "MEX", "58": "MICH", "59": "MICH", "60": "MICH", "61": "MICH",
    "62": "MOR", "63": "NAY", "64": "NL", "65": "NL", "66": "NL", "67": "NL",
    "68": "OAX", "69": "OAX", "70": "OAX", "71": "OAX", "72": "PUE", "73": "PUE",
    "74": "PUE", "75": "PUE", "76": "QRO", "77": "Q ROO", "78": "SLP", "79": "SLP",
    "80": "SIN", "81": "SIN", "82": "SIN", "83": "SON", "84": "SON", "85": "SON",
    "86": "TAB", "87": "TAMPS", "88": "TAMPS", "89": "TAMPS", "90": "TLAX",
    "91": "VER", "92": "VER", "93": "VER", "94": "VER", "95": "VER", "96": "VER",
    "97": "YUC", "98": "ZAC", "99": "ZAC",
}

def sanear_texto(texto):
    """La API de Envia truena con em-dashes y símbolos raros: a ASCII seguro."""
    reemplazos = {"—": "-", "–": "-", "\u2019": "'", "\u201c": '"', "\u201d": '"', "º": "", "ª": ""}
    for malo, bueno in reemplazos.items():
        texto = texto.replace(malo, bueno)
    return texto


def dims_para(peso_kg):
    """Caja estándar por peso: six (<6 kg) o caja grande."""
    return (28, 19, 18) if Decimal(peso_kg) < 6 else (40, 30, 26)


def _redondear_peso(peso_kg):
    """Al 0.5 superior, mínimo 0.5 — la llave del caché."""
    medio = (Decimal(str(peso_kg)) * 2).to_integral_value(rounding="ROUND_CEILING") / Decimal(2)
    return max(medio, Decimal("0.5"))


def cotizar_lane(cp_destino, peso_kg, dims=None, cliente=None):
    """Tarifas por carrier para un (CP, peso). Caché primero; incluye negativos.

    Cada carrier faltante se cotiza a través del adapter de su proveedor
    (services.get_adapter_cotizacion): el planificador no sabe quién responde.
    Cliente con integración 99minutos → UN solo carrier directo y SIN
    CotizacionCache: el caché no distingue proveedor y mezclaría tarifas de
    envia con las directas."""
    peso = _redondear_peso(peso_kg)
    dims = dims or dims_para(peso)
    if cliente is not None and getattr(cliente, "integracion_envios", "") == "99minutos":
        from .services import cotizar_lane_carrier  # lazy: evita ciclo en carga
        return [cotizar_lane_carrier("noventa9Minutos", cp_destino, peso, dims, cliente=cliente)]
    carriers = settings.TORRE["CARRIERS_COTIZAR"]
    vigencia = timezone.now() - timedelta(days=settings.TORRE["COTIZACION_CACHE_DIAS"])
    # Un "no cotiza" puede ser falla transitoria de la API: caduca en horas,
    # no en días, para no dejar un lane bueno envenenado una semana.
    vigencia_negativos = timezone.now() - timedelta(hours=6)

    cache = {}
    for c in CotizacionCache.objects.filter(cp_destino=cp_destino, peso_kg=peso, ts__gte=vigencia):
        if not c.ok and c.ts < vigencia_negativos:
            continue
        cache[c.carrier] = c
    faltantes = [c for c in carriers if c not in cache]
    if faltantes:
        from .services import cotizar_lane_carrier  # lazy: evita ciclo en carga

        def _cotizar(carrier):
            return cotizar_lane_carrier(carrier, cp_destino, peso, dims)

        with ThreadPoolExecutor(max_workers=min(len(faltantes), 6)) as pool:
            nuevas = list(pool.map(_cotizar, faltantes))
        for fila in nuevas:
            obj, _ = CotizacionCache.objects.update_or_create(
                cp_destino=cp_destino, peso_kg=peso, carrier=fila["carrier"],
                defaults={"servicio": fila["servicio"], "precio": fila["precio"],
                          "estimado_entrega": fila["estimado"], "ok": fila["ok"]},
            )
            cache[fila["carrier"]] = obj

    return [
        {"carrier": c.carrier, "servicio": c.servicio, "precio": c.precio,
         "estimado": c.estimado_entrega, "ok": c.ok}
        for c in (cache[nombre] for nombre in carriers if nombre in cache)
    ]


def mejor_opcion(cp_destino, peso_kg, dims=None, cliente=None):
    """La tarifa más barata que sí cotiza, o None si nadie cubre el lane."""
    opciones = [f for f in cotizar_lane(cp_destino, peso_kg, dims, cliente=cliente) if f["ok"] and f["precio"] is not None]
    return min(opciones, key=lambda f: f["precio"]) if opciones else None


# ── Particionado ──────────────────────────────────────────────────────

MARGEN_EMPAQUE = Decimal("1.05")  # +5% de caja/burbuja/separadores


def _unidades(pedido):
    """[(linea, peso_kg, fraccion_de)] pieza por pieza, pesadas primero.

    Un SKU con empaques_divisibles > 1 (ej. caja de 24 → 2 medias de 12) se
    expande en subunidades: el 3PL puede reempacarlo en cajas del cliente para
    que cada envío quede más barato — la razón de ser de la división.
    """
    unidades = []
    for linea in pedido.lineas.select_related("sku").all():
        peso = Decimal(linea.sku.peso_gr or 1000) / 1000
        partes = max(int(linea.sku.empaques_divisibles or 1), 1)
        if partes > 1:
            peso_sub = (peso / partes).quantize(Decimal("0.001"))
            unidades.extend([(linea, peso_sub, partes)] * (linea.cantidad * partes))
        else:
            unidades.extend([(linea, peso, 1)] * linea.cantidad)
    unidades.sort(key=lambda u: u[1], reverse=True)
    return unidades


def _bins_por_capacidad(unidades, capacidad_kg):
    """First-fit decreciente. Regresa [ [ (linea, peso), ... ], ... ] o None si algo no cabe."""
    capacidad = Decimal(str(capacidad_kg))
    bins, pesos = [], []
    for unidad in unidades:
        peso = unidad[1] * MARGEN_EMPAQUE
        if peso > capacidad:
            return None
        for i, ocupado in enumerate(pesos):
            if ocupado + peso <= capacidad:
                bins[i].append(unidad)
                pesos[i] += peso
                break
        else:
            bins.append([unidad])
            pesos.append(peso)
    return bins


def _peso_bin(unidades_bin):
    bruto = sum(u[1] for u in unidades_bin) * MARGEN_EMPAQUE
    return bruto.quantize(Decimal("0.01"))


def _particiones_candidatas(unidades, max_kg):
    """Particiones a evaluar: junta / chunks ≤9 (puntopost) / mitades / chunks ≤max."""
    candidatas = []
    total = sum(u[1] for u in unidades) * MARGEN_EMPAQUE
    if total <= max_kg:
        candidatas.append([list(unidades)])
    for capacidad in (9, max_kg - 1, max_kg):
        bins = _bins_por_capacidad(unidades, capacidad)
        if bins:
            candidatas.append(bins)
    if len(unidades) > 1:
        mitad_a, mitad_b, peso_a, peso_b = [], [], Decimal(0), Decimal(0)
        for unidad in unidades:  # ya vienen pesadas-primero: balanceo greedy
            if peso_a <= peso_b:
                mitad_a.append(unidad); peso_a += unidad[1]
            else:
                mitad_b.append(unidad); peso_b += unidad[1]
        if mitad_a and mitad_b and _peso_bin(mitad_a) <= max_kg and _peso_bin(mitad_b) <= max_kg:
            candidatas.append([mitad_a, mitad_b])
    # dedupe por firma de pesos
    unicas, vistas = [], set()
    for bins in candidatas:
        firma = tuple(sorted(_peso_bin(b) for b in bins))
        if firma not in vistas:
            vistas.add(firma)
            unicas.append(bins)
    return unicas


def _costo_particion(cp_destino, bins, cliente=None):
    """(costo_total, [opcion por bin]) con UN SOLO carrier para todo el plan.

    Regla operativa: todas las cajas de un pedido viajan con el MISMO carrier.
    El manifiesto de salida se firma por corral (= por carrier): un plan
    mixto (caja PQX + caja estafeta) caería entero al corral de una sola guía
    y la otra caja saldría sin manifiesto. Por eso aquí solo se combinan bins
    del mismo carrier: se elige el carrier que cotice TODOS los bins con el
    menor total. (None, None) si ningún carrier cubre la partición completa.
    """
    cotizaciones = []
    for unidades_bin in bins:
        filas = {
            f["carrier"]: f
            for f in cotizar_lane(cp_destino, _peso_bin(unidades_bin), cliente=cliente)
            if f["ok"] and f["precio"] is not None
        }
        if not filas:
            return None, None
        cotizaciones.append(filas)
    comunes = set(cotizaciones[0])
    for filas in cotizaciones[1:]:
        comunes &= set(filas)
    if not comunes:
        return None, None
    total, carrier = min(
        (sum((filas[c]["precio"] for filas in cotizaciones), Decimal(0)), c)
        for c in comunes
    )
    return total, [filas[carrier] for filas in cotizaciones]


@transaction.atomic
def planificar_envio(pedido, force=False):
    """Divide el pedido en paquetes cotizando particiones reales. Idempotente."""
    existentes = list(pedido.paquetes.all())
    if existentes and not force:
        return existentes
    if any(p.estado == Paquete.DESPACHADO for p in existentes):
        return existentes
    for paquete in existentes:  # replanear: solo se tiran planes sin guía
        if not paquete.guias.exists():
            paquete.delete()

    max_kg = Decimal(str(settings.TORRE["MAX_PESO_ENVIO_KG"]))
    meta = Decimal(str(settings.TORRE["TARIFA_OBJETIVO_MXN"]))

    # El atajo local pasa por el motor de reglas: una ReglaEnvio explícita
    # puede mandar un pedido local con carrier externo (y entonces se cotiza).
    from .services import CARRIER_LOCAL, elegir_carrier  # lazy: evita ciclo
    if pedido.es_local and elegir_carrier(pedido)[0] == CARRIER_LOCAL:
        # Flota local: $100 flat por paquete ≤20 kg (CDMX + metro hasta Toluca).
        tarifa_local = Decimal(str(settings.TORRE.get("TARIFA_LOCAL_MXN", 100)))
        unidades = _unidades(pedido)
        bins = _bins_por_capacidad(unidades, max_kg) if unidades else None
        if not bins:
            bins = [unidades] if unidades else [[]]
        paquetes = []
        for i, unidades_bin in enumerate(bins, start=1):
            peso = _peso_bin(unidades_bin) if unidades_bin else _peso_total(pedido)
            paquete = Paquete.objects.create(
                pedido=pedido, numero=i, peso_kg=peso,
                carrier="local", servicio="entrega_local",
                precio_cotizado=tarifa_local,
                fuera_de_meta=tarifa_local > meta,
            )
            if unidades_bin:
                _copiar_unidades(paquete, unidades_bin)
            else:
                _copiar_lineas(paquete, list(pedido.lineas.all()))
            paquetes.append(paquete)
        registrar_evento(
            "pedido", pedido.pk, "plan_envio", cliente=pedido.cliente,
            delta={"paquetes": len(paquetes), "costo_total": float(tarifa_local * len(paquetes)),
                   "modalidad": "entrega_local_flat"},
            motivo=f"Entrega local: {len(paquetes)} paquete(s) × ${tarifa_local} flat",
        )
        return paquetes

    unidades = _unidades(pedido)
    if not unidades:
        return []

    candidatas = _particiones_candidatas(unidades, max_kg)
    evaluadas, viables = [], []
    for bins in candidatas:
        costo, opciones = _costo_particion(pedido.cp, bins, cliente=pedido.cliente)
        evaluadas.append({"bins": [float(_peso_bin(b)) for b in bins],
                          "costo": float(costo) if costo is not None else None})
        if costo is not None:
            viables.append((costo, len(bins), bins, opciones))

    if not viables:
        raise ValueError(
            f"Ningún carrier cotiza el pedido {pedido.folio} a CP {pedido.cp} "
            f"con paquetes ≤{max_kg} kg. Revisar con Mesa de Control."
        )

    costo_elegido, _, bins_elegidos, opciones = min(viables, key=lambda v: (v[0], v[1]))

    # Ahorro vs mandarlo entero (aunque entero viole el tope, solo para el dato).
    entero = mejor_opcion(pedido.cp, _peso_bin(unidades), cliente=pedido.cliente)
    ahorro = max(Decimal(entero["precio"]) - costo_elegido, Decimal(0)) if entero else Decimal(0)

    paquetes = []
    for i, (unidades_bin, opcion) in enumerate(zip(bins_elegidos, opciones), start=1):
        peso = _peso_bin(unidades_bin)
        largo, ancho, alto = dims_para(peso)
        paquete = Paquete.objects.create(
            pedido=pedido, numero=i, peso_kg=peso,
            largo_cm=largo, ancho_cm=ancho, alto_cm=alto,
            carrier=opcion["carrier"], servicio=opcion["servicio"],
            precio_cotizado=opcion["precio"],
            fuera_de_meta=opcion["precio"] > meta,
            ahorro_plan_mxn=ahorro,
        )
        _copiar_unidades(paquete, unidades_bin)
        paquetes.append(paquete)

    registrar_evento(
        "pedido", pedido.pk, "plan_envio", cliente=pedido.cliente,
        delta={
            "paquetes": len(paquetes),
            "costo_total": float(costo_elegido),
            "ahorro_vs_entero": float(ahorro),
            "particiones_evaluadas": evaluadas,
            "fuera_de_meta": [p.numero for p in paquetes if p.fuera_de_meta],
        },
        motivo=f"División de envío: {len(paquetes)} paquete(s), total ${costo_elegido}",
    )
    return paquetes


def _peso_total(pedido):
    total = sum(
        Decimal(l.sku.peso_gr or 1000) / 1000 * l.cantidad
        for l in pedido.lineas.select_related("sku").all()
    ) * MARGEN_EMPAQUE
    return total.quantize(Decimal("0.01")) if total else Decimal("1.00")


def _copiar_lineas(paquete, lineas):
    for linea in lineas:
        PaqueteLinea.objects.create(paquete=paquete, linea_pedido=linea, cantidad=linea.cantidad)


def _copiar_unidades(paquete, unidades_bin):
    conteo, fracciones, lineas = {}, {}, {}
    for linea, _, fraccion in unidades_bin:
        conteo[linea.pk] = conteo.get(linea.pk, 0) + 1
        fracciones[linea.pk] = fraccion
        lineas[linea.pk] = linea
    for pk, cantidad in conteo.items():
        PaqueteLinea.objects.create(
            paquete=paquete, linea_pedido=lineas[pk],
            cantidad=cantidad, fraccion_de=fracciones[pk],
        )
