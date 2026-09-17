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
import re
from datetime import timedelta

from django.db.models import Q, Sum
from django.utils import timezone

# Zona del almacén en el plano: x 660-1300, y 680-1980.
RACK_X = 690
RACK_ANCHO = 580
RACK_Y0 = 760
RACK_Y_FIN = 1960


# Columna dentro del rack para el formato <PREFIJO>-<rack>-<lado>-<frente>-<piso>:
# izquierda/derecha × frente/atrás, de izquierda a derecha en el dibujo.
COLUMNAS_RACK = {("I", "F"): 0, ("I", "B"): 1, ("D", "F"): 2, ("D", "B"): 3}
PATRON_RACK_PISO = re.compile(r"^([A-Z]+)-(\d+)-(\d+)$")
PATRON_RACK_LADO = re.compile(r"^([A-Z]+)-(\d+)-([ID])-([FB])-(\d+)$")


def racks_bodega():
    """Racks REALES agrupados desde las Ubicaciones (picking/reserva).

    Dos formatos de código conviven:
    - PIC-3-2 = rack 3, piso 2: una fila por piso a lo ancho del rack (los 4
      pisos se ven — pedido de Chema, sep-2026), pisos de arriba abajo en
      orden ascendente como siempre.
    - PIC-1-I-F-1 = rack 1, lado izquierdo (I) o derecho (D), frente (F) o
      atrás (B), piso 1 (formato sep-2026, racks dobles): cada piso es una
      fila con cuatro celdas I-F · I-B · D-F · D-B y el piso de hasta arriba
      (la reserva, RES-1-D-F-4) se dibuja arriba, como en el físico.
    La agrupación es POR NÚMERO DE RACK ignorando la letra (la letra es el
    tipo del piso, no el rack). Códigos sin patrón van como bloque de un piso.
    Regresa [{etiqueta, filas: [{x, y, w, h, cx, cy, codigo, mini}]}].
    """
    from apps.catalogo.models import Ubicacion  # lazy por contrato

    grupos = {}
    for codigo in (
        Ubicacion.objects.filter(
            tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA], activo=True,
        ).order_by("codigo").values_list("codigo", flat=True)
    ):
        lado = PATRON_RACK_LADO.match(codigo)
        piso_solo = PATRON_RACK_PISO.match(codigo)
        if lado:
            clave, piso = f"Rack {lado.group(2)}", int(lado.group(5))
            columna = COLUMNAS_RACK[(lado.group(3), lado.group(4))]
        elif piso_solo:
            clave, piso, columna = f"Rack {piso_solo.group(2)}", int(piso_solo.group(3)), None
        else:
            clave, piso, columna = codigo, 1, None
        grupos.setdefault(clave, []).append((piso, columna, codigo))

    if not grupos:
        return []

    def _orden(item):
        clave = item[0]
        num = clave.split(" ")[-1]
        return (0, int(num)) if num.isdigit() else (1, 0)

    pitch = (RACK_Y_FIN - RACK_Y0) // max(len(grupos), 1)
    ancho_celda = RACK_ANCHO // len(COLUMNAS_RACK)
    racks = []
    for indice, (clave, celdas) in enumerate(sorted(grupos.items(), key=_orden)):
        doble = any(columna is not None for _p, columna, _c in celdas)
        pisos = sorted({p for p, _col, _c in celdas}, reverse=doble)
        y_rack = RACK_Y0 + indice * pitch
        alto_fila = max(24, min(52, (pitch - 26) // max(len(pisos), 1)))
        filas = []
        for j, piso in enumerate(pisos):
            y = y_rack + j * alto_fila
            for _p, columna, codigo in sorted(
                (c for c in celdas if c[0] == piso), key=lambda c: (c[1] is None, c[1] or 0, c[2]),
            ):
                if columna is None:
                    x, w = RACK_X, RACK_ANCHO
                else:
                    x, w = RACK_X + columna * ancho_celda, ancho_celda
                filas.append({
                    "x": x, "y": y, "w": w, "h": alto_fila,
                    "cx": x + w // 2, "cy": y + alto_fila // 2 + 8,
                    "codigo": codigo, "mini": columna is not None,
                })
        racks.append({"etiqueta": clave, "filas": filas})
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
    # Desglose por producto de cada anaquel (qué hay y de quién): vendible y
    # apartado por SKU y lote. Con `cliente` ya viene filtrado al tenant, así
    # que el portal jamás ve producto ajeno.
    productos = {}
    for fila in (
        saldos.filter(estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO])
        .values("ubicacion__codigo", "sku__cliente__nombre", "sku__codigo", "sku__descripcion", "lote__codigo", "estado")
        .annotate(piezas=Sum("cantidad"))
    ):
        clave = (fila["ubicacion__codigo"], fila["sku__cliente__nombre"], fila["sku__codigo"], fila["lote__codigo"] or "")
        p = productos.setdefault(clave, {
            "cliente": fila["sku__cliente__nombre"], "sku": fila["sku__codigo"],
            "descripcion": fila["sku__descripcion"], "lote": fila["lote__codigo"] or "",
            "vendible": 0, "apartado": 0,
        })
        p["vendible" if fila["estado"] == Saldo.UBICADO_VENDIBLE else "apartado"] += fila["piezas"]
    for clave in sorted(productos):
        registro = por_ubicacion.setdefault(clave[0], {"codigo": clave[0], "vendible": 0, "apartado": 0})
        registro.setdefault("productos", []).append(productos[clave])
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

    # Corrales del PLANO: subs posicionados dinámicamente (zona x 85..616).
    corrales_svg = []
    n_corr = len(orden_corrales) or 1
    ancho = max(80, (531 - (n_corr - 1) * 18) // n_corr)
    for i, (codigo, _nombre) in enumerate(orden_corrales):
        x = 85 + i * (ancho + 18)
        corrales_svg.append({
            "x": x, "y": 290, "w": ancho, "h": 235,
            "cx": x + ancho // 2, "cy": 415, "codigo": codigo,
        })

    zonas = {
        "corrales_svg": corrales_svg,
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
