"""Vistas del operador de piso (tablet) — contrato CONVENTIONS.md §piso.

Flujo lineal y táctil: recibir → ubicar → pickear → empacar → salida →
entrega local, más los conteos cíclicos del día. Cada acción llama a los
servicios de dominio (inventario / pedidos / envios, imports lazy por
contrato) — el piso jamás toca Saldo, Movimiento ni estados directo.
"""
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU, Lote, Ubicacion
from apps.core.decorators import rol_requerido
from apps.core.models import EventoAuditoria, EvidenciaFoto
from apps.core.services import registrar_evento
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo, TareaConteo
from apps.pedidos.models import Pedido

# Corrales de staging (catalogo.Ubicacion tipo=salida): cada corral declara sus
# carriers en el campo `carriers`; el que no declara ninguno es el comodín.
CORRAL_LOCAL = "SAL-LOCAL"
CORRAL_OTRO = "SAL-OTRO"
CORRALES_LEGACY = [
    ("SAL-PQX", "Paquetexpress"),
    (CORRAL_LOCAL, "Entrega local"),
    (CORRAL_OTRO, "Otros carriers"),
]


def _corrales_bd():
    from apps.catalogo.models import Ubicacion  # lazy por contrato
    return list(Ubicacion.objects.filter(tipo=Ubicacion.SALIDA, activo=True).order_by("codigo"))


def corrales_activos():
    """[(codigo, nombre)] de los corrales vivos; sin corrales en BD, trío legacy."""
    filas = _corrales_bd()
    if not filas:
        return list(CORRALES_LEGACY)
    return [(u.codigo, ", ".join(u.lista_carriers()) or "Otros carriers") for u in filas]

ESTADOS_ASN_ABIERTOS = [OrdenEntrada.ANUNCIADA, OrdenEntrada.EN_RECEPCION, OrdenEntrada.RECIBIDA]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _flota_propia():
    """TORRE["FLOTA_PROPIA"]: sin flota, ningún pedido nuevo se agrupa como 'local'."""
    return bool(settings.TORRE.get("FLOTA_PROPIA", False))


def _mapa_corrales():
    """({carrier: corral}, comodín) leído de los corrales activos.

    "local" jamás entra aquí: conserva SAL-LOCAL (guías viejas, ver
    _corral_de_carrier). Sin corrales en BD aplica el mapeo legacy.
    """
    mapa, comodin = {}, None
    for ubic in _corrales_bd():
        declarados = ubic.lista_carriers()
        if not declarados and comodin is None and ubic.codigo != CORRAL_LOCAL:
            comodin = ubic.codigo
        for carrier in declarados:
            mapa.setdefault(carrier, ubic.codigo)
    if not mapa and comodin is None:
        mapa = {"paquetexpress": "SAL-PQX"}
    return mapa, comodin or CORRAL_OTRO


def _corral_de_carrier(carrier, mapa=None):
    """Corral de staging del carrier; "local" conserva SAL-LOCAL (datos viejos)."""
    if carrier == "local":
        return CORRAL_LOCAL
    if mapa is None:
        mapa = _mapa_corrales()
    asignados, comodin = mapa
    return asignados.get(carrier, comodin)


def _carrier_probable(pedido):
    """Carrier que le tocará al pedido, para agruparlo en su corral (lazy a envios).

    Sin flota propia (TORRE["FLOTA_PROPIA"]=False) un pedido es_local jamás
    se agrupa como "local": va al corral de su carrier real (p. ej. estafeta
    local → SAL-OTRO, paquetexpress local → SAL-PQX).
    """
    primero = pedido.paquetes.order_by("numero").first()
    if primero is not None and primero.carrier:
        return primero.carrier
    try:
        from apps.envios.services import elegir_carrier  # lazy por contrato
    except ImportError:
        if pedido.es_local and _flota_propia():
            return "local"
        return pedido.cliente.carrier_preferente or "paquetexpress"
    return elegir_carrier(pedido)[0]


def _guia_activa(pedido):
    """Guía vigente del pedido (solo un RETORNO la desactiva)."""
    return (
        pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS))
        .order_by("-id")
        .first()
    )


def _entero(valor, mensaje):
    """Convierte a int o truena con ValueError con mensaje claro para el piso."""
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        raise ValueError(mensaje)


def _piezas(pedido):
    return sum(linea.cantidad for linea in pedido.lineas.all())


def _avance(pedido):
    """(pickeadas, total, porcentaje) del pedido, para medidores."""
    total = pickeadas = 0
    for linea in pedido.lineas.all():
        total += linea.cantidad
        pickeadas += min(linea.cantidad_pickeada, linea.cantidad)
    pct = int(round(pickeadas * 100.0 / total)) if total else 0
    return pickeadas, total, pct


def _sla_recepcion(orden):
    """(texto, tono) del reloj SLA de la orden; ('', '') si aún no corre."""
    restante = orden.sla_restante
    if restante is None:
        return "", ""
    minutos = int(restante.total_seconds() // 60)
    if minutos < 0:
        horas, mins = divmod(-minutos, 60)
        return f"SLA vencido hace {horas} h {mins:02d} min", "crit"
    horas, mins = divmod(minutos, 60)
    tono = "warn" if minutos <= 120 else "ok"
    return f"Quedan {horas} h {mins:02d} min para quedar vendible", tono


# ─────────────────────────────────────────────────────────────────────────────
# Mi turno: LA card del siguiente pedido + agenda del día
# ─────────────────────────────────────────────────────────────────────────────


def _corte_hoy():
    """Datetime (aware) del corte contractual de hoy (TORRE["CORTE_CONTRACTUAL"])."""
    crudo = str(settings.TORRE["CORTE_CONTRACTUAL"])
    try:
        hora, minuto = (int(p) for p in crudo.split(":")[:2])
    except (TypeError, ValueError):
        hora, minuto = 14, 0
    return timezone.localtime().replace(hour=hora, minute=minuto, second=0, microsecond=0)


def _reloj_corte():
    """(hora, texto, tono) del countdown al corte: warn si queda <1 h."""
    corte = _corte_hoy()
    minutos = int((corte - timezone.localtime()).total_seconds() // 60)
    hora = corte.strftime("%H:%M")
    if minutos < 0:
        return hora, "corte vencido — lo comprometido de hoy ya debió salir", "crit"
    horas, mins = divmod(minutos, 60)
    texto = f"quedan {horas} h {mins:02d} m" if horas else f"quedan {mins} min"
    return hora, texto, "warn" if minutos < 60 else "ok"


def _pendientes_por_prioridad():
    """PENDIENTES en orden de cola: primero los que rebasan el corte de hoy
    (entraron antes del corte → salen HOY), luego por creado ascendente."""
    corte = _corte_hoy()
    return sorted(
        Pedido.objects.filter(estado=Pedido.PENDIENTE)
        .select_related("cliente").prefetch_related("lineas"),
        key=lambda p: (p.creado > corte, p.creado),
    )


def _abierto_mas_viejo():
    """El EN_PICKING más viejo (para reanudar trabajo abierto), o None."""
    en_picking = sorted(
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .select_related("cliente").prefetch_related("lineas"),
        key=lambda p: p.ts_picking or p.creado,
    )
    return en_picking[0] if en_picking else None


def _siguiente_en_cola():
    """El pedido de LA card según prioridad del SERVIDOR (el empleado no decide).

    1º EN_PICKING ya empezados (terminar lo abierto, el más viejo primero);
    2º PENDIENTE por prioridad de cola.
    """
    abierto = _abierto_mas_viejo()
    if abierto is not None:
        return abierto
    pendientes = _pendientes_por_prioridad()
    return pendientes[0] if pendientes else None


def _destino_pedido(pedido):
    """A dónde sigue un pedido del carril: picking o wizard de empaque."""
    if pedido.lineas_completas:
        return redirect("piso:empaque_pedido", pk=pedido.pk)
    return redirect("piso:picking_pedido", pk=pedido.pk)


def _home_siguiente(request):
    """EMPEZAR ▶: toma el siguiente de la cola con manejo de carrera.

    La card puede ofrecer REANUDAR un EN_PICKING (empezado=1): ese se sigue
    directo. Para tomar uno nuevo, un EN_PICKING cuenta como TOMADO por el
    otro operador: si el candidato PENDIENTE lo ganó alguien entre el render
    y el POST, se toma el que sigue SIN error visible (select_for_update
    decide quién ganó — dos empleados jamás agarran el mismo).
    """
    from apps.pedidos.services import iniciar_picking  # lazy por contrato

    if request.POST.get("empezado"):
        abierto = Pedido.objects.filter(
            pk=request.POST.get("pedido_id"), estado=Pedido.EN_PICKING,
        ).first()
        if abierto is not None:
            return _destino_pedido(abierto)

    for _ in range(8):
        pendientes = _pendientes_por_prioridad()
        if not pendientes:
            abierto = _abierto_mas_viejo()
            if abierto is not None:
                return _destino_pedido(abierto)
            messages.success(request, "Todo al día: no hay pedidos en la cola.")
            return redirect("piso:home")
        candidato = pendientes[0]
        with transaction.atomic():
            pedido = Pedido.objects.select_for_update().get(pk=candidato.pk)
            if pedido.estado != Pedido.PENDIENTE:
                continue  # carrera: otro lo tomó — sigue el siguiente, sin drama
            try:
                iniciar_picking(pedido, request.user)
            except ValueError:
                continue
        return redirect("piso:picking_pedido", pk=pedido.pk)
    return redirect("piso:home")


@rol_requerido("piso", "mesa")
def home(request):
    """Mi turno: LA card con el siguiente pedido + la agenda del día debajo."""
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "confirmar_restock":
            return _home_confirmar_restock(request)
        if accion == "siguiente":
            return _home_siguiente(request)
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:home")

    hoy = timezone.localdate()
    recepciones_abiertas = list(
        OrdenEntrada.objects.filter(estado__in=ESTADOS_ASN_ABIERTOS).select_related("cliente")
    )
    pendientes = list(Pedido.objects.filter(estado=Pedido.PENDIENTE).select_related("cliente"))
    en_picking = list(
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .select_related("cliente").prefetch_related("lineas")
    )
    por_pickear = [p for p in en_picking if not p.lineas_completas]
    por_empacar = [p for p in en_picking if p.lineas_completas]
    staging = list(
        Pedido.objects.filter(estado__in=[Pedido.EMPACADO, Pedido.GUIA_GENERADA])
        .select_related("cliente")
    )
    flota = _flota_propia()
    entregas_locales = []
    if flota:
        entregas_locales = list(
            Pedido.objects.filter(es_local=True, estado__in=[Pedido.RECOLECTADO, Pedido.EN_TRANSITO])
            .select_related("cliente")
        )
    restocks = list(
        Pedido.objects.filter(estado=Pedido.CANCELACION_PENDIENTE)
        .select_related("cliente").prefetch_related("lineas")
    )
    for pedido in restocks:
        pedido.total_piezas = _piezas(pedido)
    tareas_conteo = list(
        TareaConteo.objects.filter(fecha=hoy).select_related("sku", "sku__cliente")
    )
    conteos_pendientes = [t for t in tareas_conteo if t.estado == TareaConteo.PENDIENTE]

    siguiente = _siguiente_en_cola()
    if siguiente is not None:
        siguiente.total_piezas = _piezas(siguiente)
        siguiente.num_lineas = siguiente.lineas.count()
        siguiente.ya_empezado = siguiente.estado == Pedido.EN_PICKING

    total_picking = len(pendientes) + len(por_pickear)
    corte_hora, corte_texto, corte_tono = _reloj_corte()
    todo_al_dia = (
        siguiente is None and not recepciones_abiertas and not conteos_pendientes
        and not restocks and not staging and not por_empacar
    )

    contexto = {
        "seccion": "home",
        "todo_al_dia": todo_al_dia,
        "siguiente": siguiente,
        "recepciones_abiertas": recepciones_abiertas,
        "num_por_pickear": total_picking,
        "por_empacar": por_empacar,
        "staging": staging,
        "conteos_pendientes": conteos_pendientes,
        "entregas_locales": entregas_locales,
        "restocks": restocks,
        "flota_propia": flota,
        "corte_hora": corte_hora,
        "corte_texto": corte_texto,
        "corte_tono": corte_tono,
    }
    return render(request, "piso/home.html", contexto)


def _home_confirmar_restock(request):
    """Cierra la tarea de restock de una cancelación (mercancía de vuelta al anaquel)."""
    pedido = get_object_or_404(Pedido, pk=request.POST.get("pedido_id"))
    from apps.pedidos.services import confirmar_restock  # lazy por contrato
    try:
        confirmar_restock(pedido, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Restock confirmado: {pedido.folio} quedó cancelado y la mercancía regresó al inventario.",
        )
    return redirect("piso:home")


# ─────────────────────────────────────────────────────────────────────────────
# Recepciones (ASN): recibir → foto de llegada → ubicar → cerrar
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("piso", "mesa")
def recepciones(request):
    abiertas = list(
        OrdenEntrada.objects.filter(estado__in=ESTADOS_ASN_ABIERTOS)
        .select_related("cliente").prefetch_related("lineas__sku")
    )
    for orden in abiertas:
        orden.sla_texto, orden.sla_tono = _sla_recepcion(orden)
        orden.total_anunciado = sum(l.cantidad_anunciada for l in orden.lineas.all())
        orden.total_recibido = sum(l.cantidad_recibida + l.cantidad_danada for l in orden.lineas.all())
    cerradas = list(
        OrdenEntrada.objects.filter(estado=OrdenEntrada.CERRADA).select_related("cliente")[:5]
    )
    contexto = {"seccion": "recepcion", "abiertas": abiertas, "cerradas": cerradas}
    return render(request, "piso/recepciones.html", contexto)


@rol_requerido("piso", "mesa")
def recepcion_detalle(request, pk):
    orden = get_object_or_404(OrdenEntrada.objects.select_related("cliente"), pk=pk)

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "recibir":
            return _recepcion_recibir(request, orden)
        if accion == "ubicar":
            return _recepcion_ubicar(request, orden)
        if accion == "cerrar":
            return _recepcion_cerrar(request, orden)
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:recepcion_detalle", pk=orden.pk)

    lineas = list(orden.lineas.select_related("sku"))
    putaway = dict(
        Saldo.objects.filter(sku_id__in=[l.sku_id for l in lineas], estado=Saldo.EN_PUTAWAY)
        .values_list("sku_id")
        .annotate(t=Sum("cantidad"))
        .values_list("sku_id", "t")
    )
    for linea in lineas:
        linea.por_ubicar = putaway.get(linea.sku_id, 0)
    sla_texto, sla_tono = _sla_recepcion(orden)
    contexto = {
        "seccion": "recepcion",
        "orden": orden,
        "lineas": lineas,
        "pendiente_putaway": sum(putaway.values()),
        "sla_texto": sla_texto,
        "sla_tono": sla_tono,
        "ubicaciones_destino": Ubicacion.objects.filter(
            tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA], activo=True
        ),
        "fotos_llegada": EvidenciaFoto.objects.filter(entidad="asn", entidad_id=orden.folio),
    }
    return render(request, "piso/recepcion_detalle.html", contexto)


def _recepcion_recibir(request, orden):
    """Recibe una línea (buenas + dañadas) y liga la foto de llegada al ASN.

    La foto de llegada es OBLIGATORIA en el primer registro de recepción de la
    orden (SOP RE-01: foto del camión/tarimas ligada al ASN); después es opcional.
    """
    destino = redirect("piso:recepcion_detalle", pk=orden.pk)
    linea = get_object_or_404(LineaASN, pk=request.POST.get("linea_id"), orden=orden)
    foto = request.FILES.get("foto_llegada")
    try:
        cantidad_ok = _entero(request.POST.get("cantidad_ok") or 0, "Captura la cantidad en buen estado con números enteros.")
        cantidad_danada = _entero(request.POST.get("cantidad_danada") or 0, "Captura la cantidad dañada con números enteros.")
        if (
            foto is None
            and (cantidad_ok > 0 or cantidad_danada > 0)
            and not EvidenciaFoto.objects.filter(
                entidad="asn", entidad_id=orden.folio, tipo="llegada",
            ).exists()
        ):
            raise ValueError("Tómale foto al camión/tarimas antes de registrar la primera línea.")
        from apps.inventario.services import recibir  # lazy por contrato
        recibir(linea, cantidad_ok, cantidad_danada, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino

    if foto:
        EvidenciaFoto.objects.create(
            entidad="asn", entidad_id=orden.folio, tipo="llegada",
            archivo=foto, tomada_por=request.user.username,
        )
    partes = []
    if cantidad_ok > 0:
        partes.append(f"{cantidad_ok} en buen estado")
    if cantidad_danada > 0:
        partes.append(f"{cantidad_danada} dañadas (van a cuarentena)")
    messages.success(
        request,
        f"Recibido {linea.sku.codigo}: {' y '.join(partes)}. Ahora ubícalo para que sea vendible.",
    )
    return destino


def _recepcion_ubicar(request, orden):
    """Put-away: mueve lo recibido a la ubicación escaneada (ahí se vuelve vendible)."""
    destino = redirect("piso:recepcion_detalle", pk=orden.pk)
    sku_ids = set(orden.lineas.values_list("sku_id", flat=True))
    try:
        sku_id = _entero(request.POST.get("sku_id"), "Elige el producto que vas a ubicar.")
        cantidad = _entero(request.POST.get("cantidad"), "Captura cuántas piezas vas a ubicar.")
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    if sku_id not in sku_ids:
        messages.error(request, "Ese producto no es de esta orden. Revisa la etiqueta.")
        return destino
    sku = get_object_or_404(SKU, pk=sku_id)

    codigo_ubicacion = (request.POST.get("ubicacion") or "").strip()
    if not codigo_ubicacion:
        messages.error(request, "Escanea la etiqueta del anaquel destino antes de ubicar.")
        return destino
    ubicacion = Ubicacion.objects.filter(codigo__iexact=codigo_ubicacion).first()
    if ubicacion is None:
        messages.error(
            request,
            f"No existe la ubicación {codigo_ubicacion}. Escanea la etiqueta del anaquel, no la del producto.",
        )
        return destino

    lote = None
    lote_codigo = (request.POST.get("lote") or "").strip()
    if lote_codigo:
        fecha_caducidad = None
        crudo = (request.POST.get("fecha_caducidad") or "").strip()
        if crudo:
            try:
                fecha_caducidad = date.fromisoformat(crudo)
            except ValueError:
                messages.error(request, "La fecha de caducidad no es válida. Usa el calendario.")
                return destino
        lote, _ = Lote.objects.get_or_create(
            sku=sku, codigo=lote_codigo, defaults={"fecha_caducidad": fecha_caducidad}
        )

    from apps.inventario.services import ubicar  # lazy por contrato
    try:
        ubicar(sku, cantidad, ubicacion, lote, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request,
        f"{cantidad} × {sku.codigo} ubicadas en {ubicacion.codigo}: ya cuentan como vendibles.",
    )
    return destino


def _recepcion_cerrar(request, orden):
    """Cierra la orden vía inventario.cerrar_recepcion: valida el put-away,
    guarda las tarimas recibidas y dispara la confirmación honesta al cliente."""
    destino = redirect("piso:recepcion_detalle", pk=orden.pk)
    from apps.inventario.services import cerrar_recepcion  # lazy por contrato
    try:
        crudo = (request.POST.get("tarimas_recibidas") or "").strip()
        tarimas_recibidas = (
            _entero(crudo, "Captura las tarimas recibidas con números enteros.")
            if crudo else orden.tarimas
        )
        cerrar_recepcion(orden, request.user, tarimas_recibidas=tarimas_recibidas)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(request, f"Orden {orden.folio} cerrada: todo el producto quedó vendible.")
    return destino


# ─────────────────────────────────────────────────────────────────────────────
# Picking: olas EN_PICKING con escaneo por línea
# ─────────────────────────────────────────────────────────────────────────────



def _paquetes_con_lineas(pedido):
    """Plan de división visible para pickers/packers: qué va en cada caja y por qué."""
    from apps.envios.models import Paquete  # import local: el plan puede no existir aún
    return list(
        Paquete.objects.filter(pedido=pedido)
        .prefetch_related("lineas__linea_pedido__sku")
        .order_by("numero")
    )


def _ahorro_division(pedido):
    primero = pedido.paquetes.order_by("numero").first()
    return primero.ahorro_plan_mxn if primero else 0


def _marcar_paquetes_empacados(pedido, actor):
    """Al empacar el pedido, todos sus paquetes planeados pasan a EMPACADO."""
    for paquete in pedido.paquetes.all():
        if paquete.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE):
            try:
                paquete.transicionar(Paquete.EMPACADO, actor=actor, motivo="Empaque del pedido completado")
            except ValueError:
                pass


@rol_requerido("piso", "mesa")
def picking(request):
    if request.method == "POST" and request.POST.get("accion") == "iniciar":
        from apps.pedidos.services import iniciar_picking  # lazy por contrato
        # Mismo patrón de _home_siguiente: select_for_update decide quién ganó
        # el pedido — dos operadores jamás inician el mismo.
        with transaction.atomic():
            pedido = get_object_or_404(
                Pedido.objects.select_for_update(), pk=request.POST.get("pedido_id"),
            )
            if pedido.estado == Pedido.EN_PICKING:
                messages.error(
                    request,
                    f"Otro operador ya tomó {pedido.folio}. Elige otro pedido de la lista.",
                )
                return redirect("piso:picking")
            try:
                iniciar_picking(pedido, request.user)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("piso:picking")
        messages.success(request, f"Picking de {pedido.folio} iniciado. Escanea línea por línea.")
        return redirect("piso:picking_pedido", pk=pedido.pk)

    pendientes = list(
        Pedido.objects.filter(estado=Pedido.PENDIENTE)
        .select_related("cliente").prefetch_related("lineas__sku")
    )
    for pedido in pendientes:
        pedido.total_piezas = _piezas(pedido)
    en_picking = list(
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .select_related("cliente").prefetch_related("lineas__sku")
    )
    for pedido in en_picking:
        pedido.pickeadas, pedido.total_piezas, pedido.avance_pct = _avance(pedido)
    contexto = {"seccion": "picking", "pendientes": pendientes, "en_picking": en_picking}
    return render(request, "piso/picking.html", contexto)


def _lineas_en_ruta(pedido):
    """Líneas del pedido ordenadas por su primera ubicación FEFO (la ruta).

    Cada línea trae .ubicaciones (top 3 FEFO) y .completa; el orden es por
    el código de la primera ubicación — el picker camina el pasillo una vez,
    sin zigzag. Sin stock a la vista → al final.
    """
    lineas = list(pedido.lineas.select_related("sku"))
    for linea in lineas:
        linea.completa = linea.cantidad_pickeada >= linea.cantidad
        linea.ubicaciones = list(
            Saldo.objects.filter(sku=linea.sku, estado=Saldo.UBICADO_VENDIBLE, cantidad__gt=0)
            .select_related("ubicacion", "lote")
            .order_by("lote__fecha_caducidad", "ubicacion__codigo")[:3]
        )
        linea.primera_ubicacion = linea.ubicaciones[0] if linea.ubicaciones else None
    lineas.sort(
        key=lambda l: (
            l.primera_ubicacion is None,
            l.primera_ubicacion.ubicacion.codigo if l.primera_ubicacion else "",
            l.pk,
        )
    )
    return lineas


def _quiere_json(request):
    return "application/json" in (request.headers.get("Accept") or "")


@rol_requerido("piso", "mesa")
def picking_pedido(request, pk):
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=pk)
    if pedido.estado != Pedido.EN_PICKING:
        if _quiere_json(request):
            return JsonResponse(
                {"ok": False, "error": f"El pedido {pedido.folio} no está en picking."},
                status=409,
            )
        messages.error(
            request,
            f"El pedido {pedido.folio} no está en picking (está {pedido.get_estado_display()}).",
        )
        return redirect("piso:picking")

    if request.method == "POST":
        return _picking_escanear(request, pedido)

    lineas = _lineas_en_ruta(pedido)
    pickeadas, total, _ = _avance(pedido)
    contexto = {
        "paquetes": _paquetes_con_lineas(pedido),
        "ahorro_division": _ahorro_division(pedido),
        "seccion": "picking",
        "pedido": pedido,
        "lineas": lineas,
        "completo": all(l.completa for l in lineas),
        "pickeadas": pickeadas,
        "total_piezas": total,
    }
    return render(request, "piso/picking_pedido.html", contexto)


def _picking_json(pedido, linea, lineas):
    """Payload del confirmar AJAX: avance sin reload para el visor."""
    pickeadas, total, _ = _avance(pedido)
    completo = all(l.cantidad_pickeada >= l.cantidad for l in lineas)
    return JsonResponse({
        "ok": True,
        "linea_id": linea.pk,
        "sku": linea.sku.codigo,
        "pickeada": linea.cantidad_pickeada,
        "cantidad": linea.cantidad,
        "restante": max(linea.cantidad - linea.cantidad_pickeada, 0),
        "completo": completo,
        "avance": {"pickeadas": pickeadas, "total": total},
        "siguiente": reverse("piso:empaque_pedido", args=[pedido.pk]) if completo else None,
    })


def _picking_escanear(request, pedido):
    """Escaneo por línea: código de barras + cantidad, validado contra el SKU real.

    Con `Accept: application/json` (visor de cámara) regresa JSON con el
    avance — el POST clásico (fallback sin JS) sigue con PRG y flashes.
    """
    quiere_json = _quiere_json(request)
    destino = redirect("piso:picking_pedido", pk=pedido.pk)

    def error(mensaje):
        if quiere_json:
            return JsonResponse({"ok": False, "error": mensaje}, status=400)
        messages.error(request, mensaje)
        return destino

    codigo = (request.POST.get("codigo") or "").strip()
    if not codigo:
        return error("Escanea el código de barras del producto.")

    lineas = list(pedido.lineas.select_related("sku"))
    candidatas = [
        l for l in lineas
        if codigo in {c for c in (l.sku.codigo_barras, l.sku.codigo) if c}
    ]
    if not candidatas:
        return error(
            f"Código equivocado: {codigo} no corresponde a ningún producto de este pedido. "
            "Regresa la pieza y toma la correcta."
        )
    linea = next((l for l in candidatas if l.cantidad_pickeada < l.cantidad), None)
    if linea is None:
        return error(
            f"La línea de {candidatas[0].sku.codigo} ya está completa. No pickees de más."
        )

    from apps.pedidos.services import confirmar_linea_pick  # lazy por contrato
    try:
        confirmar_linea_pick(
            linea, request.POST.get("cantidad") or "1", request.user, codigo_escaneado=codigo
        )
    except ValueError as exc:
        return error(str(exc))

    if quiere_json:
        return _picking_json(pedido, linea, lineas)

    if all(l.cantidad_pickeada >= l.cantidad for l in lineas):
        messages.success(request, f"Pedido {pedido.folio} completo. Llévalo a la mesa de empaque.")
        return redirect("piso:empaque_pedido", pk=pedido.pk)
    messages.success(
        request, f"{linea.sku.codigo}: van {linea.cantidad_pickeada} de {linea.cantidad}."
    )
    return destino


# ─────────────────────────────────────────────────────────────────────────────
# Empaque: checklist + báscula ±3% + 2 fotos obligatorias
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("piso", "mesa")
def empaque(request):
    en_picking = list(
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .select_related("cliente").prefetch_related("lineas__sku")
    )
    listos = [p for p in en_picking if p.lineas_completas]
    incompletos = [p for p in en_picking if not p.lineas_completas]
    for pedido in listos:
        pedido.total_piezas = _piezas(pedido)
    for pedido in incompletos:
        pedido.pickeadas, pedido.total_piezas, pedido.avance_pct = _avance(pedido)
    contexto = {"seccion": "empaque", "listos": listos, "incompletos": incompletos}
    return render(request, "piso/empaque.html", contexto)


def _checklist_empaque(pedido):
    """(checklist, naked) del cliente para el wizard — colapsado en la UI."""
    naked = pedido.es_local and pedido.cliente.naked_packing_local
    if naked:
        checklist = [
            f"Caja comercial LIMPIA de {pedido.cliente.nombre} — cero branding ajeno",
            "Naked packing: cero cinta plástica, cero relleno visible de terceros",
            "Revisa que ninguna botella golpee con otra",
        ]
    else:
        checklist = [
            f"Plástico burbuja OFICIAL de {pedido.cliente.nombre} (muro de insumos del cliente)",
            f"Cinta OFICIAL de {pedido.cliente.nombre} — nunca cinta genérica ni del 3PL",
            "Botellas separadas entre sí; prueba de agitado sin tintineo",
        ]
    if pedido.nota_regalo:
        checklist.append("Mete la nota de regalo — este pedido la trae")
    return checklist, naked


def _caja_cerrada(paquete):
    """True si la caja ya tiene su foto de cierre (candado de cerrar_caja)."""
    return EventoAuditoria.objects.filter(
        entidad="paquete", entidad_id=str(paquete.pk), accion="caja_cerrada_con_evidencia",
    ).exists()


def _rango_peso(gramos):
    """(min, max) en gramos con la tolerancia contractual de báscula."""
    tolerancia = float(settings.TORRE["TOLERANCIA_PESO_PCT"])
    return (
        int(round(gramos * (1 - tolerancia / 100.0))),
        int(round(gramos * (1 + tolerancia / 100.0))),
        tolerancia,
    )


@rol_requerido("piso", "mesa")
def empaque_pedido(request, pk):
    """Wizard de empaque por caja del carril único.

    Pasos: foto CONTENIDO + peso por caja (empacar_caja; la última encadena
    guía + impresión) → foto de CAJA CERRADA con etiqueta por caja
    (cerrar_caja) → pantalla de éxito con SIGUIENTE PEDIDO ▶. El pedido
    legacy sin plan de paquetes corre el mismo wizard con 1 caja (empacar
    clásico + cierre implícito).
    """
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=pk)

    if request.method == "POST":
        accion = request.POST.get("accion") or "empacar"
        if accion == "empacar_caja":
            return _empaque_caja(request, pedido)
        if accion == "cerrar_caja":
            return _empaque_cerrar_caja(request, pedido)
        if accion == "cerrar_legacy":
            return _empaque_cerrar_legacy(request, pedido)
        if accion == "contenido_kit":
            return _empaque_contenido_kit(request, pedido)
        if accion == "quitar_contenido_kit":
            return _empaque_quitar_contenido_kit(request, pedido)
        return _empacar(request, pedido)

    if pedido.estado in (Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        return _render_cierre_o_exito(request, pedido)
    if pedido.estado != Pedido.EN_PICKING:
        messages.error(
            request,
            f"El pedido {pedido.folio} no se puede empacar: está {pedido.get_estado_display()}.",
        )
        return redirect("piso:empaque")
    if not pedido.lineas_completas:
        messages.error(
            request,
            f"Faltan piezas por pickear en {pedido.folio}. Termina el escaneo antes de empacar.",
        )
        return redirect("piso:picking_pedido", pk=pedido.pk)
    return _render_paso_empacar(request, pedido)


_INDICE_KIT = re.compile(r"^sku_(\d+)$")


def _empaque_contenido_kit(request, pedido):
    """Declara los componentes del kit (renglones sku_N/cantidad_N, patrón ASN)."""
    from apps.pedidos.models import LineaPedido
    from apps.pedidos.services import declarar_contenido_kit  # lazy por contrato

    linea = get_object_or_404(LineaPedido, pk=request.POST.get("linea_kit"), pedido=pedido)
    activos = {
        str(s.pk): s
        for s in SKU.objects.filter(cliente=pedido.cliente, activo=True, es_kit=False)
    }
    items = []
    indices = sorted({
        int(m.group(1)) for clave in request.POST if (m := _INDICE_KIT.match(clave))
    })
    for i in indices:
        sku = activos.get(request.POST.get(f"sku_{i}") or "")
        crudo = (request.POST.get(f"cantidad_{i}") or "").strip()
        if sku is None and not crudo:
            continue  # renglón vacío del "+"
        try:
            piezas = int(crudo)
        except ValueError:
            piezas = 0
        if sku is None or piezas < 1:
            messages.error(request, "Completa producto y piezas en cada renglón del kit.")
            return redirect("piso:empaque_pedido", pk=pedido.pk)
        items.append((sku, piezas))
    try:
        declarar_contenido_kit(linea, items, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Contenido del kit {linea.sku.codigo} declarado.")
    return redirect("piso:empaque_pedido", pk=pedido.pk)


def _empaque_quitar_contenido_kit(request, pedido):
    from apps.pedidos.models import LineaPedido
    from apps.pedidos.services import quitar_contenido_kit  # lazy por contrato

    linea = get_object_or_404(LineaPedido, pk=request.POST.get("linea_kit"), pedido=pedido)
    try:
        quitar_contenido_kit(linea, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Contenido del kit {linea.sku.codigo} liberado.")
    return redirect("piso:empaque_pedido", pk=pedido.pk)


def _recordar_peso_empaque(request, pedido):
    """POST de empaque fallido: el navegador tira la foto, el peso NO se pierde.

    Guarda el peso capturado en sesión para el re-render (PRG) y deja la
    señal de que hubo un error — el template avisa "vuelve a tomar la foto".
    """
    request.session[f"empaque_fallido_{pedido.pk}"] = (
        request.POST.get("peso_real_gr") or ""
    ).strip()


def _render_paso_empacar(request, pedido):
    """Paso 1-2 del wizard: CAJA i de N (o caja única legacy) — foto + peso."""
    checklist, naked = _checklist_empaque(pedido)
    cajas = _paquetes_con_lineas(pedido)
    pendientes = [c for c in cajas if c.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE)]
    fallo_previo = request.session.pop(f"empaque_fallido_{pedido.pk}", None)
    lineas = list(pedido.lineas.select_related("sku"))
    contexto = {
        "seccion": "empaque",
        "pedido": pedido,
        "paquetes": cajas,
        "ahorro_division": _ahorro_division(pedido),
        "checklist": checklist,
        "naked": naked,
        "lineas": lineas,
        "hubo_error_post": fallo_previo is not None,
        "peso_previo": fallo_previo or "",
    }

    # Kits: su contenido se declara aquí (candado de empacar); 7B lo trae ya
    # declarado desde la orden y solo se muestra.
    kits = [l for l in lineas if l.sku.es_kit and l.parte_de_kit_id is None]
    if kits:
        for linea in kits:
            linea.hijas = list(linea.componentes.select_related("sku"))
        contexto["kits"] = kits
        if any(not l.hijas for l in kits):
            from apps.catalogo.models import opciones_sku_agrupadas
            contexto["opciones_kit"] = opciones_sku_agrupadas(pedido.cliente, excluir_kits=True)

    if cajas and pendientes:
        caja = pendientes[0]
        plan_gr = int(caja.peso_kg * 1000) if caja.peso_kg else 0
        peso_min, peso_max, tolerancia = _rango_peso(plan_gr)
        contexto.update({
            "paso": "caja",
            "caja": caja,
            "total_cajas": len(cajas),
            "cajas_listas": len(cajas) - len(pendientes),
            "tolerancia": tolerancia,
            "peso_esperado": plan_gr,
            "peso_min": peso_min,
            "peso_max": peso_max,
        })
        return render(request, "piso/empaque_pedido.html", contexto)

    # Legacy sin plan de paquetes (o plan ya empacado por fuera): 1 caja.
    esperado = pedido.peso_esperado_gr or 0
    peso_min, peso_max, tolerancia = _rango_peso(esperado)
    contexto.update({
        "paso": "legacy",
        "total_cajas": 1,
        "tolerancia": tolerancia,
        "peso_esperado": esperado,
        "peso_min": peso_min,
        "peso_max": peso_max,
        "fotos_existentes": EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), tipo="contenido",
        ).count(),
    })
    return render(request, "piso/empaque_pedido.html", contexto)


def _render_cierre_o_exito(request, pedido):
    """Paso 3-5 del wizard: etiquetas impresas → foto de cierre → éxito."""
    contexto = {"seccion": "empaque", "pedido": pedido}
    if pedido.cajas_cerradas_completas:
        contexto.update({
            "paso": "exito",
            "corral": _corral_de_carrier(_carrier_probable(pedido)),
        })
        return render(request, "piso/empaque_pedido.html", contexto)

    guias = list(
        pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).order_by("id")
    )
    cajas_empacadas = [
        c for c in pedido.paquetes.order_by("numero")
        if c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO)
    ]
    por_cerrar = [c for c in cajas_empacadas if not _caja_cerrada(c)]
    contexto.update({
        "paso": "cierre",
        "guias": guias,
        "tiene_guia": bool(guias),
        "cajas_empacadas": cajas_empacadas,
        "por_cerrar": por_cerrar,
        "caja_cierre": por_cerrar[0] if por_cerrar else None,
        "cierre_legacy": not cajas_empacadas,  # 1 caja implícita
        "total_cierres": max(len(cajas_empacadas), 1),
        "cierres_hechos": max(len(cajas_empacadas), 1) - (len(por_cerrar) or 1),
    })
    return render(request, "piso/empaque_pedido.html", contexto)


def _despachar_y_avisar(request, pedido, destino):
    """MISMO POST del empaque: guía + impresión automáticas (best-effort).

    Un error del carrier deja el pedido EMPACADO y recuperable desde Salida
    (botón Generar guía); un error de impresora avisa y el flujo continúa
    (la reimpresión vive en Salida).
    """
    from apps.pedidos.services import despachar_a_corral  # lazy por contrato
    try:
        resultado = despachar_a_corral(pedido, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # ErrorCarrier u otra falla del adapter: el piso debe saberlo
        messages.error(
            request,
            f"El carrier no respondió al generar la guía de {pedido.folio}: {exc}. "
            "Genera la guía desde Salida en un momento o avisa a Mesa de Control.",
        )
    else:
        numeros = ", ".join(g.numero for g in resultado["guias"])
        messages.success(
            request,
            f"{len(resultado['guias'])} guía(s) listas para {pedido.folio} ({numeros}). "
            "Pega cada etiqueta en su caja y toma la foto de cierre por caja.",
        )
        for mensaje in resultado["mensajes"]:
            nivel = messages.warning if mensaje.startswith("No se imprimió") else messages.info
            nivel(request, mensaje)
    return destino


def _empaque_caja(request, pedido):
    """Confirmar de la caja actual: peso contra SU plan + foto de contenido.

    La última caja encadena (vía services) el empaque total del pedido y aquí
    mismo la guía + impresión — el packer solo pega etiquetas y toma cierres.
    """
    destino = redirect("piso:empaque_pedido", pk=pedido.pk)
    paquete = get_object_or_404(Paquete, pk=request.POST.get("paquete_id"), pedido=pedido)
    from apps.pedidos.services import empacar_caja  # lazy por contrato
    try:
        empacar_caja(
            paquete, request.user,
            request.POST.get("peso_real_gr"), request.FILES.get("foto_contenido"),
        )
    except ValueError as exc:
        _recordar_peso_empaque(request, pedido)
        messages.error(request, str(exc))
        return destino

    pedido.refresh_from_db()
    if pedido.estado != Pedido.EMPACADO:
        messages.success(
            request,
            f"Caja {paquete.numero} verificada ({paquete.peso_real_gr} g). Sigue la próxima caja.",
        )
        return destino
    messages.success(
        request, f"{pedido.folio} empacado y verificado: todas las cajas pesadas y con foto.",
    )
    return _despachar_y_avisar(request, pedido, destino)


def _empaque_cerrar_caja(request, pedido):
    """Foto de la caja cerrada CON su etiqueta pegada → cerrar_caja."""
    destino = redirect("piso:empaque_pedido", pk=pedido.pk)
    paquete = get_object_or_404(Paquete, pk=request.POST.get("paquete_id"), pedido=pedido)
    from apps.pedidos.services import cerrar_caja  # lazy por contrato
    try:
        cerrar_caja(paquete, request.user, request.FILES.get("foto_cierre"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(request, f"Caja {paquete.numero} cerrada con su etiqueta.")
    return destino


def _empaque_cerrar_legacy(request, pedido):
    """Cierre del pedido sin empaque por caja: 1 caja implícita con evidencia."""
    destino = redirect("piso:empaque_pedido", pk=pedido.pk)
    if pedido.estado not in (Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        messages.error(
            request, f"El pedido {pedido.folio} no está empacado; no hay caja que cerrar.",
        )
        return destino
    if pedido.paquetes.filter(estado__in=[Paquete.EMPACADO, Paquete.DESPACHADO]).exists():
        # Con cajas empacadas el cierre es POR CAJA (cerrar_caja): un cierre
        # único inflaría el conteo de evidencia sin foto de cada caja.
        messages.error(
            request,
            f"El pedido {pedido.folio} se empacó por caja: toma la foto de cierre "
            "de CADA caja, no un cierre único.",
        )
        return destino
    if _guia_activa(pedido) is None:
        messages.error(
            request,
            f"El pedido {pedido.folio} todavía no tiene guía; génerala en Salida, "
            "pega la etiqueta y entonces toma la foto de cierre.",
        )
        return destino
    if pedido.cajas_cerradas_completas:
        messages.success(request, f"{pedido.folio} ya tiene su foto de cierre.")
        return destino
    foto = request.FILES.get("foto_cierre")
    if foto is None:
        messages.error(request, "Toma la foto de la caja cerrada con la etiqueta pegada.")
        return destino
    evidencia = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo="caja_cerrada",
        archivo=foto, tomada_por=request.user.username,
    )
    registrar_evento(
        "pedido", pedido.pk, "caja_cerrada_con_evidencia", actor=request.user,
        cliente=pedido.cliente, delta={"evidencia_id": evidencia.pk},
        motivo=f"Caja de {pedido.folio} cerrada con la etiqueta pegada.",
    )
    messages.success(request, f"{pedido.folio}: foto de cierre guardada. Listo para el manifiesto.")
    return destino


def _empacar(request, pedido):
    """Empaque clásico (pedido legacy sin plan por caja): peso + foto contenido.

    Contrato del carril único: al empacar solo se exige la foto del CONTENIDO
    (la guía aún no existe). En el MISMO POST se encadena guía + impresión de
    etiquetas (pedidos.services.despachar_a_corral, best-effort); la foto de
    la caja cerrada con la etiqueta pegada se toma después (paso de cierre).
    """
    destino = redirect("piso:empaque_pedido", pk=pedido.pk)
    if pedido.estado != Pedido.EN_PICKING:
        messages.error(
            request,
            f"El pedido {pedido.folio} no se puede empacar: está {pedido.get_estado_display()}.",
        )
        return destino
    peso = (request.POST.get("peso_real_gr") or "").strip()
    if not peso:
        messages.error(request, "Captura el peso que marca la báscula, en gramos.")
        return destino

    foto_contenido = request.FILES.get("foto_contenido")
    existentes = EvidenciaFoto.objects.filter(
        entidad="pedido", entidad_id=str(pedido.pk), tipo="contenido",
    ).count()
    if not foto_contenido and existentes < 1:
        _recordar_peso_empaque(request, pedido)
        messages.error(
            request,
            "Falta la foto del contenido: tómala antes de marcar empacado. "
            "La foto de la caja cerrada se toma al final, con la etiqueta pegada.",
        )
        return destino

    # Instancia con tipo correcto; empacar() la liga al pedido y la guarda.
    fotos = []
    if foto_contenido:
        fotos.append(EvidenciaFoto(tipo="contenido", archivo=foto_contenido, tomada_por=request.user.username))

    from apps.pedidos.services import empacar  # lazy por contrato
    try:
        empacar(pedido, request.user, peso, fotos)
    except ValueError as exc:
        _recordar_peso_empaque(request, pedido)
        messages.error(request, str(exc))
        return destino
    _marcar_paquetes_empacados(pedido, request.user)
    n_paquetes = pedido.paquetes.count()
    if n_paquetes > 1:
        messages.success(
            request,
            f"{pedido.folio} empacado y verificado en {n_paquetes} paquetes. "
            "Cada caja lleva su propia guía.",
        )
    else:
        messages.success(request, f"{pedido.folio} empacado y verificado.")
    return _despachar_y_avisar(request, pedido, destino)


# ─────────────────────────────────────────────────────────────────────────────
# Salida: staging por corral + manifiesto firmado → RECOLECTADO en lote
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("piso", "mesa")
def salida(request):
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "generar_guia":
            return _salida_generar_guia(request)
        if accion == "manifiesto":
            return _salida_manifiesto(request)
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:salida")

    orden_corrales = corrales_activos()
    mapa = _mapa_corrales()
    grupos = {
        codigo: {"codigo": codigo, "nombre": nombre, "sin_guia": [], "listos": []}
        for codigo, nombre in orden_corrales
    }

    def _grupo(carrier):
        corral = _corral_de_carrier(carrier, mapa)
        if corral not in grupos:  # corral sin ubicación viva (p. ej. SAL-LOCAL viejo)
            grupos[corral] = {"codigo": corral, "nombre": corral, "sin_guia": [], "listos": []}
            orden_corrales.append((corral, corral))
        return grupos[corral]

    for pedido in Pedido.objects.filter(estado=Pedido.EMPACADO).select_related("cliente"):
        _grupo(_carrier_probable(pedido))["sin_guia"].append(pedido)
    for pedido in Pedido.objects.filter(estado=Pedido.GUIA_GENERADA).select_related("cliente"):
        # Todas las guías vigentes: multi-paquete = una etiqueta por guía.
        pedido.guias_activas = list(
            pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).order_by("id")
        )
        guia = pedido.guias_activas[-1] if pedido.guias_activas else None
        pedido.guia = guia
        # Sin foto de cierre el manifiesto lo deja fuera: se avisa desde antes.
        pedido.cierre_ok = pedido.cajas_cerradas_completas
        carrier = guia.carrier if guia else _carrier_probable(pedido)
        _grupo(carrier)["listos"].append(pedido)
    for grupo in grupos.values():
        grupo["firman"] = sum(1 for p in grupo["listos"] if p.cierre_ok)
        grupo["sin_cierre"] = [p for p in grupo["listos"] if not p.cierre_ok]
        # El manifiesto se firma POR CARRIER, no por corral: SAL-OTRO junta
        # estafeta+puntopost+fedex y cada chofer se lleva SOLO lo suyo — firmar
        # el corral entero mandaría "va en camino" a compradores cuyo paquete
        # sigue en el piso.
        por_carrier = {}
        for p in grupo["listos"]:
            clave = (p.guia.carrier if p.guia else _carrier_probable(p)) or "?"
            por_carrier.setdefault(clave, []).append(p)
        grupo["carriers"] = [
            {
                "carrier": clave,
                "listos": pedidos,
                "firman": sum(1 for p in pedidos if p.cierre_ok),
                "sin_cierre": [p for p in pedidos if not p.cierre_ok],
            }
            for clave, pedidos in sorted(por_carrier.items())
        ]

    contexto = {
        "seccion": "salida",
        "corrales": [grupos[codigo] for codigo, _ in orden_corrales],
        "corral_local": CORRAL_LOCAL,
        "flota_propia": _flota_propia(),
    }
    return render(request, "piso/salida.html", contexto)


def _salida_generar_guia(request):
    """Genera la guía que falta para un pedido empacado (vía pedidos → envios)."""
    pedido = get_object_or_404(Pedido, pk=request.POST.get("pedido_id"))
    from apps.pedidos.services import generar_guia  # lazy por contrato
    try:
        guia = generar_guia(pedido)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("piso:salida")
    except Exception as exc:  # ErrorCarrier u otra falla del adapter: el piso debe saberlo
        messages.error(
            request,
            f"El carrier no respondió al generar la guía de {pedido.folio}: {exc}. "
            "Reintenta en un momento o avisa a Mesa de Control.",
        )
        return redirect("piso:salida")
    activas = [g for g in pedido.guias.all() if g.es_activa]
    if len(activas) > 1:
        numeros = ", ".join(g.numero for g in activas)
        messages.success(
            request,
            f"{len(activas)} guías listas para {pedido.folio} ({numeros}). "
            "Imprime una etiqueta por caja: cada paquete viaja con la suya.",
        )
    else:
        messages.success(
            request,
            f"Guía {guia.numero} lista para {pedido.folio}. Imprime la etiqueta y pégala en la caja.",
        )
    return redirect("piso:salida")


def _salida_manifiesto(request):
    """Manifiesto firmado: marca RECOLECTADO lo palomeado de UN carrier.

    Aquí — y solo aquí — se dispara el "va en camino" al comprador
    (lo hace pedidos.services.marcar_recolectado, plantilla B).

    Por carrier y con selección: SAL-OTRO junta varios carriers y cada chofer
    firma SOLO por lo que sube a SU camión; lo no palomeado (camión lleno,
    caja con detalle) se queda en el corral para la siguiente recolección.
    """
    corral = (request.POST.get("corral") or "").strip()
    conocidos = {codigo for codigo, _ in corrales_activos()} | {CORRAL_LOCAL, CORRAL_OTRO}
    if corral not in conocidos:
        messages.error(request, "Corral desconocido. Usa los botones de la pantalla.")
        return redirect("piso:salida")
    carrier = (request.POST.get("carrier") or "").strip()
    if not carrier:
        messages.error(request, "Falta el carrier del manifiesto. Usa los botones de la pantalla.")
        return redirect("piso:salida")
    seleccion = {v for v in request.POST.getlist("pedido_id") if v.isdigit()}
    if not seleccion:
        messages.error(
            request,
            "No palomeaste ningún pedido: marca lo que el chofer se lleva y vuelve a firmar.",
        )
        return redirect("piso:salida")

    listos, sin_cierre = [], []
    mapa = _mapa_corrales()
    for pedido in Pedido.objects.filter(
        estado=Pedido.GUIA_GENERADA, pk__in=seleccion,
    ).select_related("cliente"):
        guia = _guia_activa(pedido)
        carrier_pedido = guia.carrier if guia else _carrier_probable(pedido)
        # Un pedido de otro carrier u otro corral no sube a ESTE manifiesto
        # aunque venga palomeado (formulario viejo, doble submit, manipulación).
        if _corral_de_carrier(carrier_pedido, mapa) != corral or carrier_pedido != carrier:
            continue
        # Sin evidencia de cierre (foto de la caja cerrada con su etiqueta
        # pegada) el pedido NO sube al manifiesto: se queda y se avisa.
        if not pedido.cajas_cerradas_completas:
            sin_cierre.append(pedido)
            continue
        listos.append(pedido)
    for pedido in sin_cierre:
        messages.warning(
            request,
            f"{pedido.folio} se queda: falta foto de caja cerrada con la etiqueta pegada.",
        )
    if not listos:
        if not sin_cierre:
            messages.error(
                request,
                f"Nada de {carrier} listo en {corral} entre lo palomeado. Revisa la lista.",
            )
        return redirect("piso:salida")

    from apps.pedidos.services import marcar_recolectado  # lazy por contrato
    recolectados, errores = [], []
    for pedido in listos:
        try:
            marcar_recolectado(pedido, request.user)
            recolectados.append(pedido.folio)
        except ValueError as exc:
            errores.append(f"{pedido.folio}: {exc}")

    if recolectados:
        registrar_evento(
            "manifiesto", corral, "manifiesto_firmado", actor=request.user,
            delta={"corral": corral, "carrier": carrier, "pedidos": recolectados},
            motivo=f"Manifiesto de {carrier} firmado por el chofer: RECOLECTADO en lote.",
        )
        messages.success(
            request,
            f"Manifiesto de {carrier} en {corral} firmado: {len(recolectados)} pedido(s) "
            "recolectado(s). Ahora sí, el comprador recibe su \"va en camino\".",
        )
    for error in errores:
        messages.error(request, error)
    return redirect("piso:salida")


# ─────────────────────────────────────────────────────────────────────────────
# Etiqueta de envío imprimible (10×15) con QR a la página del repartidor
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("piso", "mesa")
def etiqueta(request, guia_pk):
    """Etiqueta imprimible de una guía: datos de la paquetería + QR que lleva
    a la página pública del repartidor (/r/e/<token>/: ubicación exacta +
    botón de auxilio). Renderizar crea el token de la guía si no existe;
    la lógica de maps vive en la página pública (apps.rastreo).

    POST accion=imprimir → manda la etiqueta directo a la térmica de bodega
    (lp desde el servidor, apps.piso.etiquetas.imprimir_etiqueta)."""
    guia = get_object_or_404(
        Guia.objects.select_related("pedido__cliente", "paquete"), pk=guia_pk
    )
    if request.method == "POST":
        return _etiqueta_imprimir(request, guia)
    pedido = guia.pedido
    direccion = pedido.direccion or {}

    # "Paquete N de M" — guías legacy sin paquete amparan el pedido completo.
    if guia.paquete is not None:
        paquete_n = guia.paquete.numero
        paquete_m = pedido.paquetes.count() or 1
    else:
        paquete_n = paquete_m = 1

    # Peso del bulto: el del paquete; legacy → peso real (o esperado) del pedido.
    if guia.paquete is not None:
        peso_kg = guia.paquete.peso_kg
    else:
        gramos = pedido.peso_real_gr or pedido.peso_esperado_gr
        peso_kg = (Decimal(gramos) / 1000).quantize(Decimal("0.01")) if gramos else None

    from apps.rastreo.services import referencias_direccion, url_publica_etiqueta  # lazy por contrato

    branding = pedido.cliente.branding or {}
    contexto = {
        "guia": guia,
        "pedido": pedido,
        "direccion": direccion,
        "marca": branding.get("nombre_publico") or pedido.cliente.nombre,
        "paquete_n": paquete_n,
        "paquete_m": paquete_m,
        "cp": pedido.cp or str(direccion.get("zip") or ""),
        "referencias": referencias_direccion(direccion),
        "qr_url": url_publica_etiqueta(guia),
        "peso_kg": peso_kg,
    }
    return render(request, "piso/etiqueta.html", contexto)


def _etiqueta_imprimir(request, guia):
    """Botón 🖨: manda la etiqueta a la térmica y regresa (PRG) a donde estaba
    el packer — la propia etiqueta, o salida si el POST trae volver=salida.
    accion=imprimir → la del carrier (default); imprimir_interna → la de Torre."""
    accion = request.POST.get("accion")
    if accion not in ("imprimir", "imprimir_interna"):
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:etiqueta", guia_pk=guia.pk)
    destino = (
        redirect("piso:salida")
        if request.POST.get("volver") == "salida"
        else redirect("piso:etiqueta", guia_pk=guia.pk)
    )
    from .etiquetas import imprimir_etiqueta

    try:
        mensaje = imprimir_etiqueta(guia, interna=(accion == "imprimir_interna"))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, mensaje)
    return destino


# ─────────────────────────────────────────────────────────────────────────────
# Conteos cíclicos del día
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("piso", "mesa")
def conteos(request):
    if request.method == "POST":
        return _conteo_registrar(request)

    hoy = timezone.localdate()
    tareas = list(
        TareaConteo.objects.filter(fecha=hoy)
        .select_related("sku", "sku__cliente", "conteo")
    )
    pendientes = [t for t in tareas if t.estado == TareaConteo.PENDIENTE]
    completadas = [t for t in tareas if t.estado == TareaConteo.COMPLETADA]
    for tarea in pendientes:
        # DÓNDE contar (sin cantidades — el conteo sigue ciego): las
        # ubicaciones con saldo físico del SKU en anaquel.
        tarea.ubicaciones = list(
            Saldo.objects.filter(
                sku=tarea.sku, cantidad__gt=0,
                estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO],
            )
            .order_by("ubicacion__codigo")
            .values_list("ubicacion__codigo", flat=True)
            .distinct()
        )
    for tarea in completadas:
        tarea.incidencia = _incidencia_descuadre(tarea.conteo) if tarea.conteo else None
    contexto = {
        "seccion": "conteos",
        "hoy": hoy,
        "pendientes": pendientes,
        "completadas": completadas,
    }
    return render(request, "piso/conteos.html", contexto)


def _conteo_registrar(request):
    """Registra el conteo ciego de una tarea del día y reporta la discrepancia."""
    destino = redirect("piso:conteos")
    tarea = get_object_or_404(TareaConteo.objects.select_related("sku"), pk=request.POST.get("tarea_id"))
    if tarea.estado == TareaConteo.COMPLETADA:
        messages.error(request, f"La tarea de {tarea.sku.codigo} ya se contó hoy.")
        return destino
    try:
        contado = _entero(
            request.POST.get("contado"),
            "Captura cuántas piezas contaste, en número entero (0 también cuenta).",
        )
        from apps.inventario.services import registrar_conteo  # lazy por contrato
        conteo = registrar_conteo(tarea.sku, contado, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino

    diferencia = conteo.diferencia
    incidencia = _incidencia_descuadre(conteo)
    if diferencia == 0:
        messages.success(
            request,
            f"{conteo.folio}: {tarea.sku.codigo} cuadra perfecto ({conteo.contado} piezas). Buen trabajo.",
        )
    elif incidencia is not None:
        messages.error(
            request,
            f"{conteo.folio}: {tarea.sku.codigo} con diferencia de {diferencia:+d} piezas "
            f"(el sistema esperaba {conteo.esperado}). Se abrió el folio {incidencia.folio} "
            "por descuadre: no ajustes nada sin doble firma.",
        )
    else:
        messages.warning(
            request,
            f"{conteo.folio}: {tarea.sku.codigo} con diferencia de {diferencia:+d} piezas "
            f"(el sistema esperaba {conteo.esperado}). Quedó registrado; Mesa lo revisa.",
        )
    return destino


def _incidencia_descuadre(conteo):
    """Folio DES abierto por este conteo, si el descuadre excedió umbral (lazy)."""
    if conteo is None:
        return None
    try:
        from apps.incidencias.models import Incidencia  # lazy: puede no existir aún
    except ImportError:
        return None
    return (
        Incidencia.objects
        .filter(sku=conteo.sku, tipo="DES", ts_apertura__gte=conteo.ts - timedelta(minutes=1))
        .order_by("-ts_apertura")
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cuarentena: dictamen con doble firma (revendible o merma)
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("piso", "mesa")
def cuarentena(request):
    """Lo dañado y retornado en revisión. Nada sale sin dictamen con dos firmas.

    Abajo, el put-away pendiente: dictámenes revendibles y retornos que
    esperan anaquel — ubícalos aquí para que vuelvan a estar vendibles.
    """
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "dictamen":
            return _cuarentena_dictamen(request)
        if accion == "ubicar_putaway":
            return _cuarentena_ubicar(request)
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:cuarentena")

    saldos = list(
        Saldo.objects.filter(estado=Saldo.CUARENTENA, cantidad__gt=0)
        .select_related("sku", "sku__cliente", "lote", "ubicacion")
        .order_by("sku__cliente__nombre", "sku__codigo", "ubicacion__codigo")
    )
    putaway = list(
        Saldo.objects.filter(estado=Saldo.EN_PUTAWAY, cantidad__gt=0)
        .select_related("sku", "sku__cliente", "lote", "ubicacion")
        .order_by("sku__cliente__nombre", "sku__codigo", "ubicacion__codigo")
    )
    from apps.core.models import PerfilUsuario  # lazy: modelo de otra app
    firmantes = list(
        PerfilUsuario.objects.filter(
            rol__in=[PerfilUsuario.ROL_PISO, PerfilUsuario.ROL_MESA],
            usuario__is_active=True,
        )
        .exclude(pin="")
        .select_related("usuario")
        .order_by("usuario__username")
    )
    contexto = {
        "seccion": "cuarentena",
        "saldos": saldos,
        "total_piezas": sum(s.cantidad for s in saldos),
        "putaway": putaway,
        "total_putaway": sum(s.cantidad for s in putaway),
        "firmantes": firmantes,
        "ubicaciones_destino": Ubicacion.objects.filter(
            tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA], activo=True
        ),
    }
    return render(request, "piso/cuarentena.html", contexto)


def _cuarentena_dictamen(request):
    """Dictamen de una fila de cuarentena: doble firma → revendible o merma."""
    destino_redirect = redirect("piso:cuarentena")
    saldo = get_object_or_404(
        Saldo.objects.select_related("sku", "lote", "ubicacion"),
        pk=request.POST.get("saldo_id"), estado=Saldo.CUARENTENA,
    )
    destino = (request.POST.get("destino") or "").strip()
    from apps.inventario.services import dictaminar_cuarentena  # lazy por contrato
    try:
        cantidad = _entero(
            request.POST.get("cantidad"),
            "Captura cuántas piezas dictaminas, en número entero.",
        )
        dictaminar_cuarentena(
            saldo.sku, cantidad, destino,
            (request.POST.get("autorizo_1") or "").strip(), request.POST.get("pin_1") or "",
            (request.POST.get("autorizo_2") or "").strip(), request.POST.get("pin_2") or "",
            request.user,
            lote=saldo.lote,
            motivo_texto=(request.POST.get("motivo_texto") or "").strip(),
            ubicacion=saldo.ubicacion,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino_redirect
    if destino == "revendible":
        messages.success(
            request,
            f"{cantidad} piezas regresan a put-away — ubícalas para que vuelvan a estar vendibles.",
        )
    else:
        messages.success(request, f"{cantidad} piezas dadas de baja como merma. Quedó en el kardex.")
    return destino_redirect


def _cuarentena_ubicar(request):
    """Put-away suelto desde cuarentena: dictámenes revendibles y retornos.

    Espeja _recepcion_ubicar (mismos mensajes): ubicación destino escaneada
    (el servicio valida picking/reserva) y lote capturado SOLO cuando el SKU
    lo pide y la fila no lo trae; si la fila ya trae lote, se usa directo.
    """
    destino = redirect("piso:cuarentena")
    saldo = get_object_or_404(
        Saldo.objects.select_related("sku", "lote"),
        pk=request.POST.get("saldo_id"), estado=Saldo.EN_PUTAWAY,
    )
    sku = saldo.sku
    try:
        cantidad = _entero(request.POST.get("cantidad"), "Captura cuántas piezas vas a ubicar.")
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino

    codigo_ubicacion = (request.POST.get("ubicacion") or "").strip()
    if not codigo_ubicacion:
        messages.error(request, "Escanea la etiqueta del anaquel destino antes de ubicar.")
        return destino
    ubicacion = Ubicacion.objects.filter(codigo__iexact=codigo_ubicacion).first()
    if ubicacion is None:
        messages.error(
            request,
            f"No existe la ubicación {codigo_ubicacion}. Escanea la etiqueta del anaquel, no la del producto.",
        )
        return destino

    lote = saldo.lote  # la fila con lote lo conserva: no se vuelve a pedir
    if lote is None:
        lote_codigo = (request.POST.get("lote") or "").strip()
        if lote_codigo:
            fecha_caducidad = None
            crudo = (request.POST.get("fecha_caducidad") or "").strip()
            if crudo:
                try:
                    fecha_caducidad = date.fromisoformat(crudo)
                except ValueError:
                    messages.error(request, "La fecha de caducidad no es válida. Usa el calendario.")
                    return destino
            lote, _ = Lote.objects.get_or_create(
                sku=sku, codigo=lote_codigo, defaults={"fecha_caducidad": fecha_caducidad}
            )

    from apps.inventario.services import ubicar  # lazy por contrato
    try:
        ubicar(sku, cantidad, ubicacion, lote, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request,
        f"{cantidad} × {sku.codigo} ubicadas en {ubicacion.codigo}: ya cuentan como vendibles.",
    )
    return destino


# ─────────────────────────────────────────────────────────────────────────────
# Entrega local (POD): foto + receptor + mayoría de edad — el producto es alcohol
# ─────────────────────────────────────────────────────────────────────────────


def _sin_flota_404(request):
    """404 amable: la flota propia no existe (TORRE["FLOTA_PROPIA"]=False)."""
    return render(request, "piso/sin_flota.html", {"seccion": "salida"}, status=404)


@rol_requerido("piso", "mesa")
def entrega_local(request):
    if not _flota_propia():
        return _sin_flota_404(request)
    hoy = timezone.localdate()
    en_reparto = list(
        Pedido.objects.filter(es_local=True, estado__in=[Pedido.RECOLECTADO, Pedido.EN_TRANSITO])
        .select_related("cliente")
    )
    por_salir = list(
        Pedido.objects.filter(es_local=True, estado__in=[Pedido.EMPACADO, Pedido.GUIA_GENERADA])
        .select_related("cliente")
    )
    entregados_hoy = list(
        Pedido.objects.filter(es_local=True, estado=Pedido.ENTREGADO, ts_entregado__date=hoy)
        .select_related("cliente")
    )
    contexto = {
        "seccion": "salida",
        "en_reparto": en_reparto,
        "por_salir": por_salir,
        "entregados_hoy": entregados_hoy,
    }
    return render(request, "piso/entrega_local.html", contexto)


@rol_requerido("piso", "mesa")
def entrega_local_pedido(request, pk):
    if not _flota_propia():
        return _sin_flota_404(request)
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=pk)
    if not pedido.es_local:
        messages.error(request, f"El pedido {pedido.folio} no es de entrega local.")
        return redirect("piso:entrega_local")
    if pedido.estado not in (Pedido.RECOLECTADO, Pedido.EN_TRANSITO):
        messages.error(
            request,
            f"El pedido {pedido.folio} no está en reparto (está {pedido.get_estado_display()}).",
        )
        return redirect("piso:entrega_local")

    if request.method == "POST":
        return _registrar_pod(request, pedido)

    contexto = {"seccion": "salida", "pedido": pedido}
    return render(request, "piso/entrega_local_pedido.html", contexto)


def _registrar_pod(request, pedido):
    """POD de entrega local: foto + nombre del receptor + mayoría de edad verificada."""
    destino = redirect("piso:entrega_local_pedido", pk=pedido.pk)
    receptor = (request.POST.get("receptor") or "").strip()
    foto = request.FILES.get("foto_pod")
    mayoria = request.POST.get("mayoria_edad") == "si"

    faltas = []
    if not foto:
        faltas.append("la foto de entrega")
    if not receptor:
        faltas.append("el nombre de quien recibe")
    if not mayoria:
        faltas.append("la verificación de mayoría de edad")
    if faltas:
        messages.error(
            request,
            "No puedes cerrar la entrega sin " + ", ".join(faltas) + ". "
            "El producto es alcohol: sin verificación de edad NO se entrega.",
        )
        return destino

    evidencia = EvidenciaFoto.objects.create(
        entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod",
        archivo=foto, tomada_por=request.user.username,
    )
    registrar_evento(
        "pedido", pedido.pk, "pod_entrega_local", actor=request.user, cliente=pedido.cliente,
        delta={
            "receptor": receptor,
            "mayoria_edad_verificada": True,
            "evidencia_id": evidencia.pk,
        },
        motivo=f"Entrega local: recibió {receptor}; mayoría de edad verificada en persona.",
    )
    try:
        pedido.transicionar(
            Pedido.ENTREGADO, actor=request.user,
            motivo=f"POD de entrega local: recibió {receptor}, mayoría de edad verificada.",
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("piso:entrega_local")

    guia = _guia_activa(pedido)
    if guia is not None and guia.estado not in Guia.ESTADOS_TERMINALES:
        try:
            guia.transicionar(
                Guia.ENTREGADO, actor=request.user, motivo=f"POD local: recibió {receptor}"
            )
        except ValueError:
            pass  # el pedido ya quedó entregado; la guía la concilia el poller
    messages.success(
        request,
        f"{pedido.folio} entregado a {receptor}. POD guardado con foto y verificación de edad.",
    )
    return redirect("piso:entrega_local")
