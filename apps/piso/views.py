"""Vistas del operador de piso (tablet) — contrato CONVENTIONS.md §piso.

Flujo lineal y táctil: recibir → ubicar → pickear → empacar → salida →
entrega local, más los conteos cíclicos del día. Cada acción llama a los
servicios de dominio (inventario / pedidos / envios, imports lazy por
contrato) — el piso jamás toca Saldo, Movimiento ni estados directo.
"""
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU, Ubicacion
from apps.core.decorators import rol_requerido
from apps.core.models import EvidenciaFoto
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


def _es_mesa(request):
    return getattr(request, "rol", None) == "mesa" or request.user.is_superuser


def _pedido_libre_o_mio(pedido, user):
    """Visible/trabajable en piso: sin dueño, mío, o ya liberado (cierre completo)."""
    if pedido.asignado_a_id in (None, user.pk):
        return True
    return pedido.cajas_cerradas_completas


def _acceso_pedido(request, pedido):
    return _es_mesa(request) or _pedido_libre_o_mio(pedido, request.user)


def _reclamar_si_libre(pedido, request):
    """Trabajar un pedido libre = volverse su dueño (adopción implícita)."""
    if pedido.asignado_a_id is None and not _es_mesa(request):
        pedido.asignado_a = request.user
        pedido.save(update_fields=["asignado_a", "actualizado"])


def _operadores_piso(request):
    from django.contrib.auth.models import User
    return list(
        User.objects.filter(perfil__rol="piso", is_active=True)
        .exclude(pk=request.user.pk).order_by("username")
    )


def _pedido_soltar(request, pedido):
    """Soltar el pedido en picking: queda libre con su avance para que lo tome
    cualquier operador; el que lo soltó regresa a la lista a tomar otro."""
    from apps.pedidos.services import soltar_pedido  # lazy por contrato

    motivo = (request.POST.get("motivo") or "").strip()[:120]
    try:
        soltar_pedido(pedido, request.user, motivo=motivo)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("piso:picking_pedido", pk=pedido.pk)
    messages.success(
        request,
        f"{pedido.folio} liberado con su avance: lo puede tomar cualquier operador desde 'En picking'.",
    )
    return redirect("piso:picking")


def _pedido_transferir(request, pedido):
    from django.contrib.auth.models import User

    from apps.pedidos.services import transferir_pedido
    destino = User.objects.filter(
        pk=request.POST.get("operador_id"), perfil__rol="piso", is_active=True,
    ).first()
    if destino is None:
        messages.error(request, "Elige a qué operador se lo mandas.")
        return
    try:
        transferir_pedido(pedido, request.user, destino)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{pedido.folio} enviado a {destino.username} — sigue siendo tuyo hasta que acepte.",
        )


def _pendientes_por_prioridad():
    """PENDIENTES en orden de cola: primero los que rebasan el corte de hoy
    (entraron antes del corte → salen HOY), luego por creado ascendente."""
    corte = _corte_hoy()
    return sorted(
        Pedido.objects.filter(estado=Pedido.PENDIENTE)
        .select_related("cliente").prefetch_related("lineas"),
        key=lambda p: (p.creado > corte, p.creado),
    )


def _abierto_mas_viejo(user):
    """El EN_PICKING más viejo MÍO o libre (para reanudar), o None."""
    from django.db.models import Q
    en_picking = sorted(
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .filter(Q(asignado_a__isnull=True) | Q(asignado_a=user))
        .select_related("cliente").prefetch_related("lineas"),
        key=lambda p: p.ts_picking or p.creado,
    )
    return en_picking[0] if en_picking else None


def _siguiente_en_cola(user):
    """El pedido de LA card según prioridad del SERVIDOR (el empleado no decide).

    1º EN_PICKING ya empezados (terminar lo abierto, el más viejo primero);
    2º PENDIENTE por prioridad de cola.
    """
    abierto = _abierto_mas_viejo(user)
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
        if abierto is not None and _acceso_pedido(request, abierto):
            return _destino_pedido(abierto)

    for _ in range(8):
        pendientes = _pendientes_por_prioridad()
        if not pendientes:
            abierto = _abierto_mas_viejo(request.user)
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
        if accion in ("aceptar_transferencia", "rechazar_transferencia"):
            from apps.pedidos.services import aceptar_transferencia, rechazar_transferencia
            pedido = get_object_or_404(Pedido, pk=request.POST.get("pedido_id"))
            try:
                if accion == "aceptar_transferencia":
                    aceptar_transferencia(pedido, request.user)
                    messages.success(request, f"{pedido.folio} ahora es tuyo — continúalo desde su pantalla.")
                else:
                    rechazar_transferencia(pedido, request.user)
                    messages.success(request, f"Transferencia de {pedido.folio} rechazada.")
            except ValueError as exc:
                messages.error(request, str(exc))
            return redirect("piso:home")
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
    if not _es_mesa(request):
        # Los pedidos con dueño desaparecen para los demás operadores.
        en_picking = [p for p in en_picking if _pedido_libre_o_mio(p, request.user)]
    por_pickear = [p for p in en_picking if not p.lineas_completas]
    por_empacar = [p for p in en_picking if p.lineas_completas]
    # Etapa de empaque después del picking: nada llega al corral sin guía y
    # foto de cierre (Pedido.empaque_completo). Lo incompleto sigue en la
    # mesa, a nombre de quien lo tiene: "Completar empaquetado".
    en_empaque = list(
        Pedido.objects.filter(
            estado__in=[Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO],
        ).select_related("cliente", "asignado_a").prefetch_related("paquetes__guias", "guias")
    )
    staging = [p for p in en_empaque if p.estado != Pedido.EMPACADO and p.empaque_completo]
    incompletos = [p for p in en_empaque if not p.empaque_completo]
    if not _es_mesa(request):
        incompletos = [p for p in incompletos if _pedido_libre_o_mio(p, request.user)]
    por_completar = por_empacar + incompletos
    for pedido in por_completar:
        pedido.falta = _que_falta_empaque(pedido)
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

    transferencias = list(
        Pedido.objects.filter(transferencia_a=request.user)
        .select_related("cliente", "asignado_a")
    )

    siguiente = _siguiente_en_cola(request.user)
    if siguiente is not None:
        siguiente.total_piezas = _piezas(siguiente)
        siguiente.num_lineas = siguiente.lineas.count()
        siguiente.ya_empezado = siguiente.estado == Pedido.EN_PICKING

    total_picking = len(pendientes) + len(por_pickear)
    corte_hora, corte_texto, corte_tono = _reloj_corte()
    todo_al_dia = (
        siguiente is None and not recepciones_abiertas and not conteos_pendientes
        and not restocks and not staging and not por_completar
    )

    contexto = {
        "seccion": "home",
        "es_mesa": _es_mesa(request),
        "todo_al_dia": todo_al_dia,
        "siguiente": siguiente,
        "transferencias": transferencias,
        "recepciones_abiertas": recepciones_abiertas,
        "num_por_pickear": total_picking,
        "por_empacar": por_empacar,
        "por_completar": por_completar,
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
        .select_related("cliente", "pedido").prefetch_related("lineas__sku")
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
    orden = get_object_or_404(OrdenEntrada.objects.select_related("cliente", "pedido"), pk=pk)

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "escanear":
            return _recepcion_escanear(request, orden)
        if accion == "foto":
            return _recepcion_foto(request, orden)
        if accion == "recibir":
            return _recepcion_recibir(request, orden)
        if accion == "ubicar":
            return _recepcion_ubicar(request, orden)
        if accion == "cerrar":
            return _recepcion_cerrar(request, orden)
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:recepcion_detalle", pk=orden.pk)

    # Plan de acomodo de toda la orden (una vez; Mesa lo rehace si hace falta).
    if orden.estado != OrdenEntrada.CERRADA and not (orden.plan_acomodo or {}).get("pasos"):
        from apps.inventario.services import planear_acomodo  # lazy por contrato
        planear_acomodo(orden, request.user)

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
    fotos_llegada = EvidenciaFoto.objects.filter(entidad="asn", entidad_id=orden.folio)
    contexto = {
        "seccion": "recepcion",
        "orden": orden,
        "lineas": lineas,
        "pendiente_putaway": sum(putaway.values()),
        "tiene_foto": fotos_llegada.filter(tipo="llegada").exists(),
        "sla_texto": sla_texto,
        "sla_tono": sla_tono,
        "ubicaciones_destino": Ubicacion.objects.filter(
            tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA], activo=True
        ),
        "fotos_llegada": fotos_llegada,
    }
    return render(request, "piso/recepcion_detalle.html", contexto)


def _recepcion_foto(request, orden):
    """Foto de llegada (camión/tarimas) ligada al ASN: obligatoria antes del primer escaneo (SOP RE-01)."""
    destino = redirect("piso:recepcion_detalle", pk=orden.pk)
    foto = request.FILES.get("foto_llegada")
    if foto is None:
        messages.error(request, "Tómale foto al camión/tarimas antes de empezar a escanear.")
        return destino
    EvidenciaFoto.objects.create(
        entidad="asn", entidad_id=orden.folio, tipo="llegada", archivo=foto, tomada_por=request.user.username,
    )
    messages.success(request, "Foto de llegada guardada. Ya puedes escanear.")
    return destino


def _linea_por_codigo(orden, valor):
    """Línea de la orden cuyo SKU tiene ese código de barras o código; None si no es de la orden."""
    valor = (valor or "").strip()
    if not valor:
        return None
    for linea in orden.lineas.select_related("sku"):
        if valor in (linea.sku.codigo_barras, linea.sku.codigo) or valor.upper() == linea.sku.codigo.upper():
            return linea
    return None


def _recepcion_escanear(request, orden):
    """Un escaneo identifica el producto y abre su cuenta por lote (Contar);
    ya no suma piezas por sí solo: el "1" automático quedaba aislado y contaba
    de más (Chema, 2026-09-21). Exige la foto de llegada (SOP RE-01)."""
    destino = redirect("piso:recepcion_detalle", pk=orden.pk)
    linea = _linea_por_codigo(orden, request.POST.get("codigo"))
    if linea is None:
        messages.error(request, "Ese código no es de un producto de esta orden. Revisa la etiqueta.")
        return destino
    if not EvidenciaFoto.objects.filter(entidad="asn", entidad_id=orden.folio, tipo="llegada").exists():
        messages.error(request, "Tómale foto al camión/tarimas antes de registrar la primera línea.")
        return destino
    return redirect(f"{reverse('piso:recepcion_contar', args=[orden.pk])}?sku={linea.sku_id}")


@rol_requerido("piso", "mesa")
def recepcion_contar(request, pk):
    """Cuenta de UN producto de la orden, por lote: un radio por línea (lote
    anunciado, sin cantidades anunciadas: conteo ciego), cuántas cuentas
    ahora (acepta cero) y cuántas dañadas; al confirmar se recibe en esa
    línea y se pasa a Ubicar con el lote ya elegido. Cero = solo volver a
    Ubicar lo ya contado (reescanear para acomodar)."""
    orden = get_object_or_404(OrdenEntrada.objects.select_related("cliente"), pk=pk)
    volver = redirect("piso:recepcion_detalle", pk=orden.pk)
    if orden.estado == OrdenEntrada.CERRADA:
        messages.error(request, f"La orden {orden.folio} ya está cerrada; no acepta más conteos.")
        return volver
    sku_id = request.POST.get("sku_id") or request.GET.get("sku")
    lineas = [l for l in orden.lineas.select_related("sku") if str(l.sku_id) == str(sku_id)]
    if not lineas:
        messages.error(request, "Escanea un producto de la orden para contarlo.")
        return volver
    sku = lineas[0].sku
    if request.method == "POST":
        return _recepcion_contar_registrar(request, orden, sku, lineas)
    from apps.inventario.services import _suma  # lazy por contrato
    return render(request, "piso/recepcion_contar.html", {
        "seccion": "recepcion", "orden": orden, "sku": sku, "lineas": lineas,
        "por_ubicar": _suma(sku, Saldo.EN_PUTAWAY),
        "linea_unica": lineas[0] if len(lineas) == 1 else None,
    })


def _recepcion_contar_registrar(request, orden, sku, lineas):
    """POST de Contar: recibe lo contado (buenas y dañadas) en la línea del
    lote elegido y manda a Ubicar con ese lote. Nada contado = directo a Ubicar."""
    from apps.inventario.services import recibir  # lazy por contrato
    reintentar = redirect(f"{reverse('piso:recepcion_contar', args=[orden.pk])}?sku={sku.pk}")
    linea = next((l for l in lineas if str(l.pk) == str(request.POST.get("linea_id") or "")), None)
    if linea is None:
        messages.error(request, "Elige el lote que estás contando.")
        return reintentar
    try:
        cantidad = _entero(
            request.POST.get("cantidad") or 0,
            "Captura cuántas cuentas, en número entero (cero si solo vas a ubicar).",
        )
        danadas = _entero(request.POST.get("danadas") or 0, "Captura las dañadas en número entero.")
        if cantidad < 0 or danadas < 0:
            raise ValueError("Las cantidades no pueden ser negativas.")
        if (cantidad or danadas) and not EvidenciaFoto.objects.filter(
            entidad="asn", entidad_id=orden.folio, tipo="llegada",
        ).exists():
            raise ValueError("Tómale foto al camión/tarimas antes de registrar la primera línea.")
        if cantidad or danadas:
            recibir(linea, cantidad, danadas, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return reintentar
    partes = []
    if cantidad:
        partes.append(f"{cantidad} contada{'s' if cantidad != 1 else ''}")
    if danadas:
        partes.append(f"{danadas} dañada{'s' if danadas != 1 else ''} a cuarentena")
    lote = (linea.lote_codigo or "").strip()
    if partes:
        de_lote = f" (lote {lote})" if lote else ""
        messages.success(request, f"{sku.codigo}: {' y '.join(partes)}{de_lote}. Ahora ubícalas.")
    destino = f"{reverse('piso:recepcion_ubicar', args=[orden.pk])}?sku={sku.pk}"
    if lote:
        destino += f"&lote={quote(lote)}"
        if linea.fecha_caducidad:
            destino += f"&fecha_caducidad={linea.fecha_caducidad.isoformat()}"
    return redirect(destino)


@rol_requerido("piso", "mesa")
def recepcion_ubicar(request, pk):
    """Pantalla de ubicar piezas recibidas (una recién escaneada o varias de
    una vez): el anaquel que le toca según el plan de la orden (o cuarentena
    si no hay espacio), cuántas van ahí y qué más vive en ese anaquel, el lote
    si hace falta elegirlo o capturarlo, y las salidas: ubicadas, otro
    anaquel, o llega dañada."""
    from apps.inventario.services import _suma, planear_acomodo, siguiente_paso  # lazy por contrato

    orden = get_object_or_404(OrdenEntrada.objects.select_related("cliente"), pk=pk)
    volver = redirect("piso:recepcion_detalle", pk=orden.pk)
    sku_id = request.POST.get("sku_id") or request.GET.get("sku")
    lineas = [l for l in orden.lineas.select_related("sku") if str(l.sku_id) == str(sku_id)]
    if not lineas:
        messages.error(request, "Escanea un producto de la orden para ubicarlo.")
        return volver
    sku = lineas[0].sku
    if request.method == "POST":
        return _recepcion_ubicar_pieza(request, orden, sku, lineas)

    por_ubicar = _suma(sku, Saldo.EN_PUTAWAY)
    if por_ubicar <= 0:
        messages.info(request, f"No hay piezas de {sku.codigo} en recepción por ubicar.")
        return volver
    # 1) El lote va ANTES del anaquel: fijo si la orden anuncia uno, a elegir si
    #    anuncia varios, a capturar si el producto lo pide; el plan es por lote.
    lotes = []
    for l in lineas:
        codigo = (l.lote_codigo or "").strip()
        if codigo and codigo not in [x["codigo"] for x in lotes]:
            lotes.append({"codigo": codigo, "caducidad": l.fecha_caducidad.isoformat() if l.fecha_caducidad else ""})
    if len(lotes) == 1:
        modo_lote = "fijo"
    elif len(lotes) > 1:
        modo_lote = "elegir"
    elif sku.requiere_lote:
        modo_lote = "capturar"
    else:
        modo_lote = "ninguno"
    lote_elegido = (request.GET.get("lote") or "").strip()
    caducidad_elegida = (request.GET.get("fecha_caducidad") or "").strip()
    if modo_lote == "fijo":
        lote_elegido, caducidad_elegida = lotes[0]["codigo"], lotes[0]["caducidad"]
    elif modo_lote == "elegir" and lote_elegido:
        caducidad_elegida = next((x["caducidad"] for x in lotes if x["codigo"] == lote_elegido), "")
    from apps.catalogo.services import lotes_sugeridos  # lazy por contrato
    contexto = {
        "seccion": "recepcion", "orden": orden, "sku": sku,
        "lotes": lotes, "modo_lote": modo_lote, "lote": lote_elegido, "caducidad": caducidad_elegida,
        "lotes_sugeridos": lotes_sugeridos(sku, orden) if modo_lote == "capturar" else [],
        "por_ubicar": por_ubicar,
        # Prellenado con lo contado sin ubicar: el operador lo baja si acomoda
        # en tandas; el tope es lo contado, no el paso del plan (Chema 2026-09-21).
        "cantidad_default": por_ubicar,
        "pedir_lote": modo_lote in ("elegir", "capturar") and not lote_elegido,
    }
    if contexto["pedir_lote"]:
        return render(request, "piso/recepcion_ubicar.html", contexto)
    # 2) El anaquel del plan para ese SKU y lote; sin paso, se rehace el plan y,
    #    si aun así no hay, se sugiere ad hoc para esta pieza.
    paso = siguiente_paso(orden, sku, lote_elegido)
    if paso is None:
        planear_acomodo(orden, request.user)
        paso = siguiente_paso(orden, sku, lote_elegido)
    if paso is None:
        from apps.inventario.services import sugerir_anaquel  # lazy por contrato
        ad_hoc = sugerir_anaquel(sku, 1, lote=lote_elegido or None)
        if ad_hoc:
            u = ad_hoc[0]["ubicacion"]
            paso = {"ubicacion": u.codigo if u else None, "cantidad": 1, "ubicadas": 0,
                    "motivo": ad_hoc[0]["motivo"] + " (fuera del plan)"}
    # 3) Qué vive ya en ese anaquel (producto, piezas, lotes) y qué tan lleno
    #    está, para acomodar con toda la información.
    anaquel, vecinos, ocupacion_anaquel = None, [], None
    if paso and paso["ubicacion"]:
        from apps.inventario.services import ocupacion  # lazy por contrato
        anaquel = Ubicacion.objects.filter(codigo=paso["ubicacion"]).first()
        if anaquel is not None:
            ocupacion_anaquel = ocupacion(anaquel)
            vecinos = sorted(ocupacion_anaquel["por_sku"], key=lambda f: (f["sku"].pk != sku.pk, f["sku"].codigo))
    contexto.update({
        "paso": paso,
        # Cuántas del SKU van todavía en ese anaquel según el plan: la referencia para no meter de más.
        "faltan_paso": (paso["cantidad"] - paso["ubicadas"]) if paso else 0,
        "anaquel": anaquel, "vecinos": vecinos, "ocupacion_anaquel": ocupacion_anaquel,
        "ubicaciones_destino": Ubicacion.objects.filter(tipo=Ubicacion.PICKING, activo=True).order_by("codigo"),
    })
    return render(request, "piso/recepcion_ubicar.html", contexto)


def _recepcion_ubicar_pieza(request, orden, sku, lineas):
    """POST de la pantalla de ubicar: ubicar N (al anaquel elegido o a
    cuarentena si viene vacío) o marcar la pieza dañada. Ubicar ya no cuenta:
    N no puede pasar de lo contado sin ubicar (la cuenta vive en Contar,
    Chema 2026-09-21); el sobrante del paso del plan sigue al siguiente anaquel."""
    from apps.inventario.services import _suma, marcar_danada, ubicar_pieza  # lazy por contrato

    volver = redirect("piso:recepcion_detalle", pk=orden.pk)
    accion = request.POST.get("accion")
    if accion == "danada":
        linea = next((l for l in lineas if l.cantidad_recibida > 0), lineas[0])
        try:
            marcar_danada(linea, request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return volver
        messages.warning(request, f"{sku.codigo}: pieza marcada dañada, va a cuarentena. Escanea la siguiente.")
        return volver
    codigo_ubicacion = (request.POST.get("ubicacion") or "").strip()
    ubicacion = None
    if codigo_ubicacion:
        ubicacion = Ubicacion.objects.filter(codigo__iexact=codigo_ubicacion).first()
        if ubicacion is None:
            messages.error(request, f"No existe la ubicación {codigo_ubicacion}. Escanea la etiqueta del anaquel, no la del producto.")
            return redirect(f"{reverse('piso:recepcion_ubicar', args=[orden.pk])}?sku={sku.pk}")
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
                return redirect(f"{reverse('piso:recepcion_ubicar', args=[orden.pk])}?sku={sku.pk}")
        from apps.catalogo.services import obtener_o_crear_lote  # lazy por contrato
        lote = obtener_o_crear_lote(sku, lote_codigo, fecha_caducidad)
    try:
        cantidad = _entero(request.POST.get("cantidad") or 1, "Captura cuántas piezas ubicas, en número entero.")
        if cantidad < 1:
            raise ValueError("La cantidad a ubicar es mínimo 1.")
        contadas = _suma(sku, Saldo.EN_PUTAWAY)
        if cantidad > contadas:
            raise ValueError(
                f"Solo tienes {contadas} contada{'s' if contadas != 1 else ''} sin ubicar de {sku.codigo}. "
                "Ubicar ya no cuenta: escanea el producto y captura las que faltan en Contar."
            )
        with transaction.atomic():
            destino = ubicar_pieza(orden, sku, lote, ubicacion, request.user, cantidad)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('piso:recepcion_ubicar', args=[orden.pk])}?sku={sku.pk}")
    piezas = "1 pieza" if cantidad == 1 else f"{cantidad} piezas"
    if destino is None:
        messages.warning(request, f"{sku.codigo}: sin anaquel con espacio, {piezas} a cuarentena. Escanea la siguiente.")
    else:
        messages.success(request, f"{sku.codigo}: {piezas} en {destino.codigo}. Escanea la siguiente.")
    return volver


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
        from apps.catalogo.services import obtener_o_crear_lote  # lazy por contrato
        lote = obtener_o_crear_lote(sku, lote_codigo, fecha_caducidad)

    from apps.inventario.services import aviso_capacidad, ubicar  # lazy por contrato
    aviso = aviso_capacidad(ubicacion, sku, cantidad)  # antes de ubicar: estimado con lo que va a entrar
    try:
        ubicar(sku, cantidad, ubicacion, lote, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request,
        f"{cantidad} × {sku.codigo} ubicadas en {ubicacion.codigo}: ya cuentan como vendibles.",
    )
    if aviso:
        messages.warning(request, aviso)
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

    # Todos los pedidos por surtir, agrupados por cliente, para elegir el que se
    # quiera (Chema 2026-09-21). Los que tiene otro operador se ven pero no se
    # abren: se sabe quién los tiene; si los suelta, aparecen con botón.
    pendientes = list(
        Pedido.objects.filter(estado=Pedido.PENDIENTE)
        .select_related("cliente").prefetch_related("lineas__sku").order_by("creado")
    )
    for pedido in pendientes:
        pedido.total_piezas = _piezas(pedido)
    en_picking = list(
        Pedido.objects.filter(estado=Pedido.EN_PICKING)
        .select_related("cliente", "asignado_a").prefetch_related("lineas__sku").order_by("creado")
    )
    for pedido in en_picking:
        pedido.pickeadas, pedido.total_piezas, pedido.avance_pct = _avance(pedido)
        pedido.puedo_abrir = _es_mesa(request) or _pedido_libre_o_mio(pedido, request.user)
    clientes = {}
    for pedido in pendientes:
        clientes.setdefault(pedido.cliente_id, {"cliente": pedido.cliente, "pendientes": [], "en_picking": []})["pendientes"].append(pedido)
    for pedido in en_picking:
        clientes.setdefault(pedido.cliente_id, {"cliente": pedido.cliente, "pendientes": [], "en_picking": []})["en_picking"].append(pedido)
    contexto = {
        "seccion": "picking", "pendientes": pendientes, "en_picking": en_picking,
        "clientes": sorted(clientes.values(), key=lambda c: c["cliente"].nombre),
    }
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

    if not _acceso_pedido(request, pedido):
        duenio = pedido.asignado_a.username if pedido.asignado_a else "otro operador"
        if _quiere_json(request):
            return JsonResponse({"ok": False, "error": f"Lo tiene {duenio}."}, status=403)
        messages.error(
            request,
            f"{pedido.folio} lo tiene {duenio}: pídele que te lo envíe desde su pantalla.",
        )
        return redirect("piso:picking")

    if request.method == "POST":
        if request.POST.get("accion") == "transferir":
            _pedido_transferir(request, pedido)
            return redirect("piso:picking_pedido", pk=pedido.pk)
        if request.POST.get("accion") == "soltar":
            return _pedido_soltar(request, pedido)
        _reclamar_si_libre(pedido, request)
        return _picking_escanear(request, pedido)

    lineas = _lineas_en_ruta(pedido)
    pickeadas, total, _ = _avance(pedido)
    contexto = {
        "operadores": _operadores_piso(request),
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
    if not _es_mesa(request):
        en_picking = [p for p in en_picking if _pedido_libre_o_mio(p, request.user)]
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
    """True si la caja ya tiene su foto de cierre (Paquete.ts_cierre)."""
    return paquete.ts_cierre is not None


def _que_falta_empaque(pedido):
    """Qué le falta a un pedido para salir de la mesa de empaque, en corto (Mi turno)."""
    cajas = list(pedido.paquetes.all())
    if pedido.estado == Pedido.EN_PICKING:
        pendientes = [c for c in cajas if c.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE)]
        if cajas and len(pendientes) < len(cajas):
            return "sin empacar: caja " + ", ".join(str(c.numero) for c in pendientes)
        return "por empacar"
    empacadas = [c for c in cajas if c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO)]
    sin_guia = [c for c in empacadas if c.guia_activa is None]
    if sin_guia:
        return "sin guía: caja " + ", ".join(str(c.numero) for c in sin_guia)
    if not empacadas and not any(g.es_activa for g in pedido.guias.all()):
        return "sin guía"
    sin_cierre = [c for c in empacadas if c.ts_cierre is None]
    if sin_cierre:
        return "falta foto de cierre: caja " + ", ".join(str(c.numero) for c in sin_cierre)
    return "falta foto de cierre"


def _cajas_cliente(pedido):
    """Cajas de empaque activas del cliente (selector del wizard)."""
    from apps.catalogo.models import Caja  # lazy por contrato
    return list(Caja.objects.filter(cliente=pedido.cliente, activo=True))


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

    ?caja=N navega entre las cajas del pedido (chips arriba): la pendiente
    se empaca, la que falta cerrar abre su cierre y la cerrada se revisa con
    sus fotos y su báscula, que se pueden cambiar (acciones reemplazar_foto
    y corregir_peso).
    """
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=pk)

    if not _acceso_pedido(request, pedido):
        duenio = pedido.asignado_a.username if pedido.asignado_a else "otro operador"
        messages.error(
            request,
            f"{pedido.folio} lo tiene {duenio}: pídele que te lo envíe desde su pantalla.",
        )
        return redirect("piso:empaque")

    if request.method == "POST":
        accion = request.POST.get("accion") or "empacar"
        if accion == "transferir":
            _pedido_transferir(request, pedido)
            return redirect("piso:empaque_pedido", pk=pedido.pk)
        if accion == "reemplazar_foto":
            return _empaque_reemplazar_foto(request, pedido)
        if accion == "corregir_peso":
            return _empaque_corregir_peso(request, pedido)
        _reclamar_si_libre(pedido, request)
        if accion == "generar_guia":
            return _empaque_generar_guia(request, pedido)
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

    # PARCIALMENTE_DESPACHADO: ya salió alguna caja; las que siguen en bodega
    # toman aquí su foto de cierre para poder subir al siguiente manifiesto.
    elegida = _caja_elegida(request, pedido)
    if pedido.estado in (Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
        return _render_cierre_o_exito(request, pedido, elegida)
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
    return _render_paso_empacar(request, pedido, elegida)


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
    caja = None
    if request.POST.get("kit_caja"):
        try:
            caja = int(request.POST["kit_caja"])
        except (TypeError, ValueError):
            caja = None
    try:
        declarar_contenido_kit(linea, items, request.user, caja=caja)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        if caja is not None:
            messages.success(
                request,
                f"Caja {caja} de {linea.cantidad} del kit {linea.sku.codigo} declarada.",
            )
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


def _dims_producto(paquete):
    """Medidas de "la caja es el producto" para ESTA caja del plan: largo y
    ancho del producto más grande y los altos apilados por pieza. None si
    alguna línea va fraccionada (media caja) o un producto no tiene medidas.
    Prellena la opción "Sin caja del catálogo" del empaque (Chema 2026-09-21)."""
    largo = ancho = alto = 0
    lineas = list(paquete.lineas.select_related("linea_pedido__sku"))
    if not lineas:
        return None
    for pl in lineas:
        sku = pl.linea_pedido.sku
        if pl.fraccion_de > 1 or not (sku.largo_cm and sku.ancho_cm and sku.alto_cm):
            return None
        largo, ancho = max(largo, sku.largo_cm), max(ancho, sku.ancho_cm)
        alto += sku.alto_cm * pl.cantidad
    return (largo, ancho, alto)


def _caja_elegida(request, pedido):
    """Caja pedida con ?caja=N (navegación del wizard); None si no viene o no existe."""
    crudo = (request.GET.get("caja") or "").strip()
    if not crudo.isdigit():
        return None
    return pedido.paquetes.filter(numero=int(crudo)).first()


def _chips_cajas(cajas, actual=None):
    """Chips de navegación del wizard: cada caja con su estado corto y si es la abierta."""
    chips = []
    for caja in cajas:
        if caja.ts_cierre is not None:
            estado = "cerrada"
        elif caja.estado == Paquete.DESPACHADO:
            estado = "despachada"
        elif caja.estado == Paquete.EMPACADO:
            estado = "empacada"
        else:
            estado = "pendiente"
        chips.append({"caja": caja, "estado": estado, "activa": caja.numero == actual})
    return chips


def _fotos_sueltas(pedido, cajas):
    """Fotos de empaque del pedido sin caja (cierre único legacy), para poder cambiarlas."""
    ligadas = {c.foto_contenido_id for c in cajas} | {c.foto_cierre_id for c in cajas}
    return list(
        EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), tipo__in=("contenido", "caja_cerrada"),
        ).exclude(pk__in=[i for i in ligadas if i]).order_by("ts")
    )


def _contexto_peso(caja):
    """Rango de báscula de una caja para la pantalla: plan ± tolerancia, como al empacar."""
    plan_gr = int(caja.peso_kg * 1000) if caja.peso_kg else 0
    peso_min, peso_max, tolerancia = _rango_peso(plan_gr)
    return {
        "peso_esperado": plan_gr,
        "peso_min": peso_min,
        "peso_max": peso_max,
        "tolerancia": tolerancia,
        "peso_modo": settings.TORRE_PESO_MODO,
    }


def _render_revisar_caja(request, pedido, caja):
    """Una caja ya empacada: contenido, guía, báscula (corregible) y fotos (cambiables)."""
    cajas = _paquetes_con_lineas(pedido)
    caja = next((c for c in cajas if c.pk == caja.pk), caja)
    contexto = {
        "seccion": "empaque",
        "pedido": pedido,
        "paso": "revisar",
        "caja": caja,
        "total_cajas": len(cajas),
        "guia": caja.guia_activa,
        "chips": _chips_cajas(cajas, actual=caja.numero),
    }
    contexto.update(_contexto_peso(caja))
    return render(request, "piso/empaque_pedido.html", contexto)


def _render_paso_empacar(request, pedido, elegida=None):
    """Paso 1-2 del wizard: CAJA i de N (o caja única legacy) — foto + peso.

    ?caja=N (elegida): si sigue pendiente se empaca ella — el orden del plan
    es sugerencia, no candado; si ya está empacada, se revisa con sus fotos.
    """
    if elegida is not None and elegida.estado in (Paquete.EMPACADO, Paquete.DESPACHADO):
        return _render_revisar_caja(request, pedido, elegida)
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
            cupo = linea.sku.productos_por_kit or 0
            linea.cupo = cupo
            linea.piezas_declaradas = sum(h.cantidad for h in linea.hijas)
            linea.objetivo_total = cupo * linea.cantidad if cupo else None
            hechas = {h.kit_caja for h in linea.hijas if h.kit_caja}
            linea.proxima_caja = next(
                (n for n in range(1, linea.cantidad + 1) if n not in hechas), None,
            )
            if linea.objetivo_total is not None:
                linea.completo = linea.piezas_declaradas == linea.objetivo_total
            else:
                linea.completo = linea.piezas_declaradas > 0
            linea.renglones = list(range(1, (cupo or 3) + 1))
        contexto["kits"] = kits
        if any(not l.completo for l in kits):
            import json
            from apps.catalogo.models import opciones_sku_agrupadas
            contexto["opciones_kit"] = opciones_sku_agrupadas(pedido.cliente, excluir_kits=True)
            # Scan-to-fill: mapa barras→pk para llenar renglones escaneando.
            contexto["kit_barras_json"] = json.dumps({
                s.codigo_barras: s.pk
                for s in SKU.objects.filter(
                    cliente=pedido.cliente, activo=True, es_kit=False,
                ).exclude(codigo_barras="")
            })

    contexto["operadores"] = _operadores_piso(request)
    contexto["duenio"] = pedido.asignado_a
    if cajas and pendientes:
        caja = pendientes[0]
        if elegida is not None:
            caja = next((c for c in pendientes if c.pk == elegida.pk), caja)
        dims_producto = _dims_producto(caja)
        # Medidas iniciales: la caja del catálogo si ya se eligió; si no, las
        # del producto; si no hay, lo que estimó el plan.
        if caja.caja_id:
            dims_iniciales = (caja.caja.largo_cm, caja.caja.ancho_cm, caja.caja.alto_cm)
        else:
            dims_iniciales = dims_producto or (caja.largo_cm, caja.ancho_cm, caja.alto_cm)
        contexto.update({
            "paso": "caja",
            "caja": caja,
            "dims_producto": dims_producto,
            "dims_iniciales": dims_iniciales,
            "total_cajas": len(cajas),
            "cajas_listas": len(cajas) - len(pendientes),
            "chips": _chips_cajas(cajas, actual=caja.numero),
            "cajas_cliente": _cajas_cliente(pedido),
        })
        contexto.update(_contexto_peso(caja))
        return render(request, "piso/empaque_pedido.html", contexto)

    # Legacy sin plan de paquetes (o plan ya empacado por fuera): 1 caja.
    esperado = pedido.peso_esperado_gr or 0
    peso_min, peso_max, tolerancia = _rango_peso(esperado)
    contexto.update({
        "paso": "legacy",
        "total_cajas": 1,
        "tolerancia": tolerancia,
        "peso_modo": settings.TORRE_PESO_MODO,
        "peso_esperado": esperado,
        "peso_min": peso_min,
        "peso_max": peso_max,
        "fotos_existentes": EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), tipo="contenido",
        ).count(),
    })
    return render(request, "piso/empaque_pedido.html", contexto)


def _render_cierre_o_exito(request, pedido, elegida=None):
    """Paso 3-5 del wizard: etiquetas impresas → foto de cierre → éxito.

    ?caja=N (elegida): si le falta el cierre, abre SU paso de cierre aunque no
    sea la primera; si ya está cerrada, la revisión con sus fotos (cambiables).
    """
    cajas = _paquetes_con_lineas(pedido)
    if (
        elegida is not None
        and elegida.estado in (Paquete.EMPACADO, Paquete.DESPACHADO)
        and _caja_cerrada(elegida)
    ):
        return _render_revisar_caja(request, pedido, elegida)
    contexto = {
        "seccion": "empaque",
        "pedido": pedido,
        "fotos_sueltas": _fotos_sueltas(pedido, cajas),
    }
    if pedido.cajas_cerradas_completas:
        contexto.update({
            "paso": "exito",
            "corral": _corral_de_carrier(_carrier_probable(pedido)),
            "chips": _chips_cajas(cajas),
        })
        return render(request, "piso/empaque_pedido.html", contexto)

    guias = list(
        pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).order_by("id")
    )
    cajas_empacadas = [c for c in cajas if c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO)]
    por_cerrar = [c for c in cajas_empacadas if not _caja_cerrada(c)]
    caja_cierre = por_cerrar[0] if por_cerrar else None
    if elegida is not None:
        caja_cierre = next((c for c in por_cerrar if c.pk == elegida.pk), caja_cierre)
    con_guia = {g.paquete_id for g in guias}
    contexto.update({
        "paso": "cierre",
        "guias": guias,
        "tiene_guia": bool(guias),
        # Cajas del plan sin guía activa (el carrier falló en esa caja): se generan desde Salida.
        "cajas_sin_guia": [c for c in cajas_empacadas if c.pk not in con_guia],
        "cajas_empacadas": cajas_empacadas,
        "por_cerrar": por_cerrar,
        "caja_cierre": caja_cierre,
        "otras_por_cerrar": [c for c in por_cerrar if caja_cierre and c.pk != caja_cierre.pk],
        "chips": _chips_cajas(cajas, actual=caja_cierre.numero if caja_cierre else None),
        "cierre_legacy": not cajas_empacadas,  # 1 caja implícita
        "total_cierres": max(len(cajas_empacadas), 1),
        "cierres_hechos": max(len(cajas_empacadas), 1) - (len(por_cerrar) or 1),
        # La guía que falló con el carrier se reintenta AQUÍ (antes vivía en
        # Salida): nada llega al corral sin guía y foto de cierre.
        "puede_reintentar_guia": pedido.estado == Pedido.EMPACADO,
    })
    if caja_cierre is not None:
        contexto.update(_contexto_peso(caja_cierre))
    return render(request, "piso/empaque_pedido.html", contexto)


def _despachar_y_avisar(request, pedido, destino):
    """MISMO POST del empaque: guía + impresión automáticas (best-effort).

    Un error del carrier deja el pedido EMPACADO y recuperable desde el propio
    paso de cierre (botón Reintentar guía); un error de impresora avisa y el
    flujo continúa (la reimpresión vive en Salida).
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
            "Reintenta en un momento con 'Reintentar guía' o avisa a Mesa de Control.",
        )
        from apps.envios.models import Guia  # lazy: modelo de otra app
        con_guia = pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).count()
        if con_guia:
            messages.info(
                request,
                f"Las {con_guia} caja(s) que sí tienen guía ya se mandaron a imprimir; "
                "la que falló se reintenta aquí mismo con 'Reintentar guía'.",
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
    caja = None
    if request.POST.get("caja_id"):
        from apps.catalogo.models import Caja  # lazy por contrato
        caja = Caja.objects.filter(
            pk=request.POST["caja_id"], cliente=pedido.cliente, activo=True,
        ).first()
    dims = None
    crudos = [request.POST.get(c) for c in ("largo_cm", "ancho_cm", "alto_cm")]
    if all(crudos):
        try:
            valores = [int(v) for v in crudos]
            dims = tuple(valores) if all(v > 0 for v in valores) else None
        except (TypeError, ValueError):
            dims = None
    tara_gr = None
    if (request.POST.get("tara_gr") or "").strip():
        try:
            tara_gr = max(int(request.POST["tara_gr"]), 0)
        except (TypeError, ValueError):
            tara_gr = None
    try:
        empacar_caja(
            paquete, request.user,
            request.POST.get("peso_real_gr"), request.FILES.get("foto_contenido"),
            caja=caja, dims=dims, tara_gr=tara_gr,
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


def _empaque_generar_guia(request, pedido):
    """Reintento de guía desde el paso de cierre: el pedido EMPACADO cuya guía
    falló con el carrier se vuelve a intentar aquí (antes vivía en Salida)."""
    destino = redirect("piso:empaque_pedido", pk=pedido.pk)
    if pedido.estado != Pedido.EMPACADO:
        messages.error(
            request,
            f"{pedido.folio} no está esperando guía (está {pedido.get_estado_display().lower()}).",
        )
        return destino
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


def _empaque_reemplazar_foto(request, pedido):
    """Cambia una foto de empaque que salió mal y regresa a la caja de donde vino."""
    numero = (request.POST.get("caja") or "").strip()
    url = reverse("piso:empaque_pedido", args=[pedido.pk])
    destino = redirect(f"{url}?caja={numero}" if numero.isdigit() else url)
    from apps.pedidos.services import reemplazar_foto_pedido  # lazy por contrato
    try:
        nueva = reemplazar_foto_pedido(
            pedido, request.user, request.POST.get("evidencia_id"), request.FILES.get("foto"),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    que = "del contenido" if nueva.tipo == "contenido" else "de la caja cerrada"
    donde = f" (caja {numero})" if numero.isdigit() else ""
    messages.success(request, f"Foto {que} cambiada{donde}. La anterior ya no existe.")
    return destino


def _empaque_corregir_peso(request, pedido):
    """Corrige la báscula de una caja ya empacada y regresa a esa caja."""
    paquete = get_object_or_404(Paquete, pk=request.POST.get("paquete_id"), pedido=pedido)
    url = reverse("piso:empaque_pedido", args=[pedido.pk])
    destino = redirect(f"{url}?caja={paquete.numero}")
    from apps.pedidos.services import corregir_peso_caja  # lazy por contrato
    try:
        corregir_peso_caja(paquete, request.user, request.POST.get("peso_real_gr"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request, f"Caja {paquete.numero}: báscula corregida a {paquete.peso_real_gr} g.",
    )
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


def _contenido_salida(pedido):
    """Contenido del pedido para Salida (pedido de Chema 2026-09-08): TODAS las
    líneas del pedido con las hijas de kit debajo de su kit (ahí suele estar
    la confusión); con más de una caja, además el desglose por caja del plan.
    Solo lectura; sin consultas extra si el queryset trae el prefetch de
    lineas__sku y paquetes__lineas__linea_pedido__sku."""
    lineas = list(pedido.lineas.all())
    hijas = {}
    for linea in lineas:
        if linea.parte_de_kit_id:
            hijas.setdefault(linea.parte_de_kit_id, []).append(linea)
    renglones = [
        {"linea": linea, "hijas": hijas.get(linea.pk, [])}
        for linea in lineas if not linea.parte_de_kit_id
    ]
    paquetes = list(pedido.paquetes.all())
    cajas = []
    if len(paquetes) > 1:
        cajas = [
            {"numero": p.numero, "textos": [pl.texto_para_piso for pl in p.lineas.all()]}
            for p in sorted(paquetes, key=lambda p: p.numero)
        ]
    return {
        "renglones": renglones,
        "piezas": sum(r["linea"].cantidad for r in renglones),
        "cajas": cajas,
    }


@rol_requerido("piso", "mesa")
def salida(request):
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "manifiesto":
            return _salida_manifiesto(request)
        if accion == "recoleccion":
            return _salida_recoleccion(request)
        messages.error(request, "No entendí la acción. Intenta de nuevo.")
        return redirect("piso:salida")

    orden_corrales = corrales_activos()
    mapa = _mapa_corrales()
    grupos = {
        codigo: {"codigo": codigo, "nombre": nombre, "listos": []}
        for codigo, nombre in orden_corrales
    }

    def _grupo(carrier):
        corral = _corral_de_carrier(carrier, mapa)
        if corral not in grupos:  # corral sin ubicación viva (p. ej. SAL-LOCAL viejo)
            grupos[corral] = {"codigo": corral, "nombre": corral, "listos": []}
            orden_corrales.append((corral, corral))
        return grupos[corral]

    prefetch_contenido = ("lineas__sku", "paquetes__lineas__linea_pedido__sku")
    from apps.pedidos.services import cajas_por_salir  # lazy por contrato
    # Al corral solo llega lo que terminó en la mesa: todas las cajas con guía
    # y foto de cierre (Pedido.empaque_completo). Lo demás se termina en el
    # wizard de empaque, desde "Completar empaquetado" en Mi turno.
    for pedido in (
        Pedido.objects.filter(estado__in=[Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO])
        .select_related("cliente").prefetch_related(*prefetch_contenido, "paquetes__guias", "guias")
    ):
        if not pedido.empaque_completo:
            continue
        pedido.contenido = _contenido_salida(pedido)
        # Cajas que siguen en bodega (empaque por caja): cada una se palomea
        # sola en el manifiesto. Vacío = el pedido se empacó entero y sale completo.
        pedido.cajas_salida = cajas_por_salir(pedido)
        for caja in pedido.cajas_salida:
            caja.guia = caja.guia_activa
        pedido.por_caja = _por_caja(pedido)
        # Todas las guías vigentes de lo que sigue aquí: una etiqueta por guía.
        pedido.guias_activas = [
            g for g in pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).order_by("id")
            if g.paquete_id is None or g.paquete.estado != Paquete.DESPACHADO
        ]
        guia = pedido.guias_activas[-1] if pedido.guias_activas else None
        pedido.guia = guia
        carrier = guia.carrier if guia else _carrier_probable(pedido)
        _grupo(carrier)["listos"].append(pedido)
    for grupo in grupos.values():
        grupo["firman"] = len(grupo["listos"])
        # El manifiesto se firma POR CARRIER, no por corral: SAL-OTRO junta
        # estafeta+puntopost+fedex y cada chofer se lleva SOLO lo suyo — firmar
        # el corral entero mandaría "va en camino" a compradores cuyo paquete
        # sigue en el piso.
        por_carrier = {}
        for p in grupo["listos"]:
            clave = (p.guia.carrier if p.guia else _carrier_probable(p)) or "?"
            por_carrier.setdefault(clave, []).append(p)
        grupo["carriers"] = [
            {"carrier": clave, "listos": pedidos, "firman": len(pedidos)}
            for clave, pedidos in sorted(por_carrier.items())
        ]

    # Recolecciones programadas (Lote E): botón solo para carriers que
    # aceptan pickup vía envia; una ya agendada se muestra en vez del form.
    from django.utils import timezone as _tz

    from apps.envios.models import Recoleccion
    hoy = _tz.localdate()
    pickup_map = settings.TORRE.get("CARRIERS_PICKUP") or {}
    agendadas = {r.carrier: r for r in Recoleccion.objects.filter(fecha__gte=hoy)}
    for grupo in grupos.values():
        for gc in grupo["carriers"]:
            gc["puede_recolectar"] = bool(pickup_map.get(gc["carrier"]))
            gc["recoleccion"] = agendadas.get(gc["carrier"])

    contexto = {
        "seccion": "salida",
        "hoy_iso": hoy.isoformat(),
        "corrales": [grupos[codigo] for codigo, _ in orden_corrales],
        "corral_local": CORRAL_LOCAL,
        "flota_propia": _flota_propia(),
    }
    return render(request, "piso/salida.html", contexto)


def _salida_recoleccion(request):
    """Agenda la recolección del carrier con TODAS sus guías vivas del corral."""
    from datetime import date as _date

    from apps.envios import services as envios_services
    from apps.envios.models import Guia

    carrier = (request.POST.get("carrier") or "").strip()
    try:
        fecha = _date.fromisoformat(request.POST.get("fecha") or "")
        desde = int(request.POST.get("desde") or 10)
        hasta = int(request.POST.get("hasta") or 18)
    except (TypeError, ValueError):
        messages.error(request, "Revisa la fecha y la ventana de la recolección.")
        return redirect("piso:salida")
    guias = [
        g for g in Guia.objects.filter(carrier=carrier, estado=Guia.GUIA_CREADA)
        .select_related("pedido")
        if g.pedido.estado == "GUIA_GENERADA"
    ]
    try:
        rec = envios_services.agendar_recoleccion(
            carrier, fecha, desde, hasta, guias, request.user,
            (request.POST.get("instrucciones") or "").strip(),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("piso:salida")
    except Exception as exc:  # ErrorCarrier: el piso debe saberlo
        messages.error(
            request,
            f"El carrier no confirmó la recolección: {exc}. Reintenta o avisa a Mesa.",
        )
        return redirect("piso:salida")
    messages.success(
        request,
        f"Recolección de {carrier} agendada: {rec.fecha} ({desde}-{hasta} h) · "
        f"folio {rec.folio_carrier or 's/n'} · {len(guias)} guía(s).",
    )
    return redirect("piso:salida")


def _por_caja(pedido):
    """True si el pedido se empacó por caja con más de una caja: el manifiesto
    palomea cajas (paquete_id), no el pedido entero."""
    return sum(
        1 for c in pedido.paquetes.all() if c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO)
    ) > 1


def _salida_manifiesto(request):
    """Manifiesto firmado: marca RECOLECTADO lo palomeado de UN carrier.

    Aquí — y solo aquí — se dispara el "va en camino" al comprador
    (lo hace pedidos.services.marcar_recolectado, plantilla B).

    Por carrier y con selección: SAL-OTRO junta varios carriers y cada chofer
    firma SOLO por lo que sube a SU camión; lo no palomeado (camión lleno,
    caja con detalle) se queda en el corral para la siguiente recolección.
    Pedidos de varias cajas se palomean POR CAJA (paquete_id): las que suben
    salen y el pedido queda PARCIALMENTE_DESPACHADO hasta que salga la última.
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
    seleccion = {int(v) for v in request.POST.getlist("pedido_id") if v.isdigit()}
    cajas_sel = {int(v) for v in request.POST.getlist("paquete_id") if v.isdigit()}
    if not seleccion and not cajas_sel:
        messages.error(
            request,
            "No palomeaste ningún pedido: marca lo que el chofer se lleva y vuelve a firmar.",
        )
        return redirect("piso:salida")
    pedidos_de_cajas = set(
        Paquete.objects.filter(pk__in=cajas_sel).values_list("pedido_id", flat=True)
    )

    from apps.pedidos.services import cajas_por_salir  # lazy por contrato
    listos, sin_cierre = [], []
    mapa = _mapa_corrales()
    for pedido in Pedido.objects.filter(
        estado__in=[Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO],
        pk__in=seleccion | pedidos_de_cajas,
    ).select_related("cliente").prefetch_related("paquetes__guias"):
        guia = _guia_activa(pedido)
        carrier_pedido = guia.carrier if guia else _carrier_probable(pedido)
        # Un pedido de otro carrier u otro corral no sube a ESTE manifiesto
        # aunque venga palomeado (formulario viejo, doble submit, manipulación).
        if _corral_de_carrier(carrier_pedido, mapa) != corral or carrier_pedido != carrier:
            continue
        # Sin evidencia de cierre (foto de la caja cerrada con su etiqueta
        # pegada) el pedido — o la caja — NO sube al manifiesto: se queda y se avisa.
        if not _por_caja(pedido):
            if not pedido.cajas_cerradas_completas:
                sin_cierre.append(pedido)
                continue
            listos.append((pedido, None))
            continue
        elegidas = [
            c for c in cajas_por_salir(pedido)
            if pedido.pk in seleccion or c.pk in cajas_sel
        ]
        cerradas = [c for c in elegidas if c.ts_cierre is not None]
        if not cerradas:
            sin_cierre.append(pedido)
            continue
        listos.append((pedido, cerradas))
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
    for pedido, cajas in listos:
        try:
            marcar_recolectado(pedido, request.user, paquetes=cajas)
            recolectados.append(pedido.folio)
        except ValueError as exc:
            errores.append(f"{pedido.folio}: {exc}")
            continue
        pedido.refresh_from_db()
        if pedido.estado == Pedido.PARCIALMENTE_DESPACHADO:
            quedan = ", ".join(str(c.numero) for c in cajas_por_salir(pedido))
            messages.warning(
                request,
                f"{pedido.folio}: salió la caja {', '.join(str(c.numero) for c in cajas)}; "
                f"la caja {quedan} se queda en el corral para la siguiente recolección.",
            )

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
        # Al contar, el piso puede marcar el anaquel lleno o con espacio
        # (corrige la ocupación estimada del acomodo sugerido).
        tarea.anaqueles = list(
            Ubicacion.objects.filter(codigo__in=tarea.ubicaciones, tipo=Ubicacion.PICKING).order_by("codigo")
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

    marca = (request.POST.get("anaquel_estado") or "").strip()
    if marca and "|" in marca:
        codigo, estado = marca.split("|", 1)
        anaquel = Ubicacion.objects.filter(codigo=codigo, tipo=Ubicacion.PICKING).first()
        if anaquel is not None and estado in ("lleno", "espacio"):
            from apps.inventario.services import marcar_anaquel  # lazy por contrato
            if marcar_anaquel(anaquel, estado == "lleno", request.user):
                messages.info(request, f"{anaquel.codigo} marcado {'lleno' if estado == 'lleno' else 'con espacio'}.")

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
    from apps.catalogo.services import lotes_sugeridos  # lazy por contrato
    from apps.inventario.services import sugerir_anaquel  # lazy por contrato
    for saldo in putaway:
        saldo.lotes_sugeridos = lotes_sugeridos(saldo.sku) if saldo.lote_id is None else []
        saldo.sugerencia = sugerir_anaquel(saldo.sku, saldo.cantidad)
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
            from apps.catalogo.services import obtener_o_crear_lote  # lazy por contrato
            lote = obtener_o_crear_lote(sku, lote_codigo, fecha_caducidad)

    from apps.inventario.services import aviso_capacidad, ubicar  # lazy por contrato
    aviso = aviso_capacidad(ubicacion, sku, cantidad)  # antes de ubicar: estimado con lo que va a entrar
    try:
        ubicar(sku, cantidad, ubicacion, lote, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request,
        f"{cantidad} × {sku.codigo} ubicadas en {ubicacion.codigo}: ya cuentan como vendibles.",
    )
    if aviso:
        messages.warning(request, aviso)
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
