"""Reporte del día: pedidos con actividad en una fecha y su evidencia por etapa.

Lo comparten Mesa (toda la bodega, con quién hizo cada paso) y el portal (solo
su cliente, sin operadores). "Actividad" = creado ese día, cualquier paso del
flujo cumplido ese día o cualquier otra actualización (cancelación, retorno).
Las fotos se catalogan por etapa según su tipo: contenido y caja cerrada van
en Empaque, el POD en Entrega; las de incidencias no se repiten aquí, se
enlaza la incidencia. El quién sale de la auditoría (`cambio_estado`).
"""
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from apps.core.models import EvidenciaFoto, EventoAuditoria

from .models import Pedido

# (clave, etiqueta, campo timestamp del pedido, estado que la cumple en la auditoría)
ETAPAS = (
    ("recibido", "Pedido recibido", "creado", None),
    ("picking", "Picking", "ts_picking", Pedido.EN_PICKING),
    ("empaque", "Empaque", "ts_empacado", Pedido.EMPACADO),
    ("guia", "Guía", "ts_guia", Pedido.GUIA_GENERADA),
    ("salida", "Salida de bodega", "ts_recolectado", Pedido.RECOLECTADO),
    ("transito", "En camino", "ts_en_transito", Pedido.EN_TRANSITO),
    ("entrega", "Entrega", "ts_entregado", Pedido.ENTREGADO),
)
ETAPA_POR_TIPO_FOTO = {"contenido": "empaque", "caja_cerrada": "empaque", "pod": "entrega"}
ETIQUETA_FOTO = {"contenido": "Contenido", "caja_cerrada": "Caja cerrada", "pod": "Entrega (POD)"}
ESTADOS_CIERRE = (Pedido.CANCELADO, Pedido.RETORNADO, Pedido.ENTREGA_PRESUNTA, Pedido.CANCELACION_PENDIENTE)
PILL = {
    Pedido.EN_PICKING: "accent", Pedido.EMPACADO: "accent", Pedido.GUIA_GENERADA: "accent",
    Pedido.RECOLECTADO: "accent", Pedido.EN_TRANSITO: "accent", Pedido.ENTREGADO: "ok",
    Pedido.ENTREGA_PRESUNTA: "warn", Pedido.PARCIALMENTE_DESPACHADO: "warn",
    Pedido.CANCELACION_PENDIENTE: "warn", Pedido.RETORNADO: "crit",
}
COLUMNAS_CSV = (
    "folio", "orden_shopify", "cliente", "comprador", "estado", "creado", "carrier", "guia",
    "rastreo", "incidencias", "fotos_empaque", "fotos_entrega", "fotos_urls",
)


def fecha_desde_get(valor):
    """(fecha, válida): la del parámetro ?fecha=AAAA-MM-DD o hoy si falta o no se entiende."""
    hoy = timezone.localdate()
    if not valor:
        return hoy, True
    try:
        return datetime.strptime(valor.strip(), "%Y-%m-%d").date(), True
    except ValueError:
        return hoy, False


def contexto_fecha(fecha):
    """Fechas para la navegación del reporte (hoy, día anterior, siguiente)."""
    hoy = timezone.localdate()
    return {"fecha": fecha, "hoy": hoy, "ayer": fecha - timedelta(days=1),
            "manana": fecha + timedelta(days=1) if fecha < hoy else None}


def resumen_estados(renglones):
    """[(estado legible, pill, n)] en el orden del flujo, solo los presentes."""
    conteo = {}
    for r in renglones:
        conteo[r["pedido"].estado] = conteo.get(r["pedido"].estado, 0) + 1
    nombres = dict(Pedido.ESTADOS)
    return [(nombres[e], PILL.get(e, ""), conteo[e]) for e, _n in Pedido.ESTADOS if e in conteo]


def rango_dia(fecha):
    """(inicio, fin) aware del día local `fecha`; fin exclusivo."""
    inicio = timezone.make_aware(datetime.combine(fecha, time.min))
    return inicio, inicio + timedelta(days=1)


def pedidos_con_actividad(fecha, cliente=None):
    """Pedidos con actividad en `fecha` (creado, algún paso cumplido o cualquier
    actualización ese día), del cliente dado o de todos."""
    inicio, fin = rango_dia(fecha)
    filtro = Q(creado__gte=inicio, creado__lt=fin) | Q(actualizado__gte=inicio, actualizado__lt=fin)
    for _clave, _etiqueta, campo, _estado in ETAPAS[1:]:
        filtro |= Q(**{f"{campo}__gte": inicio, f"{campo}__lt": fin})
    qs = Pedido.objects.filter(filtro)
    if cliente is not None:
        qs = qs.filter(cliente=cliente)
    return (
        qs.select_related("cliente", "tienda", "asignado_a")
        .prefetch_related("guias", "incidencias", "paquetes")
        .order_by("-creado")
    )


def url_orden_shopify(pedido):
    """Link al admin de Shopify de la orden, o "" si el pedido no viene de una tienda."""
    if pedido.tienda_id is None or not pedido.shopify_order_id:
        return ""
    return f"https://{pedido.tienda.dominio}/admin/orders/{pedido.shopify_order_id}"


def _fotos_por_pedido(pedidos):
    """{pk: [EvidenciaFoto]} de las fotos del pedido (entidad_id mezcla pk y
    folio según el punto de captura) y del POD de entrega local."""
    ids = {str(p.pk): p.pk for p in pedidos}
    folios = {p.folio: p.pk for p in pedidos if p.folio}
    fotos = EvidenciaFoto.objects.filter(
        entidad__in=["pedido", "entrega_local"], entidad_id__in=list(ids) + list(folios),
    ).order_by("ts", "pk")
    por_pedido = {}
    for foto in fotos:
        pk = ids.get(foto.entidad_id) or folios.get(foto.entidad_id)
        por_pedido.setdefault(pk, []).append(foto)
    return por_pedido


def _cajas_por_evidencia(pedidos):
    """{evidencia_id: número de caja} desde los eventos de cierre de caja."""
    paquetes = [str(paq.pk) for p in pedidos for paq in p.paquetes.all()]
    if not paquetes:
        return {}
    eventos = EventoAuditoria.objects.filter(
        entidad="paquete", accion="caja_cerrada_con_evidencia", entidad_id__in=paquetes,
    )
    return {
        e.delta.get("evidencia_id"): e.delta.get("caja")
        for e in eventos if e.delta.get("evidencia_id")
    }


def _transiciones(pedidos):
    """{(pk, estado_destino): (ts, actor)} de la primera transición a cada estado."""
    eventos = EventoAuditoria.objects.filter(
        entidad="pedido", accion="cambio_estado", entidad_id__in=[str(p.pk) for p in pedidos],
    ).order_by("ts", "pk")
    resultado = {}
    for e in eventos:
        clave = (int(e.entidad_id), e.delta.get("a"))
        resultado.setdefault(clave, (e.ts, e.actor_id))
    return resultado


def _fotos_incidencias(pedidos):
    """{folio o pk de incidencia: número de fotos}."""
    claves = []
    for p in pedidos:
        for inc in p.incidencias.all():
            claves += [inc.folio, str(inc.pk)]
    if not claves:
        return {}
    conteo = {}
    for foto in EvidenciaFoto.objects.filter(entidad="incidencia", entidad_id__in=claves):
        conteo[foto.entidad_id] = conteo.get(foto.entidad_id, 0) + 1
    return conteo


def armar_reporte(pedidos, con_operador=False):
    """Renglones del reporte: por pedido, sus datos de cabecera (estado, guía con
    link al carrier, orden de Shopify, incidencias) y sus etapas en orden con
    hora, fotos y, si `con_operador`, quién la cumplió."""
    from apps.envios.services import url_rastreo_carrier  # lazy por contrato

    pedidos = list(pedidos)
    fotos = _fotos_por_pedido(pedidos)
    cajas = _cajas_por_evidencia(pedidos)
    transiciones = _transiciones(pedidos)
    fotos_inc = _fotos_incidencias(pedidos)
    renglones = []
    for p in pedidos:
        guias = list(p.guias.all())
        guia = next((g for g in guias if g.es_activa), None) or (guias[0] if guias else None)
        propias = fotos.get(p.pk, [])
        etapas = []
        for clave, etiqueta, campo, estado in ETAPAS:
            ts = getattr(p, campo)
            actor = transiciones.get((p.pk, estado), (None, ""))[1] if estado else ""
            if clave == "picking" and not actor and p.asignado_a_id:
                actor = p.asignado_a.username
            fotos_etapa = [
                {"foto": f, "etiqueta": _etiqueta_foto(f, cajas)}
                for f in propias if ETAPA_POR_TIPO_FOTO.get(f.tipo) == clave
            ]
            etapas.append({
                "clave": clave, "etiqueta": etiqueta, "ts": ts, "hecha": ts is not None,
                "operador": actor if con_operador else "", "fotos": fotos_etapa,
            })
        sueltas = [f for f in propias if f.tipo not in ETAPA_POR_TIPO_FOTO]
        if sueltas:
            etapas.append({
                "clave": "otras", "etiqueta": "Otras fotos", "ts": sueltas[0].ts, "hecha": True,
                "operador": "", "fotos": [{"foto": f, "etiqueta": f.tipo or "Foto"} for f in sueltas],
            })
        if p.estado in ESTADOS_CIERRE:
            ts_cierre, actor_cierre = transiciones.get((p.pk, p.estado), (p.actualizado, ""))
            etapas.append({
                "clave": "cierre", "etiqueta": p.get_estado_display(), "ts": ts_cierre, "hecha": True,
                "operador": actor_cierre if con_operador else "", "fotos": [],
            })
        incidencias = [
            {"incidencia": inc, "abierta": inc.estado in inc.ESTADOS_ABIERTOS,
             "fotos": fotos_inc.get(inc.folio, 0) + fotos_inc.get(str(inc.pk), 0)}
            for inc in p.incidencias.all()
        ]
        conteo = {}
        for e in etapas:
            if e["fotos"]:
                conteo[e["clave"]] = len(e["fotos"])
        renglones.append({
            "pedido": p,
            "pill": PILL.get(p.estado, ""),
            "shopify_url": url_orden_shopify(p),
            "guia": guia,
            "rastreo_url": url_rastreo_carrier(guia.carrier, guia.numero) if guia else "",
            "etapas": etapas,
            "incidencias": incidencias,
            "fotos_total": len(propias),
            "resumen_fotos": " · ".join(
                f"{dict((c, et) for c, et, _f, _e in ETAPAS).get(k, 'otras').lower()} {n}" for k, n in conteo.items()
            ),
        })
    return renglones


def _etiqueta_foto(foto, cajas):
    base = ETIQUETA_FOTO.get(foto.tipo, foto.tipo or "Foto")
    caja = cajas.get(foto.pk)
    return f"{base} · caja {caja}" if caja else base


def filas_csv(renglones, ruta_evidencia):
    """Filas del CSV del reporte (mismas columnas para Mesa y portal);
    `ruta_evidencia(foto)` arma el link de cada foto."""
    filas = []
    for r in renglones:
        p, guia = r["pedido"], r["guia"]
        por_etapa = {e["clave"]: e["fotos"] for e in r["etapas"]}
        urls = [ruta_evidencia(f["foto"]) for e in r["etapas"] for f in e["fotos"]]
        filas.append([
            p.folio, p.shopify_order_id or "", p.cliente.nombre, p.comprador_nombre,
            p.get_estado_display(), timezone.localtime(p.creado).strftime("%Y-%m-%d %H:%M"),
            guia.carrier if guia else "", guia.numero if guia else "", r["rastreo_url"],
            "; ".join(i["incidencia"].folio for i in r["incidencias"]),
            len(por_etapa.get("empaque", [])), len(por_etapa.get("entrega", [])),
            " ".join(urls),
        ])
    return filas
