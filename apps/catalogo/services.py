"""Servicios de catálogo: lotes.

Un lote es (SKU, código) con caducidad opcional; el FEFO del picking ordena
por caducidad, no por el nombre del lote. Los lotes nacen aquí (o al ubicar en
piso vía obtener_o_crear_lote) y Mesa los administra en la página de lotes del
cliente. `lotes_sugeridos` alimenta el campo con sugerencias del piso y de las
ASN: primero lo anunciado en esa ASN, luego lo que tiene stock, luego lo creado
en los últimos TORRE["LOTES_SUGERENCIA_DIAS"] días.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.core.services import registrar_evento

from .models import Lote


def obtener_o_crear_lote(sku, codigo, fecha_caducidad=None):
    """Lote del SKU por código; lo crea si no existe y completa la caducidad
    si el lote existía sin ella. Regresa el Lote."""
    codigo = (codigo or "").strip()
    if not codigo:
        raise ValueError("El lote necesita un código.")
    lote, creado = Lote.objects.get_or_create(
        sku=sku, codigo=codigo, defaults={"fecha_caducidad": fecha_caducidad},
    )
    if not creado and lote.fecha_caducidad is None and fecha_caducidad is not None:
        lote.fecha_caducidad = fecha_caducidad
        lote.save(update_fields=["fecha_caducidad"])
    return lote


def crear_lote(sku, codigo, fecha_caducidad, actor):
    """Alta de lote desde Mesa (auditada). Rechaza duplicados del mismo SKU."""
    codigo = (codigo or "").strip()
    if not codigo:
        raise ValueError("Captura el código del lote.")
    if Lote.objects.filter(sku=sku, codigo=codigo).exists():
        raise ValueError(f"El lote {codigo} ya existe para {sku.codigo}.")
    lote = Lote.objects.create(sku=sku, codigo=codigo, fecha_caducidad=fecha_caducidad)
    registrar_evento(
        "lote", f"{sku.codigo}/{codigo}", "alta", actor=actor, cliente=sku.cliente,
        delta={"sku": sku.codigo, "caducidad": fecha_caducidad.isoformat() if fecha_caducidad else None},
        motivo="Lote dado de alta desde Mesa de Control",
    )
    return lote


def actualizar_caducidad(lote, fecha_caducidad, actor):
    """Corrige la caducidad de un lote (auditado con antes/después)."""
    antes = lote.fecha_caducidad
    if antes == fecha_caducidad:
        return lote
    lote.fecha_caducidad = fecha_caducidad
    lote.save(update_fields=["fecha_caducidad"])
    registrar_evento(
        "lote", f"{lote.sku.codigo}/{lote.codigo}", "caducidad_actualizada",
        actor=actor, cliente=lote.sku.cliente,
        delta={"antes": antes.isoformat() if antes else None,
               "ahora": fecha_caducidad.isoformat() if fecha_caducidad else None},
        motivo="Caducidad corregida desde Mesa de Control",
    )
    return lote


def _con_stock(sku_ids):
    """{(sku_id, lote_id): piezas} en putaway/vendible/reservado para esos SKUs."""
    from apps.inventario.models import Saldo

    filas = (
        Saldo.objects.filter(
            sku_id__in=sku_ids, lote__isnull=False, cantidad__gt=0,
            estado__in=[Saldo.EN_PUTAWAY, Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO],
        )
        .values("sku_id", "lote_id").annotate(t=Sum("cantidad"))
    )
    return {(f["sku_id"], f["lote_id"]): f["t"] for f in filas}


def lotes_sugeridos(sku, orden=None):
    """Sugerencias para el campo de lote de un SKU, en orden de relevancia y sin
    repetir código: [{"codigo", "caducidad" (ISO o ""), "origen"}].

    origen: "asn" (anunciado para este SKU en esa orden), "orden" (anunciado o
    creado para otro SKU de la misma orden: las ASN de un cliente suelen
    compartir lote), "stock" (con piezas), "reciente" (creado en la ventana).
    """
    ventana = timezone.now() - timedelta(days=settings.TORRE["LOTES_SUGERENCIA_DIAS"])
    propios = {l.codigo: l for l in Lote.objects.filter(sku=sku)}
    sugerencias = []
    vistos = set()

    def agrega(codigo, caducidad, origen):
        codigo = (codigo or "").strip()
        if not codigo or codigo in vistos:
            return
        vistos.add(codigo)
        lote = propios.get(codigo)
        if lote is not None and lote.fecha_caducidad:
            caducidad = lote.fecha_caducidad
        sugerencias.append({
            "codigo": codigo,
            "caducidad": caducidad.isoformat() if caducidad else "",
            "origen": origen,
        })

    if orden is not None:
        lineas = list(orden.lineas.all())
        for linea in lineas:
            if linea.sku_id == sku.pk:
                agrega(linea.lote_codigo, linea.fecha_caducidad, "asn")
        for linea in lineas:
            if linea.sku_id != sku.pk:
                agrega(linea.lote_codigo, linea.fecha_caducidad, "orden")
        otros = Lote.objects.filter(
            sku_id__in=[l.sku_id for l in lineas], creado__gte=orden.creado,
        ).exclude(sku=sku).order_by("-creado")
        for lote in otros:
            agrega(lote.codigo, lote.fecha_caducidad, "orden")

    stock = _con_stock([sku.pk])
    con_stock = sorted(
        (l for l in propios.values() if (sku.pk, l.pk) in stock),
        key=lambda l: (l.fecha_caducidad or timezone.localdate().replace(year=9999), l.codigo),
    )
    for lote in con_stock:
        agrega(lote.codigo, lote.fecha_caducidad, "stock")
    recientes = sorted(
        (l for l in propios.values() if l.creado >= ventana), key=lambda l: -l.creado.timestamp(),
    )
    for lote in recientes:
        agrega(lote.codigo, lote.fecha_caducidad, "reciente")
    return sugerencias


def lotes_recientes_cliente(cliente):
    """Lotes del cliente con stock o creados en la ventana, para el datalist del
    anuncio de ASN (donde el SKU cambia por renglón): [{"codigo", "caducidad"}]
    sin repetir código, del más reciente al más viejo; la caducidad (ISO o "")
    es la del lote más reciente con ese código y se autollena al elegirlo."""
    ventana = timezone.now() - timedelta(days=settings.TORRE["LOTES_SUGERENCIA_DIAS"])
    lotes = list(Lote.objects.filter(sku__cliente=cliente).select_related("sku"))
    stock = _con_stock({l.sku_id for l in lotes})
    sugerencias = []
    vistos = set()
    for lote in sorted(lotes, key=lambda l: -l.creado.timestamp()):
        if lote.codigo in vistos or not (lote.creado >= ventana or (lote.sku_id, lote.pk) in stock):
            continue
        vistos.add(lote.codigo)
        sugerencias.append({
            "codigo": lote.codigo,
            "caducidad": lote.fecha_caducidad.isoformat() if lote.fecha_caducidad else "",
        })
    return sugerencias


def lotes_cliente(cliente):
    """Lotes del cliente para la página de Mesa: cada uno con piezas en
    recepción/vendibles/apartadas, ordenados por SKU y caducidad."""
    lotes = list(
        Lote.objects.filter(sku__cliente=cliente)
        .select_related("sku").order_by("sku__codigo", "fecha_caducidad", "codigo")
    )
    stock = _con_stock({l.sku_id for l in lotes})
    for lote in lotes:
        lote.piezas = stock.get((lote.sku_id, lote.pk), 0)
    return lotes
