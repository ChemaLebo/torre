"""Línea de tiempo de pedidos (Chema 2026-09-25): por pedido y por caja, cada
hora del camino (recibido, picking terminado, empacado, guía, salida de
bodega, recolectado por el carrier, en tránsito, entregado), el compromiso de
entrega, el manifiesto con el que salió cada caja y todos los eventos que
reportó la paquetería. La misma construcción sirve a Mesa (todos los
clientes) y al portal (su cliente).
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.envios.models import Guia, LineaManifiesto, Paquete
from apps.pedidos.models import Pedido

PASOS = [
    ("creado", "Recibido"), ("ts_picking", "Picking"), ("ts_empacado", "Empacado"), ("ts_guia", "Guía"),
    ("ts_recolectado", "Salió de bodega"), ("ts_recolectado_carrier", "Recolectado por carrier"),
    ("ts_en_transito", "En tránsito"), ("ts_entregado", "Entregado"),
]
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
    return qs


def _fila_guia(guia, manifiestos):
    eventos = [
        {"ts": e.ts_carrier or e.ts_visto, "estado": e.estado, "descripcion": e.descripcion or e.crudo}
        for e in guia.eventos.all()
    ]
    linea = manifiestos.get(guia.pk)
    return {
        "carrier": guia.carrier, "numero": guia.numero, "estado": guia.get_estado_display(), "activa": guia.es_activa,
        "manifiesto": linea.manifiesto if linea else None,
        "recolectado_carrier": guia.ts_recolectado_carrier,
        "compromiso": guia.fecha_compromiso, "dias_promesa": guia.dias_promesa,
        "entregado": guia.estado == Guia.ENTREGADO, "eventos": eventos,
    }


def construir(pedidos):
    """[{pedido, pasos: [(etiqueta, ts)], compromiso, cajas: [{numero, estado, guias: [...]}]}]
    para los pedidos dados (ya filtrados). Un pedido sin plan de cajas se
    muestra como una sola caja implícita con sus guías."""
    pedidos = list(
        pedidos.select_related("cliente", "tienda")
        .prefetch_related("guias__eventos", "paquetes__guias__eventos")
    )
    lineas = LineaManifiesto.objects.filter(pedido__in=pedidos).select_related("manifiesto")
    por_guia = {l.guia_id: l for l in lineas if l.guia_id}
    filas = []
    for p in pedidos:
        cajas = []
        for caja in sorted(p.paquetes.all(), key=lambda c: c.numero):
            guias = [_fila_guia(g, por_guia) for g in sorted(caja.guias.all(), key=lambda g: g.pk)]
            cajas.append({"numero": caja.numero, "estado": caja.get_estado_display(),
                          "fuera": caja.estado == Paquete.DESPACHADO, "guias": guias})
        sueltas = [g for g in p.guias.all() if g.paquete_id is None]
        if sueltas or not cajas:
            cajas.append({"numero": None, "estado": p.get_estado_display(), "fuera": p.ts_recolectado is not None,
                          "guias": [_fila_guia(g, por_guia) for g in sorted(sueltas, key=lambda g: g.pk)]})
        compromisos = [g["compromiso"] for c in cajas for g in c["guias"] if g["compromiso"] and g["activa"] and not g["entregado"]]
        filas.append({
            "pedido": p,
            "pasos": [(etiqueta, getattr(p, campo)) for campo, etiqueta in PASOS],
            "compromiso": max(compromisos) if compromisos else None,
            "vencido": bool(compromisos) and max(compromisos) < timezone.localdate() and p.ts_entregado is None,
            "cajas": cajas,
        })
    return filas
