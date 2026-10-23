"""Reporte de fulfillment (Chema 2026-10-22): UNA FILA POR GUÍA de los pedidos
INGRESADOS en el periodo (entran todas sus guías aunque se hayan comprado
fuera del rango), con lo que el cliente, Mesa y Finanzas necesitan en un solo
lugar: pedido y orden de Shopify, tipo de envío (original, reposición,
reexpedición, cancelada, sustituida, devolución), paquetería, proveedor y
servicio, caja con dimensiones y pesos (real de báscula, volumétrico L×A×H/5000,
declarado al carrier y el que pesó el carrier si lo reportó), piezas, SKUs y
lotes con caducidad, origen y destino (CP, ciudad, estado, zona de cobro),
tiempos (en piso, hasta salir por manifiesto, hasta la recolección del carrier
y de entrega) e importes (facturado sin y con IVA, reembolsos, picking y
empaque; Mesa además cotizado al planear, costo inicial, costo final y la
diferencia sin y con IVA).

Lo que no aplica va vacío, nunca en cero. Los grupos (cada uno con su CSV y su
gráfica) son los cortes del punto 4 del alcance: productos por guía, volumen
diario y mensual, guías por pedido y distribuciones por zona, ciudad, CP,
dimensiones, peso real, rango cobrado y paquetería. Todo filtro aplica a la
tabla, los grupos, el resumen y los CSV.

Fuentes (verificadas en código, sin cambiar nada fuera del reporte):
- Zona y ciudad del rango: configuracion.ZonaCP vía finanzas.zonas; estado y
  ciudad del destino: reportes.destinos.destino (catálogo de localidades).
- Tarifa de envío: finanzas.tarifa_envio_de (zona × envio_bloque del tarifario
  del cliente; el "rango cobrado" es la zona: los bloques de kg quedaron atrás
  el 2026-09-28). Reembolsos: finanzas.ReembolsoGuia (cancelada sin registro =
  reembolsada, como Finanzas). Picking y empaque una vez por pedido.
- Costo inicial: Guia.costo_preferencial (lo cobrado al comprar); costo final:
  grand_total del objeto de envío de envia que el poller refresca en Guia.raw
  (sin eso, = inicial). Los costos de carrier traen IVA: sin IVA = / (1 + IVA).
- Lotes: pedidos.PickLinea por caja (desde el 2026-10-22); antes, por línea.
"""
from collections import Counter, OrderedDict, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.envios.models import Guia, LocalidadCP, Paquete
from apps.pedidos.models import Pedido

from . import destinos
from .base import dinero, horas

CLAVE = "fulfillment"
TITULO = "Fulfillment por guía"
DESCRIPCION = (
    "Una fila por guía de los pedidos ingresados en el periodo: tipo de envío, paquetería, caja, pesos, "
    "productos y lotes, destino y zona, tiempos e importes facturados. Lo que no aplica va vacío. "
    "Los filtros aplican a la tabla, los cortes, el resumen y los CSV."
)
CON_FECHAS = True
SOLO_MESA = True  # el portal queda fuera del alcance por ahora (Chema 2026-10-22); se replica quitando columnas

ORIGINAL, REPOSICION, REEXPEDICION, CANCELADA, SUSTITUIDA, DEVOLUCION = (
    "original", "reposición", "reexpedición", "cancelada", "sustituida", "devolución",
)
TIPOS = [(t, t.capitalize()) for t in (ORIGINAL, REPOSICION, REEXPEDICION, CANCELADA, SUSTITUIDA, DEVOLUCION)]
ZONAS = [("local", "Local"), ("metro", "Metropolitana"), ("nacional", "Foránea")]
NOMBRE_ZONA = dict(ZONAS)
DIVISOR_VOLUMETRICO = 5000  # cm³ por kg: el de envia.com y la mayoría de los carriers en México
RANGOS_PESO = [(0, 1), (1, 3), (3, 5), (5, 10), (10, 20), (20, None)]


def _carriers():
    return [(c, c) for c in Guia.objects.exclude(carrier="").values_list("carrier", flat=True).distinct().order_by("carrier")]


def _origenes():
    from apps.configuracion.models import Origen  # lazy: modelo de otra app

    return [("", "Todos")] + [(str(o.pk), f"{o.nombre} · {o.cp}") for o in Origen.objects.filter(activo=True)]


FILTROS = [
    {"nombre": "carrier", "etiqueta": "Paqueterías", "tipo": "multiselect", "solo_mesa": True, "opciones": _carriers},
    {"nombre": "tipo", "etiqueta": "Tipo de envío", "tipo": "multiselect", "opciones": TIPOS},
    {"nombre": "estado_guia", "etiqueta": "Estado del movimiento", "tipo": "multiselect", "opciones": Guia.ESTADOS},
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "multiselect", "opciones": ZONAS},
    {"nombre": "sku", "etiqueta": "SKU", "tipo": "text", "default": "", "placeholder": "código o parte"},
    {"nombre": "origen", "etiqueta": "Origen", "tipo": "select", "default": "", "opciones": _origenes},
    {"nombre": "destino", "etiqueta": "Destino", "tipo": "text", "default": "", "placeholder": "CP, ciudad o estado"},
    {"nombre": "dimensiones", "etiqueta": "Dimensiones (cm)", "tipo": "text", "default": "", "placeholder": "40x30x26"},
    {"nombre": "peso", "etiqueta": "Peso real (kg)", "tipo": "rango"},
]
COLUMNAS = [
    ("Pedido", "texto"), ("Orden Shopify", "texto"), ("Fecha pedido", "fechahora"), ("Tipo", "texto"),
    ("Guía", "texto"), ("Paquetería", "texto"), ("Proveedor", "texto"), ("Servicio", "texto"),
    ("Caja", "texto"), ("Dimensiones cm", "texto"), ("Peso real kg", "decimal"), ("Peso volumétrico kg", "decimal"),
    ("Peso declarado kg", "decimal"), ("Peso carrier kg", "decimal"), ("Cobrado por", "texto"),
    ("Piezas", "entero"), ("SKUs", "texto"), ("Lotes", "texto"),
    ("CP origen", "texto"), ("CP destino", "texto"), ("Ciudad", "texto"), ("Estado", "texto"), ("Zona", "texto"),
    ("Estado de la guía", "texto"),
    ("Horas en piso", "horas"), ("Horas hasta salir", "horas"), ("Horas hasta recolección", "horas"), ("Horas de entrega", "horas"),
    ("Envío facturado sin IVA", "dinero"), ("Envío facturado con IVA", "dinero"), ("Reembolso", "dinero"),
    ("Picking", "dinero"), ("Empaque", "dinero"),
]
COLUMNAS_MESA = [
    ("Cotizado al planear", "dinero"), ("Costo inicial", "dinero"), ("Costo final", "dinero"), ("Costo final sin IVA", "dinero"),
    ("Recargo carrier", "dinero"), ("Diferencia sin IVA", "dinero"), ("Diferencia con IVA", "dinero"),
]
COLUMNAS_PRODUCTOS = [
    ("Pedido", "texto"), ("Guía", "texto"), ("Caja", "texto"), ("SKU", "texto"), ("Producto", "texto"),
    ("Piezas", "entero"), ("Lote", "texto"), ("Caducidad", "fecha"),
]
COLUMNAS_VOLUMEN = [("Periodo", "texto"), ("Pedidos", "entero"), ("Guías", "entero"), ("Paquetes", "entero"), ("Piezas", "entero")]
COLUMNAS_CORTE = [("Grupo", "texto"), ("Guías", "entero"), ("% de guías", "pct"), ("Piezas", "entero"), ("Envío facturado sin IVA", "dinero")]


# ── helpers de una guía ───────────────────────────────────────────────────────

def _iva():
    return Decimal(str(settings.TORRE.get("IVA", 0.16)))


def _sin_iva(monto):
    return (Decimal(monto) / (1 + _iva())).quantize(Decimal("0.01")) if monto is not None else None


def _kg(gramos):
    return round(gramos / 1000.0, 2) if gramos else None


def _datos_envia(raw):
    """Lo útil del objeto de envío de envia.com (compra y rastreos): pesos y total final.
    Vacío para 99minutos, locales, mock o guías sin crudo."""
    if not isinstance(raw, dict) or not any(k in raw for k in ("shipment_weight", "grand_total", "shipment_real_weight")):
        return {}
    def numero(clave):
        try:
            return float(raw[clave]) if raw.get(clave) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {
        "declarado_kg": numero("shipment_weight"), "carrier_kg": numero("shipment_real_weight"),
        "total": numero("grand_total") if numero("grand_total") is not None else numero("total"),
        "recargo": numero("overcharge_cost") if raw.get("overcharge_applied") else None,
    }


def _tipo(guia, caja):
    if guia.estado == Guia.CANCELADA:
        return CANCELADA
    if guia.estado == Guia.SUSTITUIDA:
        return SUSTITUIDA
    if guia.estado == Guia.RETORNO or (caja is not None and (caja.regreso_reportado or caja.reingreso_estado)):
        return DEVOLUCION
    if caja is not None and any(pl.repone_a_id for pl in caja.lineas.all()):
        return REPOSICION
    if guia.sustituye_a_id:
        return REEXPEDICION
    return ORIGINAL


def _lotes_de(pl, caja):
    """Lotes de un renglón de la caja: los picks de ESA caja; sin ellos (pedidos
    anteriores al 2026-10-22), los picks de la línea; sin picks, el lote asignado."""
    linea = pl.linea_pedido
    picks = [p for p in linea.picks.all() if p.caja_id == caja.pk and p.lote_id]
    if not picks:
        picks = [p for p in linea.picks.all() if p.caja_id is None and p.lote_id]
    lotes = []
    for p in picks:
        if p.lote not in lotes:
            lotes.append(p.lote)
    if not lotes and linea.lote_asignado_id:
        lotes = [linea.lote_asignado]
    return lotes


def _lote_texto(lote):
    return f"{lote.codigo} ({lote.fecha_caducidad.isoformat()})" if lote.fecha_caducidad else lote.codigo


def _contenido(guia, caja):
    """(piezas, "SKU×n; …", "lote (cad); …", filas de productos) de la caja (o del pedido entero sin caja)."""
    productos, piezas, skus, lotes = [], 0, [], []
    if caja is not None:
        for pl in caja.lineas.all():
            sku = pl.sku_real or pl.linea_pedido.sku
            piezas += pl.cantidad
            skus.append(f"{sku.codigo}×{pl.cantidad}")
            lotes_pl = _lotes_de(pl, caja)
            lotes += [_lote_texto(lote) for lote in lotes_pl if _lote_texto(lote) not in lotes]
            productos.append([
                guia.pedido.folio, guia.numero, str(caja.numero), sku.codigo, sku.descripcion, pl.cantidad,
                "; ".join(lote.codigo for lote in lotes_pl) or None,
                lotes_pl[0].fecha_caducidad if lotes_pl and lotes_pl[0].fecha_caducidad else None,
            ])
    else:
        for linea in guia.pedido.lineas.all():
            piezas += linea.cantidad
            skus.append(f"{linea.sku.codigo}×{linea.cantidad}")
            lote = linea.lote_asignado
            if lote is not None and _lote_texto(lote) not in lotes:
                lotes.append(_lote_texto(lote))
            productos.append([guia.pedido.folio, guia.numero, None, linea.sku.codigo, linea.sku.descripcion, linea.cantidad,
                              lote.codigo if lote else None, lote.fecha_caducidad if lote and lote.fecha_caducidad else None])
    return piezas, "; ".join(skus), "; ".join(lotes), productos


def _entregado(guia):
    fechas = [e.ts_carrier for e in guia.eventos.all() if e.estado == Guia.ENTREGADO and e.ts_carrier]
    if fechas:
        return min(fechas)
    return guia.pedido.ts_entregado if guia.estado == Guia.ENTREGADO else None


def _manifiesto(guia, caja):
    lineas = [lm for lm in (caja.lineas_manifiesto.all() if caja is not None else guia.lineas_manifiesto.all()) if not lm.no_salio]
    if lineas:
        return min(lm.manifiesto.ts for lm in lineas)
    return guia.pedido.ts_recolectado if caja is None else None


def _rango_peso(kg):
    for desde, hasta in RANGOS_PESO:
        if hasta is None or kg < hasta:
            return f"{desde}-{hasta} kg" if hasta is not None else f"{desde}+ kg"
    return None


def _normalizar_dims(texto):
    return "".join(ch for ch in str(texto or "").lower().replace("×", "x") if not ch.isspace())


# ── generar ──────────────────────────────────────────────────────────────────

def _pedidos(cliente, inicio, fin):
    return list(
        Pedido.objects.filter(cliente=cliente, creado__gte=inicio, creado__lt=fin)
        .prefetch_related(
            "lineas__sku", "lineas__lote_asignado", "lineas__picks__lote",
            "paquetes__lineas__linea_pedido__sku", "paquetes__lineas__linea_pedido__picks__lote", "paquetes__lineas__sku_real",
            "paquetes__lineas_manifiesto__manifiesto", "paquetes__caja",
            "guias__eventos", "guias__reembolsos", "guias__lineas_manifiesto__manifiesto",
        )
        .order_by("creado", "pk")
    )


def _fila(guia, caja, pedido, ctx, es_mesa):
    """Dict con todo lo de una guía; `columnas` es la fila ordenada."""
    from apps.finanzas.services import tarifa_envio_de, zona_de_carrier  # lazy por contrato
    from apps.finanzas.zonas import ciudad_de_cp, zona_de_cp  # lazy por contrato

    cp = destinos.cp_de(pedido)
    lugar = destinos.destino(cp, pedido, ctx["localidades"])
    zona = (zona_de_cp(cp) if cp else None) or zona_de_carrier(guia.carrier)
    ciudad = (ciudad_de_cp(cp) if cp else "") or lugar["ciudad"] or None
    tipo = _tipo(guia, caja)
    envia = _datos_envia(guia.raw)
    peso_real = _kg(caja.peso_real_gr) if caja is not None else _kg(pedido.peso_real_gr)
    dims = (caja.largo_cm, caja.ancho_cm, caja.alto_cm) if caja is not None else None
    volumetrico = round(dims[0] * dims[1] * dims[2] / DIVISOR_VOLUMETRICO, 2) if dims and all(dims) else None
    declarado = envia.get("declarado_kg") if envia else (peso_real if peso_real else (float(caja.peso_kg) if caja is not None else None))
    cobrado_por = None
    if declarado is not None and volumetrico is not None:
        cobrado_por = "volumétrico" if volumetrico > declarado else "real"
    if envia.get("recargo"):
        cobrado_por = "carrier (repesaje)"
    piezas, skus, lotes, productos = _contenido(guia, caja)
    listo = (caja.ts_listo_salida or caja.ts_cierre) if caja is not None else pedido.ts_guia
    manifiesto = _manifiesto(guia, caja)
    recolectado = guia.ts_recolectado_carrier
    entregado = _entregado(guia)
    cancelada = guia.estado == Guia.CANCELADA
    tarifa = Decimal("0") if cancelada else tarifa_envio_de(guia, ctx["tarifas"])
    reembolso = sum((r.monto for r in guia.reembolsos.all()), Decimal("0"))
    if cancelada and not reembolso:
        reembolso = tarifa_envio_de(guia, ctx["tarifas"])  # histórico: cancelada = reembolsada (como Finanzas)
    facturado = tarifa - reembolso if (tarifa or reembolso) else (None if cancelada else tarifa)
    con_iva = (facturado * (1 + _iva())).quantize(Decimal("0.01")) if facturado is not None else None
    if guia.carrier == "local":
        inicial = dinero(pedido.costo_entrega_propia) if pedido.costo_entrega_propia else None
    else:
        inicial = dinero(guia.costo_preferencial) if guia.costo_preferencial else None
    final = dinero(envia["total"]) if envia.get("total") is not None else inicial
    final_sin_iva = _sin_iva(final) if final is not None else None
    diferencia_sin = (facturado - final_sin_iva) if facturado is not None and final_sin_iva is not None else None
    diferencia_con = (con_iva - final) if con_iva is not None and final is not None else None
    fila = [
        pedido.folio, pedido.orden_shopify_legible or None, pedido.creado, tipo,
        guia.numero or None, guia.carrier, guia.proveedor or None, guia.servicio or None,
        str(caja.numero) if caja is not None else None, "×".join(str(d) for d in dims) if dims else None,
        Decimal(str(peso_real)) if peso_real is not None else None, Decimal(str(volumetrico)) if volumetrico is not None else None,
        Decimal(str(declarado)) if declarado is not None else None,
        Decimal(str(envia["carrier_kg"])) if envia.get("carrier_kg") is not None else None, cobrado_por,
        piezas or None, skus or None, lotes or None,
        ctx["cp_origen"], cp or None, ciudad, lugar["estado"] or None, NOMBRE_ZONA.get(zona, zona),
        guia.get_estado_display(),
        horas(pedido.creado, listo), horas(listo, manifiesto), horas(listo, recolectado),
        horas(recolectado or manifiesto, entregado),
        facturado, con_iva, reembolso or None, None, None,  # picking y empaque se llenan una vez por pedido
    ]
    if es_mesa:
        fila += [
            dinero(guia.costo_cotizado) if guia.costo_cotizado else None, inicial, final, final_sin_iva,
            dinero(envia["recargo"]) if envia.get("recargo") else None, diferencia_sin, diferencia_con,
        ]
    return {
        "fila": fila, "guia": guia, "caja": caja, "pedido": pedido, "tipo": tipo, "zona": zona, "cp": cp,
        "ciudad": ciudad, "estado": lugar["estado"], "dims": dims, "peso_real": peso_real, "piezas": piezas,
        "skus": skus.lower(), "facturado": facturado, "productos": productos, "entregado": entregado is not None,
        "cancelada": cancelada,
    }


def _pasa(r, filtros, es_mesa):
    if es_mesa and filtros.get("carrier") and r["guia"].carrier not in filtros["carrier"]:
        return False
    if filtros.get("tipo") and r["tipo"] not in filtros["tipo"]:
        return False
    if filtros.get("estado_guia") and r["guia"].estado not in filtros["estado_guia"]:
        return False
    if filtros.get("zona") and r["zona"] not in filtros["zona"]:
        return False
    if filtros.get("sku") and filtros["sku"].lower() not in r["skus"]:
        return False
    if filtros.get("origen") and str(filtros["origen"]) != str(r["origen_pk"] or ""):
        return False
    if filtros.get("destino"):
        aguja = destinos.sin_acentos(filtros["destino"]).lower()
        pajar = destinos.sin_acentos(" ".join(x for x in (r["cp"], r["ciudad"], r["estado"]) if x)).lower()
        if aguja not in pajar:
            return False
    if filtros.get("dimensiones"):
        if not r["dims"] or _normalizar_dims(filtros["dimensiones"]) != _normalizar_dims("x".join(str(d) for d in r["dims"])):
            return False
    minimo, maximo = filtros.get("peso") or (None, None)
    if minimo is not None and (r["peso_real"] is None or Decimal(str(r["peso_real"])) < minimo):
        return False
    if maximo is not None and (r["peso_real"] is None or Decimal(str(r["peso_real"])) > maximo):
        return False
    return True


def _corte(titulo, cubetas, total, orden=None, serie="guías"):
    """Grupo [Grupo, Guías, %, Piezas, Facturado] con barras; `orden` = llaves en orden fijo, si no por guías desc."""
    llaves = orden if orden is not None else sorted(cubetas, key=lambda k: (-cubetas[k]["guias"], str(k)))
    filas = [
        [k, c["guias"], round(100.0 * c["guias"] / total, 1) if total else None, c["piezas"] or None, dinero(c["facturado"]) if c["facturado"] else None]
        for k in llaves if (c := cubetas.get(k))
    ]
    return {"titulo": titulo, "columnas": COLUMNAS_CORTE, "filas": filas,
            "grafica": {"tipo": "barras", "etiqueta": 0, "valor": 1, "serie": serie}}


def _volumen(titulo, filas_por_periodo):
    filas = [[k, len(v["pedidos"]), v["guias"], len(v["paquetes"]), v["piezas"]] for k, v in filas_por_periodo.items()]
    return {"titulo": titulo, "columnas": COLUMNAS_VOLUMEN, "filas": filas,
            "grafica": {"tipo": "columnas", "etiqueta": 0, "series": [(1, "Pedidos"), (2, "Guías"), (3, "Paquetes"), (4, "Piezas")]}}


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.configuracion.services import origen_principal  # lazy por contrato
    from apps.envios.adapters import ORIGEN_DEFAULT  # lazy: el remitente de los adapters
    from apps.finanzas.services import tarifario_de  # lazy por contrato

    pedidos = _pedidos(cliente, inicio, fin)
    cps = {destinos.cp_de(p) for p in pedidos} - {""}
    origen = origen_principal()
    ctx = {
        "tarifas": tarifario_de(cliente),
        "localidades": {loc.cp: loc for loc in LocalidadCP.objects.filter(cp__in=cps)},
        "cp_origen": origen.cp if origen is not None else ORIGEN_DEFAULT.get("postalCode"),
    }
    tarifa_picking = Decimal(str(ctx["tarifas"]["alistamiento_pedido"]))
    tarifa_empaque = Decimal(str(ctx["tarifas"]["empaque_pedido"]))
    registros = []
    for pedido in pedidos:
        cajas = {c.pk: c for c in pedido.paquetes.all()}
        guias = sorted(pedido.guias.all(), key=lambda g: (g.creado, g.pk))
        vivas = [g for g in guias if g.estado not in (Guia.CANCELADA, Guia.SUSTITUIDA)]
        primera = (vivas or guias)[:1]
        for guia in guias:
            caja = cajas.get(guia.paquete_id)
            r = _fila(guia, caja, pedido, ctx, es_mesa)
            r["origen_pk"] = origen.pk if origen is not None else None
            if primera and guia is primera[0]:
                pickeado = bool(pedido.ts_picking or pedido.ts_empacado) or bool(vivas)
                empacado = bool(pedido.ts_empacado) or any(c.ts_cierre for c in cajas.values()) or bool(vivas)
                r["fila"][31] = tarifa_picking if pickeado else None
                r["fila"][32] = tarifa_empaque if empacado else None
            if _pasa(r, filtros, es_mesa):
                registros.append(r)
    total = len(registros)
    filas = [r["fila"] for r in registros]
    # Cortes.
    productos = [p for r in registros for p in r["productos"]]
    def cubeta():
        return {"guias": 0, "piezas": 0, "facturado": Decimal("0")}
    por_zona, por_ciudad, por_cp, por_dims, por_peso, por_rango, por_carrier = (defaultdict(cubeta) for _ in range(7))
    por_dia, por_mes = OrderedDict(), OrderedDict()
    guias_por_pedido = Counter()
    for r in registros:
        for cubetas, llave in (
            (por_zona, NOMBRE_ZONA.get(r["zona"], r["zona"])),
            (por_ciudad, f"{r['ciudad'] or destinos.SIN_CIUDAD}" + (f" · {r['estado']}" if r["estado"] else "")),
            (por_cp, r["cp"] or destinos.SIN_CP),
            (por_dims, "×".join(str(d) for d in r["dims"]) if r["dims"] else "(sin caja)"),
            (por_peso, _rango_peso(r["peso_real"]) if r["peso_real"] is not None else "(sin peso)"),
            (por_rango, f"{NOMBRE_ZONA.get(r['zona'], r['zona'])} · ${ctx['tarifas']['envio_bloque'].get(r['zona'], 0)}"),
            (por_carrier, r["guia"].carrier),
        ):
            c = cubetas[llave]
            c["guias"] += 1
            c["piezas"] += r["piezas"]
            c["facturado"] += r["facturado"] or Decimal("0")
        guias_por_pedido[r["pedido"].pk] += 1
        dia = timezone.localtime(r["pedido"].creado).date()
        for cubetas, llave in ((por_dia, dia.isoformat()), (por_mes, dia.strftime("%Y-%m"))):
            v = cubetas.setdefault(llave, {"pedidos": set(), "guias": 0, "paquetes": set(), "piezas": 0})
            v["pedidos"].add(r["pedido"].pk)
            v["guias"] += 1
            if r["caja"] is not None:
                v["paquetes"].add(r["caja"].pk)
            v["piezas"] += r["piezas"]
    # Todos los días del periodo, con ceros, para que la gráfica no salte fechas.
    dia = timezone.localtime(inicio).date()
    ultimo = timezone.localtime(fin - timedelta(seconds=1)).date()
    dias = OrderedDict()
    while dia <= ultimo:
        dias[dia.isoformat()] = por_dia.get(dia.isoformat(), {"pedidos": set(), "guias": 0, "paquetes": set(), "piezas": 0})
        dia += timedelta(days=1)
    distribucion = Counter(min(n, 4) for n in guias_por_pedido.values())
    por_numero = {("4+" if n == 4 else str(n)): {"guias": distribucion[n], "piezas": 0, "facturado": Decimal("0")} for n in sorted(distribucion)}
    tipos = Counter(r["tipo"] for r in registros)
    facturado_total = sum((r["facturado"] or Decimal("0")) for r in registros)
    resumen = [
        ("pedidos", len(guias_por_pedido)), ("guías", total), ("paquetes", sum(1 for r in registros if r["caja"] is not None)),
        ("piezas", sum(r["piezas"] for r in registros)),
    ] + [(t, tipos[t]) for t, _n in TIPOS if tipos[t]] + [
        ("entregadas", sum(1 for r in registros if r["entregado"])),
        ("envío facturado sin IVA MXN", dinero(facturado_total)),
        ("envío facturado con IVA MXN", dinero(facturado_total * (1 + _iva()))),
    ]
    if es_mesa:
        costo_final = sum((r["fila"][35] or Decimal("0")) for r in registros)
        resumen += [("costo final carriers MXN", dinero(costo_final)),
                    ("diferencia con IVA MXN", dinero(facturado_total * (1 + _iva()) - costo_final))]
    grupos = [
        {"titulo": "Productos por guía", "columnas": COLUMNAS_PRODUCTOS, "filas": productos},
        _volumen("Volumen diario", dias),
        _volumen("Volumen mensual", por_mes),
        {**_corte("Guías por pedido", por_numero, len(guias_por_pedido), orden=list(por_numero), serie="pedidos"),
         "columnas": [("Guías por pedido", "texto"), ("Pedidos", "entero"), ("% de pedidos", "pct"), ("Piezas", "entero"), ("Envío facturado sin IVA", "dinero")]},
        _corte("Guías por zona", por_zona, total, orden=[n for _z, n in ZONAS if n in por_zona]),
        _corte("Guías por ciudad", por_ciudad, total),
        _corte("Guías por código postal", por_cp, total),
        _corte("Guías por dimensiones de caja", por_dims, total),
        _corte("Guías por rango de peso real", por_peso, total, orden=[k for k in [_rango_peso(d) for d, _h in RANGOS_PESO] + ["(sin peso)"] if k in por_peso]),
        _corte("Guías por rango cobrado (zona · tarifa)", por_rango, total),
        _corte("Guías por paquetería", por_carrier, total),
    ]
    return {"filas": filas, "resumen": resumen, "grupos": grupos}
