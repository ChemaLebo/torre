"""Cotización por lane y planificación de división de envíos.

Reglas de negocio (CONVENTIONS-ENVIOS.md, medidas contra la API real 2026-08-03):
- Tope duro: ningún paquete pasa de settings.TORRE["MAX_PESO_ENVIO_KG"].
- Meta: settings.TORRE["TARIFA_OBJETIVO_MXN"] por envío; si la mejor tarifa la
  excede se marca fuera_de_meta (jamás se bloquea el envío).
- La división se decide COTIZANDO particiones reales y comparando totales:
  puntopost ($86/$91, solo ≤10 kg y cobertura parcial) hace que 16 kg dividido
  en 2×8 cueste $182 vs $224 entero por estafeta.
- Preferencia antes que precio (2026-09-22): el carrier que decide
  services.carrier_preferido (la ReglaEnvio del pedido o, sin regla,
  TORRE["CARRIER_PRIORITARIO"]) gana si cotiza todas las cajas del plan; si
  no, el precio decide entre lo permitido. La regla prefiere, no acota.
"""
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

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

# Catálogo de estados de envia — GET queries.envia.com/state?country_code=MX
# (2026-09-07): (nombre, code_2_digits, code_3_digits, code_shopify). Envia
# valida direcciones con SUS códigos de 2 letras (FAQ: "Envia uses its own
# 2-letter state codes... do not reuse codes from other platforms"). El
# code_shopify — lo que manda Shopify y lo que guarda CP_ESTADO — se traduce
# con estado_envia() en el destino de cotización y guía; el origen ya va en
# 2 letras (ORIGEN_DEFAULT). Lección de PED-00015 (estafeta 1129 con DF/YUC)
# y PED-00019/20/21 (1129 con DF; CHIH no pasa ni el esquema: "String is too
# long"). Los code_shopify de 4-5 letras (CAMP, CHIS, CHIH, COAH, MICH,
# Q ROO, TAMPS, TLAX) jamás generaron.
ESTADOS_MX = [
    ("Aguascalientes", "AG", "AGS", "AGS"),
    ("Baja California", "BC", "BCN", "BC"),
    ("Baja California Sur", "BS", "BCS", "BCS"),
    ("Campeche", "CM", "CAM", "CAMP"),
    ("Chiapas", "CS", "CHP", "CHIS"),
    ("Chihuahua", "CH", "CHH", "CHIH"),
    ("Ciudad de México", "CX", "CMX", "DF"),
    ("Coahuila", "CO", "COA", "COAH"),
    ("Colima", "CL", "COL", "COL"),
    ("Durango", "DG", "DGO", "DGO"),
    ("Guanajuato", "GT", "GTO", "GTO"),
    ("Guerrero", "GR", "GRO", "GRO"),
    ("Hidalgo", "HG", "HGO", "HGO"),
    ("Jalisco", "JA", "JAL", "JAL"),
    ("México", "EM", "MEX", "MEX"),
    ("Michoacán", "MI", "MIC", "MICH"),
    ("Morelos", "MO", "MOR", "MOR"),
    ("Nayarit", "NA", "NAY", "NAY"),
    ("Nuevo León", "NL", "NLE", "NL"),
    ("Oaxaca", "OA", "OAX", "OAX"),
    ("Puebla", "PU", "PUE", "PUE"),
    ("Querétaro", "QT", "QRO", "QRO"),
    ("Quintana Roo", "QR", "ROO", "Q ROO"),
    ("San Luis Potosí", "SL", "SLP", "SLP"),
    ("Sinaloa", "SI", "SIN", "SIN"),
    ("Sonora", "SO", "SON", "SON"),
    ("Tabasco", "TB", "TAB", "TAB"),
    ("Tamaulipas", "TM", "TAM", "TAMPS"),
    ("Tlaxcala", "TL", "TLA", "TLAX"),
    ("Veracruz", "VE", "VER", "VER"),
    ("Yucatán", "YU", "YUC", "YUC"),
    ("Zacatecas", "ZA", "ZAC", "ZAC"),
]
# code_shopify → code_2_digits (lo que envia acepta en origin/destination.state).
ESTADO_ENVIA = {shopify: dos for _, dos, _, shopify in ESTADOS_MX}
# code_shopify → nombre (dropdown del pedido manual; `province` con shape Shopify).
NOMBRE_ESTADO_MX = {shopify: nombre for nombre, _, _, shopify in ESTADOS_MX}
# Choices del dropdown de estado: valor = code_shopify, etiqueta = nombre.
OPCIONES_ESTADO = [(shopify, nombre) for nombre, _, _, shopify in ESTADOS_MX]


def estado_envia(code_shopify):
    """Código de estado como lo quiere envia (2 letras) a partir del code_shopify.

    Un valor que no esté en la tabla (vacío, o ya en 2 letras porque la
    dirección se corrigió a mano) pasa derecho: jamás se inventa un estado.
    """
    clave = str(code_shopify or "").strip().upper()
    return ESTADO_ENVIA.get(clave, code_shopify)


def sanear_texto(texto):
    """La API de Envia truena con em-dashes y símbolos raros: a ASCII seguro."""
    reemplazos = {"—": "-", "–": "-", "\u2019": "'", "\u201c": '"', "\u201d": '"', "º": "", "ª": ""}
    for malo, bueno in reemplazos.items():
        texto = texto.replace(malo, bueno)
    return texto


def dims_para(peso_kg):
    """Caja estándar por peso: six (<6 kg) o caja grande."""
    return (28, 19, 18) if Decimal(peso_kg) < 6 else (40, 30, 26)


def dims_de_unidades(unidades_bin):
    """Medidas reales cuando la caja es el producto mismo: un bin con UNA pieza
    entera de un SKU con medidas regresa (largo, ancho, alto) del SKU (Chema,
    2026-09-21: el 12 pack viaja en su propia caja). Cualquier otro caso, None:
    el llamador cae a dims_para (estimado por peso)."""
    if len(unidades_bin) != 1:
        return None
    linea, _peso, fraccion = unidades_bin[0]
    sku = linea.sku
    if fraccion != 1 or not (sku.largo_cm and sku.ancho_cm and sku.alto_cm):
        return None
    return (sku.largo_cm, sku.ancho_cm, sku.alto_cm)


def _redondear_peso(peso_kg):
    """Al 0.5 superior, mínimo 0.5 — la llave del caché."""
    medio = (Decimal(str(peso_kg)) * 2).to_integral_value(rounding="ROUND_CEILING") / Decimal(2)
    return max(medio, Decimal("0.5"))


def cotizar_lane(cp_destino, peso_kg, dims=None, cliente=None, carriers=None):
    """Tarifas por carrier para un (CP, peso). Caché primero; incluye negativos.

    Cada carrier faltante se cotiza a través del adapter de su proveedor
    (services.get_adapter_cotizacion): el planificador no sabe quién responde.
    Los carriers que viajan por un proveedor DIRECTO (99minutos: por el flip
    del cliente o por TORRE["PROVEEDOR_POR_CARRIER"]) se cotizan sin
    CotizacionCache: el caché no distingue proveedor y mezclaría tarifas de
    envia con las directas. `carriers` acota la lista (la carta del reparto,
    la paquetería forzada por Mesa); sin él, cliente 99minutos = solo
    noventa9Minutos y los demás la lista blanca CARRIERS_COTIZAR."""
    from .services import PROVEEDOR_99MIN, _proveedor_para, cotizar_lane_carrier  # lazy: evita ciclo

    peso = _redondear_peso(peso_kg)
    dims = dims or dims_para(peso)
    if not carriers:
        if cliente is not None and getattr(cliente, "integracion_envios", "") == "99minutos":
            carriers = ["noventa9Minutos"]
        else:
            carriers = settings.TORRE["CARRIERS_COTIZAR"]
    carriers = list(carriers)
    directos = {c for c in carriers if _proveedor_para(c, cliente) == PROVEEDOR_99MIN}
    filas_directas = {
        c: cotizar_lane_carrier(c, cp_destino, peso, dims, cliente=cliente) for c in carriers if c in directos
    }
    carriers_cache = [c for c in carriers if c not in directos]
    vigencia = timezone.now() - timedelta(days=settings.TORRE["COTIZACION_CACHE_DIAS"])
    # Un "no cotiza" puede ser falla transitoria de la API: caduca en horas,
    # no en días, para no dejar un lane bueno envenenado una semana.
    vigencia_negativos = timezone.now() - timedelta(hours=6)

    cache = {}
    for c in CotizacionCache.objects.filter(cp_destino=cp_destino, peso_kg=peso, ts__gte=vigencia):
        if not c.ok and c.ts < vigencia_negativos:
            continue
        cache[c.carrier] = c
    faltantes = [c for c in carriers_cache if c not in cache]
    if faltantes:
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

    filas = []
    for nombre in carriers:
        if nombre in filas_directas:
            filas.append(filas_directas[nombre])
        elif nombre in cache:
            c = cache[nombre]
            filas.append({"carrier": c.carrier, "servicio": c.servicio, "precio": c.precio,
                          "estimado": c.estimado_entrega, "ok": c.ok})
    return filas


def carrier_prioritario():
    """TORRE["CARRIER_PRIORITARIO"]: el carrier que gana siempre que cotice,
    sin importar el precio, cuando NINGUNA ReglaEnvio aplica al pedido (iMile
    por acuerdo con Colima, 2026-09-21). "" = el más barato manda. Quien tiene
    el pedido a la mano pasa services.carrier_preferido(pedido) en su lugar."""
    return settings.TORRE.get("CARRIER_PRIORITARIO") or ""


def elegir_entre(opciones, preferido=None):
    """La opción que manda entre cotizaciones válidas: la del carrier
    preferido si está, si no la más barata. `preferido` es lo que decidió
    services.carrier_preferido (la ReglaEnvio del pedido o, sin regla, el
    prioritario global); None = el prioritario global; "" = solo precio.
    None sin opciones."""
    if not opciones:
        return None
    if preferido is None:
        preferido = carrier_prioritario()
    if preferido:
        for f in opciones:
            if f["carrier"] == preferido:
                return f
    return min(opciones, key=lambda f: f["precio"])


def mejor_opcion(cp_destino, peso_kg, dims=None, cliente=None, carriers=None, preferido=None):
    """La opción que manda entre las que sí cotizan (elegir_entre: el carrier
    preferido si cotiza, si no la más barata), o None si nadie cubre el lane."""
    opciones = [
        f for f in cotizar_lane(cp_destino, peso_kg, dims, cliente=cliente, carriers=carriers)
        if f["ok"] and f["precio"] is not None
    ]
    return elegir_entre(opciones, preferido)


# ── Particionado ──────────────────────────────────────────────────────

MARGEN_EMPAQUE = Decimal("1.05")  # +5% de caja/burbuja/separadores


def _unidades(pedido, cubiertas=None):
    """[(linea, peso_kg, fraccion_de)] pieza por pieza, pesadas primero.

    Un SKU con empaques_divisibles > 1 (ej. caja de 24 → 2 medias de 12) se
    expande en subunidades: el 3PL puede reempacarlo en cajas del cliente para
    que cada envío quede más barato — la razón de ser de la división.

    Fulfillment parcial (2026-09-22): solo entra lo que ESTA ola surte. Una
    línea faltante (sin inventario) no se planea; lo que ya salió en un
    manifiesto (cantidad_despachada) o ya viaja en una caja fija con guía
    (`cubiertas`: {linea_pk: unidades}) tampoco.
    """
    cubiertas = cubiertas or {}
    unidades = []
    for linea in pedido.lineas.select_related("sku").all():
        if linea.faltante:
            continue
        ya = max(linea.cantidad_despachada, cubiertas.get(linea.pk, 0))
        por_planear = max(linea.cantidad - ya, 0)
        if por_planear <= 0:
            continue
        peso = Decimal(linea.sku.peso_gr or 1000) / 1000
        partes = max(int(linea.sku.empaques_divisibles or 1), 1)
        if partes > 1:
            peso_sub = (peso / partes).quantize(Decimal("0.001"))
            unidades.extend([(linea, peso_sub, partes)] * (por_planear * partes))
        else:
            unidades.extend([(linea, peso, 1)] * por_planear)
    unidades.sort(key=lambda u: u[1], reverse=True)
    return unidades


def _unidades_cubiertas(paquetes):
    """{linea_pk: unidades de venta} que ya viajan en esas cajas (fijas: con
    guía o despachadas). Las medias cajas se suman exactas y se redondean
    hacia arriba: la unidad de venta ya se abrió y no se vuelve a planear."""
    acumulado = {}
    for paquete in paquetes:
        for pl in paquete.lineas.all():
            fraccion = Fraction(pl.cantidad, max(pl.fraccion_de, 1))
            acumulado[pl.linea_pedido_id] = acumulado.get(pl.linea_pedido_id, Fraction(0)) + fraccion
    return {pk: math.ceil(v) for pk, v in acumulado.items()}


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
    """Particiones a evaluar: junta / chunks ≤9 (puntopost) / chunks a 12 y
    15 kg (2026-09-24: 99minutos no cotiza cajas grandes y sin tallas
    intermedias el único plan viable era una unidad por caja, PED-00109 en 5
    cajas) / mitades / chunks ≤max."""
    candidatas = []
    total = sum(u[1] for u in unidades) * MARGEN_EMPAQUE
    if total <= max_kg:
        candidatas.append([list(unidades)])
    capacidades = sorted({c for c in (Decimal(9), Decimal(12), Decimal(15), max_kg - 1, max_kg) if c <= max_kg})
    for capacidad in capacidades:
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


def _costo_particion(cp_destino, bins, cliente=None, carriers=None, preferido=None):
    """(costo_total, [opcion por bin]) con UN SOLO carrier para todo el plan.

    Regla operativa: todas las cajas de un pedido viajan con el MISMO carrier.
    El manifiesto de salida se firma por corral (= por carrier): un plan
    mixto (caja PQX + caja estafeta) caería entero al corral de una sola guía
    y la otra caja saldría sin manifiesto. Por eso aquí solo se combinan bins
    del mismo carrier: gana el preferido (services.carrier_preferido; None =
    el prioritario global) si cotiza TODOS los bins, si no el carrier que los
    cotice todos con el menor total. (None, None) si ningún carrier cubre la
    partición completa.
    """
    cotizaciones = []
    for unidades_bin in bins:
        filas = {
            f["carrier"]: f
            for f in cotizar_lane(cp_destino, _peso_bin(unidades_bin), cliente=cliente, carriers=carriers)
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
    if preferido is None:
        preferido = carrier_prioritario()
    if preferido in comunes:  # cotiza todos los bins: gana aunque sea más caro
        carrier = preferido
        total = sum((filas[carrier]["precio"] for filas in cotizaciones), Decimal(0))
    else:
        total, carrier = min(
            (sum((filas[c]["precio"] for filas in cotizaciones), Decimal(0)), c)
            for c in comunes
        )
    return total, [filas[carrier] for filas in cotizaciones]


def planificar_envio(pedido, force=False):
    """Divide el pedido en paquetes cotizando particiones reales. Idempotente.

    La config vigente acota qué se cotiza (services.carriers_del_pedido: la
    carta del reparto por porcentajes, el directo de 99minutos o la lista
    blanca) y la ReglaEnvio del pedido PREFIERE sin acotar
    (services.carrier_preferido: gana si cotiza todas las cajas, si no manda
    el precio). La carta se saca FUERA del atomic del plan: si nadie cotiza,
    la carta se queda con el pedido (dato del fallo) en vez de revertirse."""
    from .services import carrier_preferido, carriers_del_pedido  # lazy: evita ciclo

    carriers = carriers_del_pedido(pedido)
    preferido = carrier_preferido(pedido)
    return _planificar(pedido, force, carriers, preferido)


@transaction.atomic
def _planificar(pedido, force, carriers, preferido=None):
    """El plan en sí (ver planificar_envio). Las cajas ya despachadas o con
    guía son FIJAS (la ola anterior) y no se tocan; un plan vivo (sin guía)
    se conserva salvo `force`. Fulfillment parcial: se planean cajas nuevas
    solo con lo pendiente (lo que ya viaja en las fijas queda fuera),
    numeradas después de las que salieron; regresa fijas + nuevas."""
    existentes = list(pedido.paquetes.prefetch_related("lineas", "guias"))
    # Fija = ya salió o tiene guía VIVA; una caja cuya guía se canceló vuelve
    # a ser viva y se replanea (2026-09-24: con guías cancelables, contar
    # cualquier guía dejaba las cajas clavadas para siempre).
    fijas = [p for p in existentes if p.estado == Paquete.DESPACHADO or any(g.es_activa for g in p.guias.all())]
    vivas = [p for p in existentes if p not in fijas]
    if vivas and not force:
        return existentes
    for paquete in vivas:  # replanear: solo se tiran planes sin guía
        paquete.delete()
    desde = max((p.numero for p in fijas), default=0) + 1
    unidades = _unidades(pedido, _unidades_cubiertas(fijas))
    if not unidades:
        return fijas

    max_kg = Decimal(str(settings.TORRE["MAX_PESO_ENVIO_KG"]))
    meta = Decimal(str(settings.TORRE["TARIFA_OBJETIVO_MXN"]))

    # El atajo local pasa por el motor de reglas: una ReglaEnvio explícita
    # puede mandar un pedido local con carrier externo (y entonces se cotiza).
    from .services import CARRIER_LOCAL, elegir_carrier  # lazy: evita ciclo
    if pedido.es_local and elegir_carrier(pedido)[0] == CARRIER_LOCAL:
        # Flota local: $100 flat por paquete ≤20 kg (CDMX + metro hasta Toluca).
        tarifa_local = Decimal(str(settings.TORRE.get("TARIFA_LOCAL_MXN", 100)))
        bins = _bins_por_capacidad(unidades, max_kg) or [unidades]
        paquetes = []
        for i, unidades_bin in enumerate(bins, start=desde):
            peso = _peso_bin(unidades_bin)
            largo, ancho, alto = dims_de_unidades(unidades_bin) or dims_para(peso)
            paquete = Paquete.objects.create(
                pedido=pedido, numero=i, peso_kg=peso,
                largo_cm=largo, ancho_cm=ancho, alto_cm=alto,
                carrier="local", servicio="entrega_local",
                precio_cotizado=tarifa_local,
                fuera_de_meta=tarifa_local > meta,
            )
            _copiar_unidades(paquete, unidades_bin)
            paquetes.append(paquete)
        registrar_evento(
            "pedido", pedido.pk, "plan_envio", cliente=pedido.cliente,
            delta={"paquetes": len(paquetes), "costo_total": float(tarifa_local * len(paquetes)),
                   "modalidad": "entrega_local_flat", "cajas_previas": [p.numero for p in fijas]},
            motivo=f"Entrega local: {len(paquetes)} paquete(s) × ${tarifa_local} flat",
        )
        return fijas + paquetes

    candidatas = _particiones_candidatas(unidades, max_kg)
    evaluadas, viables = [], []
    for bins in candidatas:
        costo, opciones = _costo_particion(
            pedido.cp, bins, cliente=pedido.cliente, carriers=carriers, preferido=preferido,
        )
        evaluadas.append({"bins": [float(_peso_bin(b)) for b in bins],
                          "costo": float(costo) if costo is not None else None})
        if costo is not None:
            viables.append((costo, len(bins), bins, opciones))

    if not viables:
        # Con quién se intentó (Chema 2026-09-24): la incidencia "Sin
        # paquetería" y la nota del replaneo lo muestran tal cual.
        intentados = ", ".join(carriers) if carriers else "ninguno configurado"
        raise ValueError(
            f"Ningún carrier cotiza el pedido {pedido.folio} a CP {pedido.cp} "
            f"con paquetes ≤{max_kg} kg (se intentó con: {intentados}). Revisar con Mesa de Control."
        )

    costo_elegido, _, bins_elegidos, opciones = min(viables, key=lambda v: (v[0], v[1]))

    # Ahorro vs mandarlo entero (aunque entero viole el tope, solo para el dato).
    entero = mejor_opcion(
        pedido.cp, _peso_bin(unidades), cliente=pedido.cliente, carriers=carriers, preferido=preferido,
    )
    ahorro = max(Decimal(entero["precio"]) - costo_elegido, Decimal(0)) if entero else Decimal(0)

    paquetes = []
    for i, (unidades_bin, opcion) in enumerate(zip(bins_elegidos, opciones), start=desde):
        peso = _peso_bin(unidades_bin)
        largo, ancho, alto = dims_de_unidades(unidades_bin) or dims_para(peso)
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
            "cajas_previas": [p.numero for p in fijas],
        },
        motivo=f"División de envío: {len(paquetes)} paquete(s), total ${costo_elegido}",
    )
    return fijas + paquetes


def _copiar_unidades(paquete, unidades_bin):
    """PaqueteLinea por línea del bin: cuántas unidades (o subunidades) van en la caja."""
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
