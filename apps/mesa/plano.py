"""Plano de la bodega (Local 380 E) — geometría de racks y datos vivos por zona.

El partial compartido `templates/includes/plano_bodega.html` dibuja el plano
completo (viewBox 0 0 1660 2110, unidad = cm, del arquitectónico de 8A casa de
diseño, 18/may/2026) con geometría fija; lo único dinámico del dibujo son los
racks: 7 filas de racks dobles (14 rects) a los que se reparten las ubicaciones
reales de `catalogo.Ubicacion` tipo picking/reserva. Racks sin ubicación
asignada se pintan tenues.

`zonas_bodega(cliente=None)` arma los datos vivos de cada zona: sin cliente ve
la bodega completa (Mesa); con cliente TODO queda filtrado al tenant (portal) —
lo ajeno jamás aparece, ni en conteos. Los datos regresan crudos (sin pills ni
copy): cada vista los decora con su propio vocabulario.

Lo usan mesa:bodega y portal:bodega (import lazy desde portal, por contrato).
"""
from datetime import timedelta

from django.db.models import Q, Sum
from django.utils import timezone

# Zona del almacén en el plano: x 660-1300, y 680-1980.
RACK_X = 690
RACK_ANCHO = 580
RACK_ALTO = 56
RACK_Y0 = 760
RACK_PASO_FILA = 174  # dos racks pegados (espalda con espalda) + pasillo
RACK_FILAS = 7


def racks_bodega():
    """Los 14 racks del plano con su ubicación real asignada (o vacía).

    Regresa dicts listos para el SVG: x, y, w, h, cx/cy (centro del texto)
    y codigo ("" si el rack no tiene ubicación asignada → se pinta tenue).
    """
    from apps.catalogo.models import Ubicacion  # lazy por contrato

    codigos = list(
        Ubicacion.objects.filter(
            tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA], activo=True,
        )
        .order_by("codigo")
        .values_list("codigo", flat=True)
    )
    racks = []
    for fila in range(RACK_FILAS):
        y_fila = RACK_Y0 + fila * RACK_PASO_FILA
        for nivel in (0, 1):
            indice = len(racks)
            y = y_fila + nivel * RACK_ALTO
            racks.append({
                "x": RACK_X,
                "y": y,
                "w": RACK_ANCHO,
                "h": RACK_ALTO,
                "cx": RACK_X + RACK_ANCHO // 2,
                "cy": y + RACK_ALTO // 2 + 9,  # baseline del texto centrado
                "codigo": codigos[indice] if indice < len(codigos) else "",
            })
    return racks


def zonas_bodega(cliente=None):
    """Datos vivos por zona del plano. Con `cliente` filtra TODO al tenant.

    Regresa (zonas, badges, zona_activa). Querysets aquí, nunca en el template;
    el badge de oficina queda en None porque solo la Mesa lo llena.
    """
    from apps.inventario.models import OrdenEntrada, Saldo  # lazy por contrato
    from apps.pedidos.models import Pedido  # lazy por contrato
    from apps.piso.views import (  # lazy: el mapeo carrier→corral vive en piso
        _carrier_probable, _corral_de_carrier, _guia_activa, _mapa_corrales,
        corrales_activos,
    )

    hoy = timezone.localdate()

    # ── Descarga: ASNs en piso (EN_RECEPCION/RECIBIDA) o con cita hoy/mañana ──
    qs_ordenes = OrdenEntrada.objects.filter(
        Q(estado__in=[OrdenEntrada.EN_RECEPCION, OrdenEntrada.RECIBIDA])
        | Q(
            estado=OrdenEntrada.ANUNCIADA,
            fecha_compromiso__range=(hoy, hoy + timedelta(days=1)),
        )
    ).select_related("cliente").prefetch_related("lineas")
    if cliente is not None:
        qs_ordenes = qs_ordenes.filter(cliente=cliente)
    ordenes = list(qs_ordenes)

    # ── Saldos por estado (desarmado/put-away, almacén, cuarentena) ──
    saldos = Saldo.objects.filter(cantidad__gt=0)
    if cliente is not None:
        saldos = saldos.filter(sku__cliente=cliente)

    putaway = list(
        saldos.filter(estado=Saldo.EN_PUTAWAY)
        .values("sku__cliente__nombre", "sku__codigo", "sku__descripcion")
        .annotate(piezas=Sum("cantidad"))
        .order_by("sku__cliente__nombre", "sku__codigo")
    )

    vendibles = saldos.filter(estado=Saldo.UBICADO_VENDIBLE)
    por_ubicacion = {}
    for fila in vendibles.values("ubicacion__codigo").annotate(piezas=Sum("cantidad")):
        por_ubicacion[fila["ubicacion__codigo"]] = {
            "codigo": fila["ubicacion__codigo"], "vendible": fila["piezas"], "apartado": 0,
        }
    for fila in (
        saldos.filter(estado=Saldo.RESERVADO)
        .values("ubicacion__codigo").annotate(piezas=Sum("cantidad"))
    ):
        registro = por_ubicacion.setdefault(
            fila["ubicacion__codigo"],
            {"codigo": fila["ubicacion__codigo"], "vendible": 0, "apartado": 0},
        )
        registro["apartado"] = fila["piezas"]
    almacen = {
        "ubicaciones": sorted(por_ubicacion.values(), key=lambda u: u["codigo"]),
        "total_vendible": sum(u["vendible"] for u in por_ubicacion.values()),
        "total_apartado": sum(u["apartado"] for u in por_ubicacion.values()),
        "n_skus": vendibles.values("sku").distinct().count(),
        "por_cliente": list(
            vendibles.values("sku__cliente__nombre")
            .annotate(piezas=Sum("cantidad")).order_by("-piezas")
        ),
    }

    cuarentena = list(
        saldos.filter(estado=Saldo.CUARENTENA)
        .values("sku__cliente__nombre", "sku__codigo", "sku__descripcion")
        .annotate(piezas=Sum("cantidad"))
        .order_by("sku__cliente__nombre", "sku__codigo")
    )
    cuarentena_piezas = sum(fila["piezas"] for fila in cuarentena)

    # ── Packing: EN_PICKING separados en surtiéndose vs listos para empacar ──
    qs_picking = (
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .select_related("cliente").prefetch_related("lineas")
    )
    if cliente is not None:
        qs_picking = qs_picking.filter(cliente=cliente)
    surtiendo, listos_empaque = [], []
    for pedido in qs_picking:
        pedido.piezas = sum(l.cantidad for l in pedido.lineas.all())
        pedido.piezas_pickeadas = sum(
            min(l.cantidad_pickeada, l.cantidad) for l in pedido.lineas.all()
        )
        (listos_empaque if pedido.lineas_completas else surtiendo).append(pedido)

    # ── Paquetes listos: EMPACADO / GUIA_GENERADA agrupados por corral SAL-* ──
    qs_corral = (
        Pedido.objects.filter(estado__in=[Pedido.EMPACADO, Pedido.GUIA_GENERADA])
        .select_related("cliente").prefetch_related("paquetes", "guias")
    )
    if cliente is not None:
        qs_corral = qs_corral.filter(cliente=cliente)
    orden_corrales = corrales_activos()
    mapa = _mapa_corrales()
    corrales = {
        codigo: {"codigo": codigo, "nombre": nombre, "pedidos": [], "paquetes": 0}
        for codigo, nombre in orden_corrales
    }
    en_corral = list(qs_corral)
    for pedido in en_corral:
        guia = _guia_activa(pedido) if pedido.estado == Pedido.GUIA_GENERADA else None
        carrier = guia.carrier if guia else _carrier_probable(pedido)
        pedido.n_paquetes = pedido.paquetes.count() or 1
        codigo = _corral_de_carrier(carrier, mapa)
        if codigo not in corrales:  # corral sin ubicación viva (p. ej. SAL-LOCAL viejo)
            corrales[codigo] = {"codigo": codigo, "nombre": codigo, "pedidos": [], "paquetes": 0}
            orden_corrales.append((codigo, codigo))
        grupo = corrales[codigo]
        grupo["pedidos"].append(pedido)
        grupo["paquetes"] += pedido.n_paquetes

    zonas = {
        "ordenes": ordenes,
        "putaway": putaway,
        "almacen": almacen,
        "surtiendo": surtiendo,
        "listos_empaque": listos_empaque,
        "en_picking_n": len(surtiendo) + len(listos_empaque),
        "corrales": [corrales[codigo] for codigo, _ in orden_corrales],
        "en_corral_n": len(en_corral),
        "paquetes_n": sum(grupo["paquetes"] for grupo in corrales.values()),
        "cuarentena": cuarentena,
        "cuarentena_piezas": cuarentena_piezas,
    }
    badges = {
        "descarga": len(ordenes),
        "almacen": almacen["total_vendible"],
        "packing": zonas["en_picking_n"],
        "paquetes_listos": len(en_corral),
        "cuarentena": cuarentena_piezas,
        "oficina": None,
    }
    # Zona inicial: descarga si hay una ASN viva en piso; si no, packing.
    descargando = any(orden.estado != OrdenEntrada.ANUNCIADA for orden in ordenes)
    zona_activa = "descarga" if descargando else "packing"
    return zonas, badges, zona_activa
