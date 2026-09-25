"""Línea de tiempo de pedidos (Chema 2026-09-25): UNA FILA POR PAQUETE (caja),
sin acordeones. Cada fila trae las horas del pedido (recibido, picking
terminado, empacado) y las de esa caja: guía comprada, salida de bodega (el
manifiesto), recolección reportada por el carrier, en tránsito, en ruta,
entregado, el compromiso de entrega y el último evento de la paquetería. Un
pedido de tres cajas son tres filas. La misma construcción sirve a Mesa
(todos los clientes) y al portal (su cliente).
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.envios.models import Guia, LineaManifiesto, Paquete

DIAS_DEFAULT = 14


def filtrar(qs, filtros):
    """Aplica los filtros de la pantalla (fechas de creación, estado, texto)
    al queryset de pedidos; sin fechas, los últimos DIAS_DEFAULT días."""
    desde, hasta = filtros.get("desde"), filtros.get("hasta")
    if not desde and not hasta:
        desde = (timezone.localdate() - timedelta(days=DIAS_DEFAULT)).isoformat()
    if desde:
        qs = qs.filter(creado__date__gte=desde)
    if hasta:
        qs = qs.filter(creado__date__lte=hasta)
    if filtros.get("estado"):
        qs = qs.filter(estado=filtros["estado"])
    q = (filtros.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(folio__icontains=q) | Q(shopify_order_name__icontains=q) | Q(comprador_nombre__icontains=q))
    if filtros.get("guia_estado"):
        # Estatus del PAQUETE (su guía), no del pedido: se acota aquí y se
        # afina por fila en construir (un pedido puede tener cajas en estados distintos).
        qs = qs.filter(guias__estado=filtros["guia_estado"]).distinct()
    return qs


def _primer_evento(guia, estados):
    for e in guia.eventos.all():  # ordenados por hora del carrier
        if e.estado in estados and e.ts_carrier is not None:
            return e.ts_carrier
    return None


def _fila(pedido, caja, guia, total_cajas, manifiestos, hoy):
    """Una fila de la tabla: la caja (o el envío entero, legacy) con su guía."""
    linea = manifiestos.get(guia.pk) if guia else None
    eventos = list(guia.eventos.all()) if guia else []
    ultimo = eventos[-1] if eventos else None
    entregada = bool(guia and guia.estado == Guia.ENTREGADO)
    salida = linea.manifiesto.ts if linea else None
    if salida is None and pedido.ts_recolectado and (caja is None or caja.estado == Paquete.DESPACHADO):
        salida = pedido.ts_recolectado  # salida sin hoja (antes del manifiesto con folio)
    en_transito = _primer_evento(guia, {Guia.EN_TRANSITO}) if guia else None
    if en_transito is None and guia and guia.estado in (Guia.EN_TRANSITO, Guia.EN_RUTA, Guia.ENTREGADO):
        en_transito = pedido.ts_en_transito
    entregado = _primer_evento(guia, {Guia.ENTREGADO}) if guia else None
    if entregado is None and entregada:
        entregado = pedido.ts_entregado
    compromiso = guia.fecha_compromiso if guia and guia.es_activa and not entregada else None
    return {
        "pedido": pedido,
        "caja": caja.numero if caja else None, "total_cajas": total_cajas,
        "caja_estado": caja.get_estado_display() if caja else "",
        "guia": guia,
        "manifiesto": linea.manifiesto if linea else None,
        "ts": {
            "recibido": pedido.creado, "picking": pedido.ts_picking, "empacado": pedido.ts_empacado,
            "guia": guia.creado if guia else None,
            "salida": salida,
            "recolectado_carrier": guia.ts_recolectado_carrier if guia else None,
            "en_transito": en_transito,
            "en_ruta": _primer_evento(guia, {Guia.EN_RUTA}) if guia else None,
            "entregado": entregado,
        },
        "compromiso": compromiso,
        "vencido": bool(compromiso) and compromiso < hoy and not entregada,
        "dias_promesa": guia.dias_promesa if guia else None,
        "ultimo_evento": ultimo.descripcion or ultimo.crudo if ultimo else (guia.ultimo_evento if guia else ""),
        "ultimo_evento_ts": (ultimo.ts_carrier or ultimo.ts_visto) if ultimo else None,
        "eventos": len(eventos),
    }


def construir(pedidos, guia_estado=""):
    """Filas por paquete para los pedidos dados (ya filtrados), en el orden de
    los pedidos y de sus cajas. Un pedido sin plan de cajas es una fila por
    guía (o una sola fila sin guía). `guia_estado` deja solo las filas cuya
    guía está en ese estatus (filtro "estatus del paquete")."""
    pedidos = list(
        pedidos.select_related("cliente", "tienda")
        .prefetch_related("guias__eventos", "paquetes__guias__eventos")
    )
    lineas = LineaManifiesto.objects.filter(pedido__in=pedidos).select_related("manifiesto")
    por_guia = {l.guia_id: l for l in lineas if l.guia_id}
    hoy = timezone.localdate()
    filas = []
    for p in pedidos:
        cajas = sorted(p.paquetes.all(), key=lambda c: c.numero)
        sueltas = sorted((g for g in p.guias.all() if g.paquete_id is None), key=lambda g: g.pk)
        total = len(cajas) or max(len(sueltas), 1)
        for caja in cajas:
            guias = sorted(caja.guias.all(), key=lambda g: g.pk)
            guia = next((g for g in guias if g.es_activa), guias[-1] if guias else None)
            filas.append(_fila(p, caja, guia, total, por_guia, hoy))
        if not cajas:
            for guia in sueltas or [None]:
                filas.append(_fila(p, None, guia, total, por_guia, hoy))
        elif sueltas:  # guías viejas sin caja de un pedido que sí tiene plan
            for guia in sueltas:
                if guia.es_activa:
                    filas.append(_fila(p, None, guia, total, por_guia, hoy))
    if guia_estado:
        filas = [f for f in filas if f["guia"] is not None and f["guia"].estado == guia_estado]
    return filas
