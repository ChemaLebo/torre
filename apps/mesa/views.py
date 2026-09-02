"""Mesa de Control: el tablero interno de la operación completa.

Aquí NO se filtra por tenant: la Mesa ve todos los clientes. Toda acción de
gestión (responder incidencias, compensaciones, reclamaciones, push manual)
pasa por los servicios de dominio para que la auditoría y los relojes SLA
queden siempre consistentes. Cross-app: modelos y servicios se importan lazy
(dentro de cada función) según CONVENTIONS.md.
"""
import csv
import io
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.core.decorators import rol_requerido
from apps.core.models import Cliente, EventoAuditoria, EvidenciaFoto, PerfilUsuario
from apps.core.services import registrar_evento
from apps.mesa.forms import (
    CAMPOS_TARIFARIO_SIMPLES, FormAnuncioASNMesa, FormCliente, FormPedidoManual,
    FormSKU, FormTarifario, ZONAS_ENVIO,
)

# Minutos antes de vencer un reloj SLA en los que la pill pasa a "warn".
UMBRAL_WARN_SLA_MIN = 240

# Proyección visual del estado del pedido a la pill del sistema WOP.
PILL_PEDIDO = {
    "PENDIENTE": "",
    "EN_PICKING": "accent",
    "EMPACADO": "accent",
    "GUIA_GENERADA": "accent",
    "RECOLECTADO": "accent",
    "EN_TRANSITO": "accent",
    "ENTREGADO": "ok",
    "ENTREGA_PRESUNTA": "warn",
    "PARCIALMENTE_DESPACHADO": "warn",
    "CANCELACION_PENDIENTE": "warn",
    "CANCELADO": "",
    "RETORNADO": "crit",
}

# Estados desde los que la Mesa puede pedir cancelación: la matriz de
# pedidos.services.cancelar hace el resto (los terminales truenan con ValueError
# y CANCELACION_PENDIENTE se cierra en piso con el restock, no aquí).
ESTADOS_CANCELABLES_MESA = {
    "PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA",
    "RECOLECTADO", "EN_TRANSITO", "PARCIALMENTE_DESPACHADO", "ENTREGA_PRESUNTA",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de presentación
# ─────────────────────────────────────────────────────────────────────────────


def _humano_delta(delta):
    """'4 min', '3 h', '2 días' — para relojes y frescura, sin tecnicismos."""
    minutos = int(abs(delta.total_seconds()) // 60)
    if minutos < 60:
        return f"{minutos} min"
    horas = minutos // 60
    if horas < 48:
        return f"{horas} h"
    return f"{horas // 24} días"


def _frescura(ts, ahora=None):
    """'hace X min' para salud de sync. NUNCA la palabra 'tiempo real'."""
    if ts is None:
        return "nunca"
    ahora = ahora or timezone.now()
    return f"hace {_humano_delta(ahora - ts)}"


def _anotar_reloj_sla(incidencia, ahora):
    """Cuelga al objeto: reloj activo, pill ok|warn|crit y texto legible.

    El reloj activo es el de primera respuesta mientras no haya respuesta
    humana; después, el de resolución. Vencido → crit; por vencer → warn.
    """
    if incidencia.ts_primera_respuesta is None and incidencia.sla_respuesta_limite is not None:
        limite, etiqueta = incidencia.sla_respuesta_limite, "primera respuesta"
    else:
        limite, etiqueta = incidencia.sla_resolucion_limite, "resolución"

    incidencia.reloj_etiqueta = etiqueta
    incidencia.reloj_limite = limite
    if not incidencia.abierta:
        incidencia.reloj_pill = "ok"
        incidencia.reloj_texto = "reloj detenido"
        return incidencia
    if limite is None:
        incidencia.reloj_pill = "ok"
        incidencia.reloj_texto = "sin reloj"
        return incidencia

    restante = limite - ahora
    if restante < timedelta(0):
        incidencia.reloj_pill = "crit"
        incidencia.reloj_texto = f"{etiqueta} vencida hace {_humano_delta(restante)}"
    elif restante <= timedelta(minutes=UMBRAL_WARN_SLA_MIN):
        incidencia.reloj_pill = "warn"
        incidencia.reloj_texto = f"quedan {_humano_delta(restante)} para {etiqueta}"
    else:
        incidencia.reloj_pill = "ok"
        incidencia.reloj_texto = f"quedan {_humano_delta(restante)} para {etiqueta}"
    return incidencia


def _guias_sin_movimiento(ahora):
    """Guías vivas quietas más allá del umbral de su ruta (settings.TORRE)."""
    from apps.envios.models import Guia

    torre = settings.TORRE
    atoradas = []
    guias = (
        Guia.objects.exclude(estado__in=list(Guia.ESTADOS_TERMINALES))
        .exclude(carrier="local")
        .select_related("pedido", "pedido__cliente")
    )
    for guia in guias:
        umbral = (
            torre["SIN_MOVIMIENTO_LOCAL_HORAS"]
            if guia.pedido.es_local
            else torre["SIN_MOVIMIENTO_FORANEO_HORAS"]
        )
        referencia = guia.ts_ultimo_movimiento or guia.creado
        if referencia is None:
            continue
        horas_quietas = (ahora - referencia).total_seconds() / 3600
        if horas_quietas > umbral:
            guia.horas_quietas = int(horas_quietas)
            guia.horas_umbral = umbral
            atoradas.append(guia)
    atoradas.sort(key=lambda g: -g.horas_quietas)
    return atoradas


def _pct_salida_mismo_dia(ahora):
    """% de la semana: pedidos pre-corte recolectados el mismo día de su ingreso.

    Los pedidos de hoy que siguen en proceso no cuentan (ni a favor ni en
    contra): el día no ha terminado. Regresa (pct o None, medibles, a_tiempo).
    """
    from apps.pedidos.models import Pedido

    hoy = timezone.localdate()
    desde = ahora - timedelta(days=7)
    medibles = 0
    a_tiempo = 0
    pedidos = Pedido.objects.filter(creado__gte=desde).exclude(
        estado__in=[Pedido.CANCELADO, Pedido.CANCELACION_PENDIENTE]
    )
    for pedido in pedidos:
        creado_local = timezone.localtime(pedido.creado)
        corte = pedido.corte_vigente_al_ingreso
        if corte is None or creado_local.time() > corte:
            continue  # post-corte: la promesa es el siguiente día hábil
        if creado_local.date() == hoy and pedido.ts_recolectado is None:
            continue  # todavía en proceso hoy: aún puede salir a tiempo
        medibles += 1
        if (
            pedido.ts_recolectado is not None
            and timezone.localtime(pedido.ts_recolectado).date() == creado_local.date()
        ):
            a_tiempo += 1
    pct = round(a_tiempo * 100 / medibles) if medibles else None
    return pct, medibles, a_tiempo


def _exactitud_inventario():
    """% de conteos exactos (contado == esperado) en los últimos 30 días."""
    from apps.inventario.models import Conteo
    from django.db.models import F

    desde = timezone.now() - timedelta(days=30)
    total = Conteo.objects.filter(ts__gte=desde).count()
    if not total:
        return None, 0
    exactos = Conteo.objects.filter(ts__gte=desde, contado=F("esperado")).count()
    return round(exactos * 100 / total, 1), total


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def dashboard(request):
    from apps.incidencias.models import Incidencia
    from apps.incidencias.services import abiertas_fuera_de_sla
    from apps.pedidos.models import Pedido

    ahora = timezone.now()
    hoy = timezone.localdate()

    # Pedidos de hoy, por estado (orden canónico del pipeline).
    conteos = dict(
        Pedido.objects.filter(creado__date=hoy)
        .values_list("estado")
        .annotate(n=Count("id"))
    )
    total_hoy = sum(conteos.values())
    pedidos_hoy = [
        {
            "estado": clave,
            "nombre": nombre,
            "n": conteos[clave],
            "pct": round(conteos[clave] * 100 / total_hoy) if total_hoy else 0,
            "pill": PILL_PEDIDO.get(clave, ""),
        }
        for clave, nombre in Pedido.ESTADOS
        if conteos.get(clave)
    ]

    # Incidencias abiertas ordenadas por su reloj SLA (el más apretado arriba).
    abiertas = [
        _anotar_reloj_sla(inc, ahora)
        for inc in Incidencia.objects.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
        .select_related("cliente", "pedido")
    ]
    abiertas.sort(key=lambda i: (i.reloj_limite is None, i.reloj_limite or ahora))
    fuera_de_sla = abiertas_fuera_de_sla().count()

    salida_pct, salida_medibles, salida_a_tiempo = _pct_salida_mismo_dia(ahora)
    exactitud_pct, conteos_30d = _exactitud_inventario()
    atoradas = _guias_sin_movimiento(ahora)
    eventos = EventoAuditoria.objects.select_related("cliente")[:15]

    # Banner semáforo del día.
    if fuera_de_sla or atoradas:
        problemas = []
        if fuera_de_sla:
            problemas.append(f"{fuera_de_sla} incidencia(s) fuera de SLA")
        if atoradas:
            problemas.append(f"{len(atoradas)} guía(s) sin movimiento")
        banner = {"nivel": "crit", "texto": "Atención: " + " y ".join(problemas) + ". La pelota es nuestra."}
    elif any(i.reloj_pill == "warn" for i in abiertas):
        banner = {
            "nivel": "warn",
            "texto": f"Relojes apretados: {sum(1 for i in abiertas if i.reloj_pill == 'warn')} incidencia(s) por vencer. Hoy: {total_hoy} pedido(s).",
        }
    else:
        banner = {
            "nivel": "ok",
            "texto": f"Todo en orden. {total_hoy} pedido(s) hoy, corte a las {settings.TORRE['CORTE_CONTRACTUAL']}.",
        }

    return render(request, "mesa/dashboard.html", {
        "seccion": "dashboard",
        "banner": banner,
        "total_hoy": total_hoy,
        "pedidos_hoy": pedidos_hoy,
        "incidencias_abiertas": abiertas,
        "fuera_de_sla": fuera_de_sla,
        "salida_pct": salida_pct,
        "salida_medibles": salida_medibles,
        "salida_a_tiempo": salida_a_tiempo,
        "exactitud_pct": exactitud_pct,
        "conteos_30d": conteos_30d,
        "guias_atoradas": atoradas,
        "eventos": eventos,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Bodega en vivo: el plano real del Local 380 E con datos por zona
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def bodega(request):
    """El plano real del Local 380 E con la carga de trabajo de cada zona, ahora.

    Los querysets viven en apps.mesa.plano.zonas_bodega (compartido con el
    portal, que lo llama filtrado por tenant); aquí solo se decora con el
    vocabulario de la Mesa (pills y relojes SLA).
    """
    from apps.incidencias.models import Incidencia
    from apps.mesa.plano import racks_bodega, zonas_bodega

    zonas, badges, zona_activa = zonas_bodega()
    badges["oficina"] = Incidencia.objects.filter(
        estado__in=Incidencia.ESTADOS_ABIERTOS
    ).count()

    for orden in zonas["ordenes"]:
        _decorar_asn(orden)
    for grupo in zonas["corrales"]:
        for pedido in grupo["pedidos"]:
            pedido.pill = PILL_PEDIDO.get(pedido.estado, "")

    return render(request, "mesa/bodega.html", {
        "seccion": "bodega",
        "actualizado": timezone.localtime(),
        "badges": badges,
        "racks": racks_bodega(),
        "zona_activa": zona_activa,
        **zonas,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Incidencias: lista y gestión completa
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def incidencias(request):
    from apps.incidencias.models import Incidencia

    ahora = timezone.now()
    qs = Incidencia.objects.select_related("cliente", "pedido", "sku")

    estado = request.GET.get("estado", "").strip()
    tipo = request.GET.get("tipo", "").strip()
    cliente_id = request.GET.get("cliente", "").strip()
    if estado == "abiertas":
        qs = qs.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
    elif estado:
        qs = qs.filter(estado=estado)
    if tipo:
        qs = qs.filter(tipo=tipo)
    if cliente_id:
        qs = qs.filter(cliente_id=cliente_id)

    filas = [_anotar_reloj_sla(inc, ahora) for inc in qs]
    # Abiertas primero, ordenadas por reloj; cerradas después, recientes arriba.
    filas.sort(key=lambda i: (
        not i.abierta,
        (i.reloj_limite or ahora) if i.abierta else ahora,
        -(i.ts_apertura.timestamp()),
    ))

    return render(request, "mesa/incidencias.html", {
        "seccion": "incidencias",
        "incidencias": filas,
        "tipos": Incidencia.TIPOS,
        "estados": Incidencia.ESTADOS,
        "clientes_filtro": Cliente.objects.all(),
        "filtro": {"estado": estado, "tipo": tipo, "cliente": cliente_id},
        "abiertas_n": sum(1 for i in filas if i.abierta),
    })


def _gestionar_incidencia(request, incidencia):
    """Despacha la acción POST del detalle. Regresa mensaje de éxito o lanza ValueError."""
    from apps.incidencias.models import Compensacion, ReclamacionCarrier
    from apps.incidencias.services import cerrar, resolver, responder

    accion = request.POST.get("accion", "")
    usuario = request.user.username
    texto = (request.POST.get("texto") or "").strip()

    if accion == "responder":
        if not texto:
            raise ValueError("Escribe la respuesta antes de enviarla.")
        responder(incidencia, autor=usuario, rol_autor="mesa", texto=texto)
        return "Respuesta publicada en el timeline (visible para el cliente)."

    if accion == "nota_interna":
        if not texto:
            raise ValueError("Escribe la nota interna antes de guardarla.")
        responder(incidencia, autor=usuario, rol_autor="mesa", texto=texto, interno=True)
        return "Nota interna guardada. No se muestra en el portal, pero queda en el registro."

    if accion == "tomar":
        incidencia.transicionar("EN_CURSO", actor=request.user, motivo="La Mesa toma la incidencia")
        incidencia.dueno = usuario
        incidencia.save(update_fields=["dueno"])
        return f"Incidencia en curso. La pelota la tiene {usuario}."

    if accion == "resolver":
        if not texto:
            raise ValueError("Describe la resolución antes de marcar resuelta.")
        resolver(incidencia, texto, request.user)
        return "Incidencia resuelta. El cliente puede confirmarla o reabrirla desde su portal."

    if accion == "cerrar":
        cerrar(incidencia, request.user)
        return "Incidencia cerrada. Si el pedido ya no tiene incidencias abiertas, quedó liberado."

    if accion == "compensacion_crear":
        tipo = request.POST.get("tipo", "")
        if tipo not in dict(Compensacion.TIPOS):
            raise ValueError("Elige el tipo de compensación.")
        try:
            monto = Decimal(str(request.POST.get("monto", "")).strip())
        except InvalidOperation:
            raise ValueError("Captura el monto de la compensación en MXN (ej. 450.00).")
        if monto <= 0:
            raise ValueError("El monto de la compensación debe ser mayor a cero.")
        comp = Compensacion.objects.create(incidencia=incidencia, tipo=tipo, monto=monto)
        registrar_evento(
            "compensacion", comp.pk, "creada", actor=request.user, cliente=incidencia.cliente,
            delta={"incidencia": incidencia.folio, "tipo": tipo, "monto": str(monto)},
        )
        return f"Compensación cotizada: {comp.get_tipo_display()} por ${monto} MXN."

    if accion == "compensacion_avanzar":
        comp = get_object_or_404(
            Compensacion, pk=request.POST.get("compensacion_id"), incidencia=incidencia
        )
        nuevo = request.POST.get("nuevo_estado", "")
        if nuevo not in dict(Compensacion.ESTADOS):
            raise ValueError("Estado de compensación desconocido.")
        if nuevo == Compensacion.PAGADA:
            referencia = (request.POST.get("referencia_pago") or "").strip()
            if referencia:
                comp.referencia_pago = referencia
                comp.save(update_fields=["referencia_pago"])
        comp.transicionar(nuevo, actor=request.user, motivo=f"Gestión desde Mesa ({incidencia.folio})")
        return f"Compensación actualizada a {comp.get_estado_display()}."

    if accion == "reclamacion_crear":
        carrier = (request.POST.get("carrier") or "").strip()
        if not carrier:
            raise ValueError("Indica el carrier al que se reclama.")
        try:
            monto = Decimal(str(request.POST.get("monto_reclamado", "")).strip())
        except InvalidOperation:
            raise ValueError("Captura el monto reclamado en MXN.")
        if monto <= 0:
            raise ValueError("El monto reclamado debe ser mayor a cero.")
        rec = ReclamacionCarrier.objects.create(
            incidencia=incidencia, carrier=carrier, monto_reclamado=monto
        )
        registrar_evento(
            "reclamacion_carrier", rec.pk, "creada", actor=request.user, cliente=incidencia.cliente,
            delta={"incidencia": incidencia.folio, "carrier": carrier, "monto_reclamado": str(monto)},
        )
        return f"Expediente de reclamación preparado contra {carrier} por ${monto} MXN."

    if accion == "reclamacion_avanzar":
        rec = get_object_or_404(
            ReclamacionCarrier, pk=request.POST.get("reclamacion_id"), incidencia=incidencia
        )
        nuevo = request.POST.get("nuevo_estado", "")
        if nuevo not in dict(ReclamacionCarrier.ESTADOS):
            raise ValueError("Estado de reclamación desconocido.")
        monto_crudo = (request.POST.get("monto_recuperado") or "").strip()
        if nuevo in (ReclamacionCarrier.ACEPTADA, ReclamacionCarrier.PAGADA) and monto_crudo:
            try:
                rec.monto_recuperado = Decimal(monto_crudo)
            except InvalidOperation:
                raise ValueError("El monto recuperado no es un número válido.")
            rec.save(update_fields=["monto_recuperado"])
        rec.transicionar(nuevo, actor=request.user, motivo=f"Gestión desde Mesa ({incidencia.folio})")
        return f"Reclamación actualizada a {rec.get_estado_display()}."

    raise ValueError("Acción desconocida. Recarga la página e intenta de nuevo.")


@rol_requerido("mesa")
def incidencia_detalle(request, pk):
    from apps.incidencias.models import Compensacion, Incidencia, ReclamacionCarrier

    incidencia = get_object_or_404(
        Incidencia.objects.select_related("cliente", "pedido", "sku"), pk=pk
    )

    if request.method == "POST":
        try:
            exito = _gestionar_incidencia(request, incidencia)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, exito)
        return redirect("mesa:incidencia_detalle", pk=incidencia.pk)

    ahora = timezone.now()
    _anotar_reloj_sla(incidencia, ahora)

    compensaciones = list(incidencia.compensaciones.all())
    for comp in compensaciones:
        comp.siguientes = [
            (estado, dict(Compensacion.ESTADOS)[estado])
            for estado in sorted(Compensacion.TRANSICIONES.get(comp.estado, set()))
        ]
    reclamaciones = list(incidencia.reclamaciones.all())
    for rec in reclamaciones:
        rec.siguientes = [
            (estado, dict(ReclamacionCarrier.ESTADOS)[estado])
            for estado in sorted(ReclamacionCarrier.TRANSICIONES.get(rec.estado, set()))
        ]

    fotos_ids = set()
    if incidencia.pedido is not None:
        fotos_ids = {str(incidencia.pedido.pk), incidencia.pedido.folio}
    fotos = list(
        EvidenciaFoto.objects.filter(
            Q(entidad="incidencia", entidad_id__in={str(incidencia.pk), incidencia.folio})
            | Q(entidad="pedido", entidad_id__in=fotos_ids)
        )
    )

    return render(request, "mesa/incidencia_detalle.html", {
        "seccion": "incidencias",
        "incidencia": incidencia,
        "mensajes_timeline": incidencia.mensajes.all(),
        "compensaciones": compensaciones,
        "reclamaciones": reclamaciones,
        "tipos_compensacion": Compensacion.TIPOS,
        "fotos": fotos,
        "puede_tomar": incidencia.estado == Incidencia.ABIERTA,
        "puede_resolver": incidencia.estado in Incidencia.ESTADOS_ABIERTOS,
        "puede_cerrar": incidencia.estado == Incidencia.RESUELTA,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Pedidos globales
# ─────────────────────────────────────────────────────────────────────────────


def _pedidos_cancelar(request):
    """Cancela un pedido por folio vía la matriz de pedidos.services.cancelar.

    Regresa el mensaje de éxito según lo que de verdad pasó: cancelado directo,
    restock pendiente en piso o incidencia CAN por cancelación tardía.
    """
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import cancelar  # lazy por contrato

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    motivo = (request.POST.get("motivo") or "").strip()
    if not motivo:
        raise ValueError(
            "Escribe el motivo antes de cancelar: queda en el expediente del pedido."
        )
    cancelar(pedido, actor=request.user, motivo=motivo)
    registrar_evento(
        "pedido", pedido.folio, "cancelacion_solicitada_mesa",
        actor=request.user, cliente=pedido.cliente, motivo=motivo,
    )
    if pedido.estado == Pedido.CANCELADO:
        return f"{pedido.folio} cancelado y stock liberado."
    if pedido.estado == Pedido.CANCELACION_PENDIENTE:
        return (
            f"{pedido.folio} con cancelación pendiente: piso tiene que confirmar "
            "el restock para que quede cancelado."
        )
    return (
        f"{pedido.folio} ya está en la calle: se abrió incidencia CAN para gestionarlo."
    )


def _pedidos_reintentar_reservas(request):
    """Botón por renglón: reintenta las reservas de UN pedido (palanca de prioridad)."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import reintentar_reservas_pedido

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido, folio=folio)
    resultado = reintentar_reservas_pedido(pedido, request.user)
    completo = not pedido.lineas.filter(reservada=False).exists()
    return resultado, completo


@rol_requerido("mesa")
def pedidos(request):
    from apps.pedidos.models import Pedido

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "cancelar":
            try:
                exito = _pedidos_cancelar(request)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, exito)
        elif accion == "reintentar_reservas":
            try:
                exito, completo = _pedidos_reintentar_reservas(request)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                # Verde solo si el pedido quedó completo; parcial = warning
                # (amarillo): sigue faltando stock y Mesa debe verlo como tal.
                (messages.success if completo else messages.warning)(request, exito)
        else:
            messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
        # PRG conservando los filtros activos (patrón _redirect_inventario):
        # cancelar en tanda no debe rearmar el filtro cada vez.
        qs_filtros = request.GET.urlencode()
        destino = reverse("mesa:pedidos")
        return redirect(f"{destino}?{qs_filtros}" if qs_filtros else destino)

    qs = Pedido.objects.select_related("cliente", "tienda").order_by("-creado")
    cliente_id = request.GET.get("cliente", "").strip()
    estado = request.GET.get("estado", "").strip()
    q = request.GET.get("q", "").strip()
    if cliente_id:
        qs = qs.filter(cliente_id=cliente_id)
    if estado:
        qs = qs.filter(estado=estado)
    if q:
        qs = qs.filter(
            Q(folio__icontains=q)
            | Q(comprador_nombre__icontains=q)
            | Q(shopify_order_id__icontains=q)
        )

    filas = list(qs[:200])
    for pedido in filas:
        pedido.pill = PILL_PEDIDO.get(pedido.estado, "")
        pedido.cancelable = pedido.estado in ESTADOS_CANCELABLES_MESA

    return render(request, "mesa/pedidos.html", {
        "seccion": "pedidos",
        "pedidos": filas,
        "total": qs.count(),
        "estados": Pedido.ESTADOS,
        "clientes_filtro": Cliente.objects.all(),
        "filtro": {"cliente": cliente_id, "estado": estado, "q": q},
    })


@rol_requerido("mesa")
def pedido_nuevo(request):
    """Alta manual de un pedido: clientes sin Shopify (mayoreo, B2B, almacenaje).

    Dos pasos porque los SKUs dependen del cliente: primero se elige el
    cliente activo y luego se captura el pedido completo.
    """
    slug = (request.POST.get("cliente") or request.GET.get("cliente") or "").strip()
    if not slug:
        return render(request, "mesa/pedido_form.html", {
            "seccion": "pedidos",
            "cliente_sel": None,
            "clientes_activos": Cliente.objects.filter(activo=True).order_by("nombre"),
        })

    cliente = get_object_or_404(Cliente, slug=slug, activo=True)
    form = FormPedidoManual(cliente, request.POST or None)
    if request.method == "POST" and form.is_valid():
        from apps.pedidos.services import crear_pedido_manual  # lazy por contrato

        try:
            pedido = crear_pedido_manual(
                cliente,
                comprador_nombre=form.cleaned_data["comprador_nombre"],
                comprador_tel=form.cleaned_data.get("comprador_tel", ""),
                comprador_email=form.cleaned_data.get("comprador_email", ""),
                direccion=form.direccion(),
                cp=form.cleaned_data["cp"],
                valor_declarado=form.cleaned_data.get("valor_declarado"),
                nota_regalo=form.cleaned_data.get("nota_regalo", ""),
                lineas=form.cleaned_data["lineas"],
                actor=request.user,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(f"{reverse('mesa:pedido_nuevo')}?cliente={cliente.slug}")
        sin_stock = [linea for linea in pedido.lineas.all() if not linea.reservada]
        ruta = "entrega local" if pedido.es_local else "foráneo"
        exito = (
            f"{pedido.folio} creado para {cliente.nombre} ({ruta}, "
            f"peso estimado {pedido.peso_esperado_gr} g)."
        )
        if sin_stock:
            plural = "renglón" if len(sin_stock) == 1 else "renglones"
            exito += f" OJO: {len(sin_stock)} {plural} sin stock — se abrió incidencia FAL."
        messages.success(request, exito)
        return redirect(f"{reverse('mesa:pedidos')}?q={pedido.folio}")

    return render(request, "mesa/pedido_form.html", {
        "seccion": "pedidos",
        "cliente_sel": cliente,
        "form": form,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Inventario: stock por cliente, ajustes con doble firma y ubicaciones
# ─────────────────────────────────────────────────────────────────────────────

# Últimos movimientos del kardex visibles en el detalle de SKU (solo UI).
KARDEX_MESA_MOVIMIENTOS = 30


def _redirect_inventario(**params):
    """Redirect PRG a mesa:inventario conservando cliente/sku/ver."""
    from urllib.parse import urlencode

    url = reverse("mesa:inventario")
    limpios = {clave: valor for clave, valor in params.items() if valor}
    if limpios:
        url += "?" + urlencode(limpios)
    return redirect(url)


def _filas_inventario_mesa(cliente):
    """Resumen por SKU activo del cliente (servicio resumen_sku) + resurtido."""
    from apps.catalogo.models import SKU
    from apps.inventario.services import resumen_sku  # lazy por contrato

    filas = []
    for sku in SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo"):
        fila = resumen_sku(sku)
        fila["bajo_reorden"] = sku.punto_reorden > 0 and fila["disponible"] <= sku.punto_reorden
        fila["ultimo_conteo_hace"] = (
            _frescura(fila["ultimo_conteo"]) if fila["ultimo_conteo"] else "sin conteo aún"
        )
        filas.append(fila)
    return filas


@rol_requerido("mesa")
def inventario(request):
    """Inventario operativo: resumen por cliente, detalle de SKU con kardex y
    ajuste con doble firma, y catálogo de ubicaciones del Local 380 E.

    El stock solo se LEE aquí; toda mutación pasa por inventario.services.
    """
    from apps.catalogo.models import SKU, Ubicacion
    from apps.inventario.models import Ajuste, Movimiento, Saldo

    if request.method == "POST":
        accion = request.POST.get("accion", "")
        if accion == "ajustar":
            return _inventario_ajustar(request)
        if accion == "ubicacion_nueva":
            return _inventario_ubicacion_nueva(request)
        if accion == "ubicacion_toggle":
            return _inventario_ubicacion_toggle(request)
        if accion == "ubicacion_carriers":
            return _inventario_ubicacion_carriers(request)
        messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
        return redirect("mesa:inventario")

    contexto = {
        "seccion": "inventario",
        "clientes_activos": Cliente.objects.filter(activo=True).order_by("nombre"),
    }

    if request.GET.get("ver") == "ubicaciones":
        vivos = dict(
            Saldo.objects.filter(cantidad__gt=0)
            .values_list("ubicacion_id")
            .annotate(n=Count("id"))
            .values_list("ubicacion_id", "n")
        )
        ubicaciones = list(Ubicacion.objects.order_by("codigo"))
        for ubic in ubicaciones:
            ubic.saldos_vivos = vivos.get(ubic.pk, 0)
        contexto.update({
            "ver_ubicaciones": True,
            "ubicaciones": ubicaciones,
            "tipos_ubicacion": Ubicacion.TIPOS,
        })
        return render(request, "mesa/inventario.html", contexto)

    slug = (request.GET.get("cliente") or "").strip()
    if not slug:
        return render(request, "mesa/inventario.html", contexto)

    cliente = get_object_or_404(Cliente, slug=slug)
    contexto["cliente_sel"] = cliente

    codigo = (request.GET.get("sku") or "").strip()
    if codigo:
        from apps.inventario.services import resumen_sku  # lazy por contrato

        sku = get_object_or_404(SKU, cliente=cliente, codigo=codigo)
        contexto.update({
            "sku_detalle": sku,
            "resumen": resumen_sku(sku),
            "saldos": list(
                Saldo.objects.filter(sku=sku)
                .select_related("ubicacion", "lote")
                .order_by("ubicacion__codigo", "estado")
            ),
            "movimientos": list(
                Movimiento.objects.filter(sku=sku)
                .select_related("lote")[:KARDEX_MESA_MOVIMIENTOS]
            ),
            "motivos_ajuste": Ajuste.MOTIVOS,
        })
        return render(request, "mesa/inventario.html", contexto)

    filas = _filas_inventario_mesa(cliente)
    totales = {
        clave: sum(fila[clave] for fila in filas)
        for clave in (
            "fisico", "en_recepcion", "vendible", "apartado",
            "en_empaque", "cuarentena", "disponible",
        )
    }
    contexto.update({"filas": filas, "totales": totales})
    return render(request, "mesa/inventario.html", contexto)


def _inventario_ajustar(request):
    """Ajuste con doble firma desde el detalle de SKU (inventario.aplicar_ajuste)."""
    from apps.catalogo.models import SKU

    slug = (request.POST.get("cliente") or "").strip()
    codigo = (request.POST.get("sku") or "").strip()
    cliente = get_object_or_404(Cliente, slug=slug)
    sku = get_object_or_404(SKU, cliente=cliente, codigo=codigo)
    destino = _redirect_inventario(cliente=slug, sku=codigo)

    from apps.inventario.services import aplicar_ajuste  # lazy por contrato
    try:
        try:
            delta = int((request.POST.get("delta") or "").strip())
        except ValueError:
            raise ValueError("Captura el ajuste como número entero con signo (ej. -2 o 5).")
        ajuste = aplicar_ajuste(
            sku, delta, request.POST.get("motivo") or "",
            (request.POST.get("autorizo_1") or "").strip(), request.POST.get("pin_1") or "",
            (request.POST.get("autorizo_2") or "").strip(), request.POST.get("pin_2") or "",
            incidencia_ref=(request.POST.get("incidencia_ref") or "").strip(),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request,
        f"Ajuste {ajuste.folio} aplicado: {ajuste.delta:+d} {sku.codigo} ({ajuste.motivo}). "
        f"Firmaron {ajuste.autorizo_1} y {ajuste.autorizo_2}.",
    )
    return destino


def _inventario_ubicacion_nueva(request):
    """Alta de una ubicación física del Local 380 E, con auditoría."""
    from apps.catalogo.models import Ubicacion

    destino = _redirect_inventario(ver="ubicaciones")
    codigo = (request.POST.get("codigo") or "").strip().upper()
    tipo = request.POST.get("tipo") or ""
    carriers = _normalizar_carriers(request.POST.get("carriers"))
    try:
        if not codigo:
            raise ValueError("Captura el código de la ubicación (ej. A-02-1).")
        if tipo not in dict(Ubicacion.TIPOS):
            raise ValueError("Elige el tipo de ubicación del catálogo.")
        if carriers and tipo != Ubicacion.SALIDA:
            raise ValueError("Los carriers solo aplican a corrales (tipo salida).")
        if Ubicacion.objects.filter(codigo__iexact=codigo).exists():
            raise ValueError(f"Ya existe la ubicación {codigo}.")
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    ubicacion = Ubicacion.objects.create(codigo=codigo, tipo=tipo, carriers=carriers)
    registrar_evento(
        "ubicacion", ubicacion.codigo, "alta", actor=request.user,
        delta={"tipo": tipo, "carriers": carriers},
        motivo="Alta de ubicación desde Mesa de Control",
    )
    messages.success(
        request,
        f"Ubicación {ubicacion.codigo} dada de alta ({ubicacion.get_tipo_display()}).",
    )
    return destino


def _normalizar_carriers(crudo):
    """"a, b ,," → "a,b" (el orden se respeta)."""
    return ",".join(p.strip() for p in (crudo or "").split(",") if p.strip())


def _inventario_ubicacion_carriers(request):
    """Edita los carriers de un corral (tipo salida), con auditoría."""
    from apps.catalogo.models import Ubicacion

    destino = _redirect_inventario(ver="ubicaciones")
    ubicacion = get_object_or_404(Ubicacion, pk=request.POST.get("ubicacion_id"))
    if ubicacion.tipo != Ubicacion.SALIDA:
        messages.error(request, "Los carriers solo aplican a corrales (tipo salida).")
        return destino
    nuevos = _normalizar_carriers(request.POST.get("carriers"))
    viejos = ubicacion.carriers
    if nuevos == viejos:
        messages.info(request, f"{ubicacion.codigo} ya tenía esos carriers.")
        return destino
    ubicacion.carriers = nuevos
    ubicacion.save(update_fields=["carriers"])
    registrar_evento(
        "ubicacion", ubicacion.codigo, "carriers_actualizados", actor=request.user,
        delta={"antes": viejos, "ahora": nuevos},
        motivo="Editor de corrales de Mesa de Control",
    )
    messages.success(
        request,
        f"Corral {ubicacion.codigo}: {nuevos or 'comodín (todos los carriers no asignados)'}.",
    )
    return destino


def _inventario_ubicacion_toggle(request):
    """Prende/apaga una ubicación. Apagar exige que no queden saldos vivos."""
    from apps.catalogo.models import Ubicacion
    from apps.inventario.models import Saldo

    destino = _redirect_inventario(ver="ubicaciones")
    ubicacion = get_object_or_404(Ubicacion, pk=request.POST.get("ubicacion_id"))
    if ubicacion.activo:
        piezas = (
            Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0)
            .aggregate(t=Sum("cantidad"))["t"] or 0
        )
        if piezas > 0:
            messages.error(
                request,
                f"Esa ubicación todavía tiene {piezas} piezas; muévelas antes de apagarla.",
            )
            return destino
    ubicacion.activo = not ubicacion.activo
    ubicacion.save(update_fields=["activo"])
    accion = "activada" if ubicacion.activo else "desactivada"
    registrar_evento(
        "ubicacion", ubicacion.codigo, accion, actor=request.user,
        motivo=f"Ubicación {accion} desde Mesa de Control",
    )
    messages.success(request, f"Ubicación {ubicacion.codigo} {accion}.")
    return destino


# ─────────────────────────────────────────────────────────────────────────────
# Recepciones (ASN): tablero global + captura de avisos que llegan por WhatsApp
# ─────────────────────────────────────────────────────────────────────────────

# Proyección visual del estado de la ASN a la pill del sistema WOP.
PILL_ASN = {
    "ANUNCIADA": "",
    "EN_RECEPCION": "accent",
    "RECIBIDA": "warn",
    "CERRADA": "ok",
}


def _sla_recepcion(orden):
    """(texto, tono) del reloj SLA de la orden; ('', '') si aún no corre.

    Mismo cálculo simple que usa el piso, sobre las properties del modelo.
    """
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


def _decorar_asn(orden):
    orden.pill = PILL_ASN.get(orden.estado, "")
    orden.sla_texto, orden.sla_tono = _sla_recepcion(orden)
    lineas = list(orden.lineas.all())
    orden.total_anunciado = sum(l.cantidad_anunciada for l in lineas)
    orden.total_recibido = sum(l.cantidad_recibida + l.cantidad_danada for l in lineas)
    return orden


def _avisar_piso_asn(orden):
    """Web Push al piso: se anunció una recepción. Best-effort TOTAL: un push
    caído o sin VAPID JAMÁS rompe la captura de la ASN."""
    try:
        from apps.mensajeria import push  # lazy por contrato

        cita = (
            orden.fecha_compromiso.strftime("%d/%m")
            if orden.fecha_compromiso else "por confirmar"
        )
        push.enviar_push_a_rol(
            "piso",
            f"🚚 Recepción anunciada {orden.folio}",
            f"{orden.tarimas} tarimas · cita {cita}",
            url="/piso/recepciones/",
        )
    except Exception:
        pass


@rol_requerido("mesa")
def recepciones(request):
    """Tablero de ASNs de TODOS los clientes + alta en dos pasos.

    El aviso que llega por WhatsApp se captura aquí en ≤15 min: primero se
    elige el cliente (los SKUs dependen de él) y luego se llena el anuncio.
    """
    from apps.inventario.models import LineaASN, OrdenEntrada  # lazy: modelo de otra app

    slug = (request.POST.get("cliente") or request.GET.get("cliente") or "").strip()
    cliente = None
    if slug:
        cliente = get_object_or_404(Cliente, slug=slug, activo=True)

    form = None
    if cliente is not None:
        form = FormAnuncioASNMesa(cliente, request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            fecha = form.cleaned_data["fecha_compromiso"]
            tarimas = form.cleaned_data.get("tarimas") or 0
            orden = OrdenEntrada.objects.create(
                cliente=cliente, fecha_compromiso=fecha, tarimas=tarimas,
            )
            for sku, cantidad in form.cleaned_data["lineas"]:
                LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=cantidad)
            registrar_evento(
                "asn", orden.folio, "anunciada_mesa",
                actor=request.user, cliente=cliente,
                delta={
                    "fecha_compromiso": str(fecha),
                    "tarimas": tarimas,
                    "lineas": [
                        {"sku": sku.codigo, "cantidad": cantidad}
                        for sku, cantidad in form.cleaned_data["lineas"]
                    ],
                },
                motivo="Capturada por Mesa (aviso fuera del portal)",
            )
            _avisar_piso_asn(orden)
            cita = fecha.strftime("%d/%m/%Y")
            messages.success(
                request,
                f"ASN {orden.folio} capturada para {cliente.nombre} con cita el {cita}. "
                "El piso ya la ve en su lista de recepciones.",
            )
            return redirect("mesa:recepciones")

    estados_abiertos = [
        OrdenEntrada.ANUNCIADA, OrdenEntrada.EN_RECEPCION, OrdenEntrada.RECIBIDA,
    ]
    abiertas = [
        _decorar_asn(orden)
        for orden in OrdenEntrada.objects.filter(estado__in=estados_abiertos)
        .select_related("cliente").prefetch_related("lineas__sku")
    ]
    cerradas = [
        _decorar_asn(orden)
        for orden in OrdenEntrada.objects.filter(estado=OrdenEntrada.CERRADA)
        .select_related("cliente").prefetch_related("lineas__sku")[:10]
    ]

    return render(request, "mesa/recepciones.html", {
        "seccion": "recepciones",
        "abiertas": abiertas,
        "cerradas": cerradas,
        "cliente_captura": cliente,
        "form": form,
        "clientes_activos": Cliente.objects.filter(activo=True).order_by("nombre"),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Salud de sync
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def sync(request):
    from apps.integraciones.models import (
        PushInventarioPendiente, SyncLog, Tienda, WebhookEvento,
    )

    # El botón "correr push" se quitó (2026-08-11): la cola la drena el cron
    # cada 5 min y el drenado corre síncrono en el request — un click ansioso
    # amarraba un worker contra la API de Shopify. Manual: ssh + manage.py
    # push_inventario.
    ahora = timezone.now()
    tiendas = list(Tienda.objects.select_related("cliente").order_by("cliente__nombre", "dominio"))
    for tienda in tiendas:
        for direccion, attr in ((SyncLog.DIRECCION_INGESTA, "ingesta"), (SyncLog.DIRECCION_PUSH, "push")):
            log = tienda.sync_logs.filter(direccion=direccion).first()  # ordering: -ts
            frescura = _frescura(log.ts if log else None, ahora)
            if log is None:
                pill = "warn"
            elif log.resultado == SyncLog.RESULTADO_ERROR:
                pill = "crit"
            elif ahora - log.ts > timedelta(hours=4):
                pill = "warn"
            else:
                pill = "ok"
            setattr(tienda, f"log_{attr}", log)
            setattr(tienda, f"frescura_{attr}", frescura)
            setattr(tienda, f"pill_{attr}", pill)

    webhooks = list(
        WebhookEvento.objects.select_related("tienda", "tienda__cliente")[:15]
    )
    cola = list(
        PushInventarioPendiente.objects.select_related("sku", "sku__cliente").order_by("creado")
    )
    for pendiente in cola:
        pendiente.frescura = _frescura(pendiente.creado, ahora)

    return render(request, "mesa/sync.html", {
        "seccion": "sync",
        "tiendas": tiendas,
        "webhooks": webhooks,
        "cola": cola,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Clientes
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def clientes(request):
    from apps.incidencias.models import Incidencia

    lista = list(
        Cliente.objects.annotate(
            n_tiendas=Count("tiendas", distinct=True),
            n_skus=Count("skus", distinct=True),
            n_pedidos=Count("pedidos", distinct=True),
        ).order_by("nombre")
    )
    abiertas = dict(
        Incidencia.objects.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
        .values_list("cliente_id")
        .annotate(n=Count("id"))
    )
    for cliente in lista:
        cliente.n_incidencias_abiertas = abiertas.get(cliente.pk, 0)

    return render(request, "mesa/clientes.html", {"seccion": "clientes", "clientes": lista})


@rol_requerido("mesa")
def cliente_detalle(request, pk):
    from apps.envios.models import ReglaEnvio
    from apps.incidencias.models import Incidencia
    from apps.integraciones.models import SyncLog, Tienda
    from apps.mensajeria.models import PlantillaMensaje
    from apps.pedidos.models import Pedido

    cliente = get_object_or_404(Cliente, pk=pk)

    if request.method == "POST":
        try:
            exito = _gestionar_cliente(request, cliente)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, exito)
        return redirect("mesa:cliente_detalle", pk=cliente.pk)

    ahora = timezone.now()

    # Prefill del form de tienda cuando se pide editar una (?tienda=<pk>).
    tienda_editar = None
    tienda_id = request.GET.get("tienda", "").strip()
    if tienda_id:
        tienda_editar = get_object_or_404(Tienda, pk=tienda_id, cliente=cliente)

    tiendas = list(cliente.tiendas.all())
    for tienda in tiendas:
        log_ingesta = tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_INGESTA).first()
        log_push = tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_PUSH).first()
        tienda.frescura_ingesta = _frescura(log_ingesta.ts if log_ingesta else None, ahora)
        tienda.frescura_push = _frescura(log_push.ts if log_push else None, ahora)

    pedidos_recientes = list(
        Pedido.objects.filter(cliente=cliente).select_related("tienda").order_by("-creado")[:8]
    )
    for pedido in pedidos_recientes:
        pedido.pill = PILL_PEDIDO.get(pedido.estado, "")

    incidencias_abiertas = [
        _anotar_reloj_sla(inc, ahora)
        for inc in Incidencia.objects.filter(
            cliente=cliente, estado__in=Incidencia.ESTADOS_ABIERTOS
        ).select_related("pedido")
    ]

    return render(request, "mesa/cliente_detalle.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "tiendas": tiendas,
        "tienda_editar": tienda_editar,
        "usuarios": cliente.usuarios.select_related("usuario"),
        "n_skus": cliente.skus.count(),
        "pedidos_recientes": pedidos_recientes,
        "incidencias_abiertas": incidencias_abiertas,
        "reglas_envio": ReglaEnvio.objects.filter(Q(cliente=cliente) | Q(cliente__isnull=True)),
        "plantillas": PlantillaMensaje.objects.filter(Q(cliente=cliente) | Q(cliente__isnull=True)),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Gestión de clientes desde Mesa: alta/edición, tarifario, usuarios, catálogo
# ─────────────────────────────────────────────────────────────────────────────


def _serializar_delta(valor):
    """Valores del delta de auditoría: Decimal como str, el resto tal cual."""
    if isinstance(valor, Decimal):
        return str(valor)
    return valor


@rol_requerido("mesa")
def cliente_nuevo(request):
    form = FormCliente(request.POST or None)
    if request.method == "POST" and form.is_valid():
        datos = form.datos_cliente()
        cliente = Cliente.objects.create(
            slug=form.cleaned_data["slug"],
            branding=form.branding_actualizado(),
            **datos,
        )
        registrar_evento(
            "cliente", cliente.slug, "alta", actor=request.user, cliente=cliente,
            motivo="Alta desde Mesa de Control",
        )
        messages.success(
            request,
            "Cliente creado. Siguiente: carga sus SKUs, crea su usuario del portal "
            "y revisa su tarifario.",
        )
        return redirect("mesa:cliente_detalle", pk=cliente.pk)
    return render(request, "mesa/cliente_form.html", {
        "seccion": "clientes",
        "form": form,
        "cliente": None,
    })


@rol_requerido("mesa")
def cliente_editar(request, pk):
    cliente = get_object_or_404(Cliente, pk=pk)
    branding = cliente.branding or {}
    inicial = {
        "nombre": cliente.nombre,
        "slug": cliente.slug,
        "razon_social": cliente.razon_social,
        "rfc": cliente.rfc,
        "contacto_nombre": cliente.contacto_nombre,
        "contacto_whatsapp": cliente.contacto_whatsapp,
        "buffer_stock": cliente.buffer_stock,
        "carrier_preferente": cliente.carrier_preferente,
        "integracion_envios": cliente.integracion_envios,
        "naked_packing_local": cliente.naked_packing_local,
        "umbral_visto_bueno_mxn": cliente.umbral_visto_bueno_mxn,
        "guia_de_voz": cliente.guia_de_voz,
        "activo": cliente.activo,
        **{f"brand_{clave}": branding.get(clave, "") for clave in (
            "color_primario", "color_fondo", "logo_url",
            "nombre_publico", "whatsapp_soporte", "dominio_tienda",
        )},
    }
    form = FormCliente(request.POST or None, cliente=cliente, initial=inicial)
    if request.method == "POST" and form.is_valid():
        datos = form.datos_cliente()
        datos["branding"] = form.branding_actualizado(base=cliente.branding)
        delta = {}
        for campo, nuevo in datos.items():
            viejo = getattr(cliente, campo)
            if viejo != nuevo:
                delta[campo] = [_serializar_delta(viejo), _serializar_delta(nuevo)]
                setattr(cliente, campo, nuevo)
        if delta:
            cliente.save()
            registrar_evento(
                "cliente", cliente.slug, "edicion", actor=request.user, cliente=cliente,
                delta=delta, motivo="Edición desde Mesa de Control",
            )
            messages.success(request, f"Cliente actualizado: cambiaron {len(delta)} campo(s).")
        else:
            messages.success(request, "Sin cambios que guardar: la ficha ya estaba así.")
        return redirect("mesa:cliente_detalle", pk=cliente.pk)
    return render(request, "mesa/cliente_form.html", {
        "seccion": "clientes",
        "form": form,
        "cliente": cliente,
    })


@rol_requerido("mesa")
def cliente_tarifario(request, pk):
    from apps.mesa.finanzas import tarifario_de  # lazy por contrato

    cliente = get_object_or_404(Cliente, pk=pk)
    default = settings.TORRE["TARIFARIO_DEFAULT"]
    override = cliente.tarifario or {}

    if request.method == "POST":
        form = FormTarifario(request.POST)
        if form.is_valid():
            viejo = cliente.tarifario or {}
            nuevo = form.overrides()
            cliente.tarifario = nuevo
            cliente.save(update_fields=["tarifario"])
            registrar_evento(
                "cliente", cliente.slug, "tarifario_actualizado", actor=request.user,
                cliente=cliente, delta={"antes": viejo, "despues": nuevo},
                motivo="Editor de tarifario de Mesa de Control",
            )
            messages.success(
                request,
                "Tarifario guardado. Solo quedan como propias las tarifas que "
                "difieren del default; el resto sigue el tarifario general.",
            )
            return redirect("mesa:cliente_tarifario", pk=cliente.pk)
    else:
        efectivo = tarifario_de(cliente)
        inicial = {
            campo: efectivo.get(campo, default.get(campo, 0))
            for campo in CAMPOS_TARIFARIO_SIMPLES
        }
        for zona in ZONAS_ENVIO:
            inicial[f"envio_{zona}"] = efectivo.get("envio_bloque", {}).get(
                zona, default.get("envio_bloque", {}).get(zona, 0)
            )
        form = FormTarifario(initial=inicial)

    # Filas para el template: campo + default (help) + pill propio/default.
    filas = []
    for campo in CAMPOS_TARIFARIO_SIMPLES:
        defecto = default.get(campo, 0)
        form.fields[campo].help_text = f"(default: ${defecto})"
        filas.append({"campo": form[campo], "propio": campo in override})
    envio_override = override.get("envio_bloque", {})
    for zona in ZONAS_ENVIO:
        defecto = default.get("envio_bloque", {}).get(zona, 0)
        form.fields[f"envio_{zona}"].help_text = f"(default: ${defecto})"
        filas.append({"campo": form[f"envio_{zona}"], "propio": zona in envio_override})

    return render(request, "mesa/cliente_tarifario.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "form": form,
        "filas": filas,
        "tiene_overrides": bool(override),
    })


def _usuario_portal_del_cliente(request, cliente):
    """Usuario portal referido por usuario_id, validado contra el cliente."""
    usuario_id = request.POST.get("usuario_id", "")
    usuario = get_user_model().objects.filter(pk=usuario_id or None).first()
    perfil = getattr(usuario, "perfil", None) if usuario else None
    if (
        usuario is None
        or perfil is None
        or perfil.rol != PerfilUsuario.ROL_PORTAL
        or perfil.cliente_id != cliente.pk
    ):
        raise ValueError("Ese usuario no es del portal de este cliente. Recarga la página e intenta de nuevo.")
    return usuario


def _gestionar_cliente(request, cliente):
    """Despacha la acción POST de la ficha del cliente (usuarios y tiendas).

    Regresa mensaje de éxito o lanza ValueError con copy claro.
    """
    accion = request.POST.get("accion", "")

    if accion == "usuario_nuevo":
        username = (request.POST.get("username") or "").strip()
        if not username:
            raise ValueError("Captura el nombre de usuario para crear el acceso.")
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            raise ValueError(f"El usuario '{username}' ya existe. Elige otro nombre de usuario.")
        nombre = (request.POST.get("nombre") or "").strip()
        partes = nombre.split(" ", 1)
        email = (request.POST.get("email") or "").strip() or f"{username}@torre380e.mx"
        password = (request.POST.get("password") or "").strip() or secrets.token_urlsafe(8)
        usuario = User(
            username=username,
            first_name=partes[0] if nombre else "",
            last_name=partes[1] if len(partes) > 1 else "",
            email=email,
        )
        usuario.set_password(password)
        usuario.save()
        PerfilUsuario.objects.create(
            usuario=usuario, rol=PerfilUsuario.ROL_PORTAL, cliente=cliente
        )
        registrar_evento(
            "usuario_portal", username, "alta", actor=request.user, cliente=cliente,
            motivo="Alta de usuario del portal desde Mesa de Control",
        )
        return (
            f"Usuario {username} creado. Contraseña: {password} — compártela por "
            "canal seguro, no se vuelve a mostrar."
        )

    if accion == "usuario_reset":
        usuario = _usuario_portal_del_cliente(request, cliente)
        password = secrets.token_urlsafe(8)
        usuario.set_password(password)
        usuario.save(update_fields=["password"])
        registrar_evento(
            "usuario_portal", usuario.username, "reset_password", actor=request.user,
            cliente=cliente, motivo="Reset de contraseña desde Mesa de Control",
        )
        return (
            f"Contraseña de {usuario.username} regenerada. Contraseña: {password} — "
            "compártela por canal seguro, no se vuelve a mostrar."
        )

    if accion == "usuario_toggle":
        usuario = _usuario_portal_del_cliente(request, cliente)
        usuario.is_active = not usuario.is_active
        usuario.save(update_fields=["is_active"])
        estado = "activado" if usuario.is_active else "desactivado"
        registrar_evento(
            "usuario_portal", usuario.username, estado, actor=request.user,
            cliente=cliente, motivo="Cambio de acceso desde Mesa de Control",
        )
        return f"Usuario {usuario.username} {estado}."

    if accion == "tienda_guardar":
        return _guardar_tienda(request, cliente)

    raise ValueError("Acción desconocida. Recarga la página e intenta de nuevo.")


def _guardar_tienda(request, cliente):
    """Alta o edición (por tienda_id oculto) de una tienda Shopify del cliente."""
    from apps.integraciones.models import Tienda

    dominio = (request.POST.get("dominio") or "").strip().lower()
    if not dominio:
        raise ValueError("Captura el dominio de la tienda (ej. marca.myshopify.com).")
    token = (request.POST.get("token") or "").strip()
    webhook_secret = (request.POST.get("webhook_secret") or "").strip()
    location_id = (request.POST.get("location_id") or "").strip()
    activo = request.POST.get("activo") == "on"

    tienda_id = (request.POST.get("tienda_id") or "").strip()
    duplicada = Tienda.objects.filter(dominio=dominio)
    if tienda_id:
        tienda = get_object_or_404(Tienda, pk=tienda_id, cliente=cliente)
        if duplicada.exclude(pk=tienda.pk).exists():
            raise ValueError(f"Ya hay otra tienda conectada con el dominio {dominio}.")
        delta = {}
        for campo, nuevo in (
            ("dominio", dominio), ("location_id", location_id), ("activo", activo),
        ):
            viejo = getattr(tienda, campo)
            if viejo != nuevo:
                delta[campo] = [viejo, nuevo]
                setattr(tienda, campo, nuevo)
        # Credenciales vacías en edición = conservar las actuales: el form
        # nunca las prefillea (solo muestra su cola de referencia).
        if token and tienda.token != token:
            tienda.token = token
            delta["token"] = "actualizado (el valor no se registra)"
        if webhook_secret and tienda.webhook_secret != webhook_secret:
            tienda.webhook_secret = webhook_secret
            delta["webhook_secret"] = "actualizado (el valor no se registra)"
        tienda.save()
        registrar_evento(
            "tienda", tienda.dominio, "edicion", actor=request.user, cliente=cliente,
            delta=delta, motivo="Edición de tienda desde Mesa de Control",
        )
        return f"Tienda {tienda.dominio} actualizada."

    if duplicada.exists():
        raise ValueError(f"Ya hay una tienda conectada con el dominio {dominio}.")
    tienda = Tienda.objects.create(
        cliente=cliente, dominio=dominio, token=token,
        webhook_secret=webhook_secret, location_id=location_id, activo=activo,
    )
    registrar_evento(
        "tienda", tienda.dominio, "alta", actor=request.user, cliente=cliente,
        delta={"tienda_id": tienda.pk}, motivo="Alta de tienda desde Mesa de Control",
    )
    return (
        f"Tienda {tienda.dominio} conectada. Pega el webhook en Shopify: "
        f"/hooks/shopify/{tienda.pk}/."
    )


def _cajas_mover(request, cliente):
    """Ledger de cajas: entrada al rack o restock rack → packing, con evento."""
    from apps.catalogo.models import Caja, CajaStock
    from apps.core.services import registrar_evento

    caja = get_object_or_404(Caja, pk=request.POST.get("caja_id"), cliente=cliente)
    try:
        cantidad = int(request.POST.get("cantidad") or 0)
    except (TypeError, ValueError):
        cantidad = 0
    destino = redirect("mesa:cliente_cajas", pk=cliente.pk)
    if cantidad <= 0:
        messages.error(request, "Captura cuántas cajas mueves (entero mayor a cero).")
        return destino
    rack, _ = CajaStock.objects.get_or_create(caja=caja, zona=CajaStock.RACK)
    packing, _ = CajaStock.objects.get_or_create(caja=caja, zona=CajaStock.PACKING)
    if request.POST["accion"] == "entrada_rack":
        rack.cantidad += cantidad
        rack.save(update_fields=["cantidad"])
        accion = "cajas_entrada"
        motivo = f"Entraron {cantidad} caja(s) {caja.nombre} al rack."
    else:
        if rack.cantidad < cantidad:
            messages.error(
                request,
                f"Solo hay {rack.cantidad} caja(s) {caja.nombre} en rack; "
                f"no alcanza para mover {cantidad}.",
            )
            return destino
        rack.cantidad -= cantidad
        packing.cantidad += cantidad
        rack.save(update_fields=["cantidad"])
        packing.save(update_fields=["cantidad"])
        accion = "cajas_restock_packing"
        motivo = f"Restock: {cantidad} caja(s) {caja.nombre} del rack a packing."
    messages.success(request, motivo)
    registrar_evento(
        "caja", caja.pk, accion, actor=request.user, cliente=cliente,
        delta={"cantidad": cantidad, "rack": rack.cantidad, "packing": packing.cantidad},
        motivo=motivo,
    )
    return destino


@rol_requerido("mesa")
def cliente_cajas(request, pk):
    """Catálogo de cajas de empaque del cliente: lista + alta/edición."""
    from apps.catalogo.models import Caja
    from .forms import FormCaja

    cliente = get_object_or_404(Cliente, pk=pk)
    if request.method == "POST" and request.POST.get("accion") in ("entrada_rack", "mover_packing"):
        return _cajas_mover(request, cliente)
    if request.method == "POST":
        form = FormCaja(request.POST)
        if form.is_valid():
            datos = form.datos_caja()
            caja_id = form.cleaned_data.get("caja_id")
            existente = Caja.objects.filter(cliente=cliente, nombre=datos["nombre"])
            if caja_id:
                existente = existente.exclude(pk=caja_id)
            if existente.exists():
                messages.error(request, f"Ya hay una caja llamada {datos['nombre']}.")
            elif caja_id:
                Caja.objects.filter(pk=caja_id, cliente=cliente).update(**datos)
                messages.success(request, f"Caja {datos['nombre']} actualizada.")
            else:
                Caja.objects.create(cliente=cliente, **datos)
                messages.success(request, f"Caja {datos['nombre']} registrada.")
            return redirect("mesa:cliente_cajas", pk=cliente.pk)
        messages.error(request, "Revisa los campos de la caja: nombre, medidas y tara.")
    form = FormCaja()
    editar = None
    if request.GET.get("editar"):
        editar = Caja.objects.filter(pk=request.GET["editar"], cliente=cliente).first()
        if editar is not None:
            form = FormCaja(initial={
                "caja_id": editar.pk, "nombre": editar.nombre,
                "largo_cm": editar.largo_cm, "ancho_cm": editar.ancho_cm,
                "alto_cm": editar.alto_cm, "peso_gr": editar.peso_gr,
                "posicion_rack": editar.posicion_rack,
                "activo": editar.activo,
            })
    cajas = list(Caja.objects.filter(cliente=cliente).prefetch_related("stock"))
    for c in cajas:
        c.en_rack = next((x.cantidad for x in c.stock.all() if x.zona == "rack"), 0)
        c.en_packing = next((x.cantidad for x in c.stock.all() if x.zona == "packing"), 0)
    return render(request, "mesa/cliente_cajas.html", {
        "cliente": cliente,
        "cajas": cajas,
        "form": form,
        "editar": editar,
    })


@rol_requerido("mesa")
def cliente_skus(request, pk):
    from apps.catalogo.models import SKU
    from apps.inventario.services import resumen_sku  # lazy por contrato

    cliente = get_object_or_404(Cliente, pk=pk)

    form = None
    if request.method == "POST":
        accion = request.POST.get("accion", "")
        if accion == "importar_csv":
            try:
                exito = _importar_csv_skus(request, cliente)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, exito)
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "transformar_shopify":
            try:
                contexto = _transformar_shopify(request, cliente)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("mesa:cliente_skus", pk=cliente.pk)
            return render(request, "mesa/cliente_skus_transformar.html", contexto)
        if accion == "descargar_transformado":
            return _descargar_transformado(request, cliente)
        if accion == "categoria_nueva":
            try:
                exito = _skus_categoria_nueva(request, cliente)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, exito)
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "categorias_bulk":
            messages.success(request, _skus_categorias_bulk(request, cliente))
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "sku_guardar":
            form = FormSKU(cliente, request.POST)
            if form.is_valid():
                try:
                    exito = _guardar_sku(request, cliente, form)
                except ValueError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, exito)
                return redirect("mesa:cliente_skus", pk=cliente.pk)
            # Form inválido: cae al render de abajo con sus errores.
        else:
            messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
            return redirect("mesa:cliente_skus", pk=cliente.pk)

    # Link al admin de Shopify (por SKU): con la primera tienda activa basta —
    # la búsqueda por query encuentra el producto sin guardar product_id.
    tienda_principal = cliente.tiendas.filter(activo=True).first()
    sku_editar = None
    if form is None:
        editar_id = request.GET.get("editar", "").strip()
        if editar_id:
            sku_editar = get_object_or_404(SKU, pk=editar_id, cliente=cliente)
            form = FormSKU(cliente, initial={
                "sku_id": sku_editar.pk,
                "categoria": sku_editar.categoria_id,
                "codigo": sku_editar.codigo,
                "descripcion": sku_editar.descripcion,
                "variante": sku_editar.variante,
                "codigo_barras": sku_editar.codigo_barras,
                "peso_gr": sku_editar.peso_gr,
                "largo_cm": sku_editar.largo_cm,
                "ancho_cm": sku_editar.ancho_cm,
                "alto_cm": sku_editar.alto_cm,
                "requiere_lote": sku_editar.requiere_lote,
                "unidad": sku_editar.unidad,
                "precio_declarado": sku_editar.precio_declarado,
                "punto_reorden": sku_editar.punto_reorden,
                "empaques_divisibles": sku_editar.empaques_divisibles,
                "backorder_habilitado": sku_editar.backorder_habilitado,
                "es_kit": sku_editar.es_kit,
                "productos_por_kit": sku_editar.productos_por_kit,
                "usa_caja_propia": sku_editar.usa_caja_propia,
                "activo": sku_editar.activo,
            })
        else:
            form = FormSKU(cliente)

    from apps.catalogo.models import Categoria
    otros = Categoria.otros_de(cliente)
    skus = list(SKU.objects.filter(cliente=cliente).select_related("categoria").order_by("codigo"))
    for sku in skus:
        sku.disponible = resumen_sku(sku)["disponible"]
        sku.categoria_efectiva_id = sku.categoria_id or otros.pk

    return render(request, "mesa/cliente_skus.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "skus": skus,
        "form": form,
        "sku_editar": sku_editar,
        "tienda_principal": tienda_principal,
        "categorias": list(Categoria.objects.filter(cliente=cliente)),
    })


def _skus_categoria_nueva(request, cliente):
    """Alta de una categoría del catálogo del cliente, con auditoría."""
    from apps.catalogo.models import Categoria

    nombre = (request.POST.get("nombre") or "").strip()
    if not nombre:
        raise ValueError("Captura el nombre de la categoría.")
    if Categoria.objects.filter(cliente=cliente, nombre__iexact=nombre).exists():
        raise ValueError(f"Ya existe la categoría {nombre}.")
    categoria = Categoria.objects.create(cliente=cliente, nombre=nombre)
    registrar_evento(
        "categoria", f"{cliente.slug}:{categoria.nombre}", "alta", actor=request.user,
        cliente=cliente, motivo="Alta de categoría desde Mesa de Control",
    )
    return f"Categoría {categoria.nombre} dada de alta."


def _skus_categorias_bulk(request, cliente):
    """Guarda las categorías elegidas en la tabla del catálogo (edición masiva)."""
    from apps.catalogo.models import SKU, Categoria

    validas = {str(c.pk): c for c in Categoria.objects.filter(cliente=cliente)}
    cambios = []
    for sku in SKU.objects.filter(cliente=cliente).select_related("categoria"):
        elegida = validas.get(request.POST.get(f"cat_{sku.pk}", ""))
        if elegida is None or sku.categoria_id == elegida.pk:
            continue
        antes = sku.categoria.nombre if sku.categoria else Categoria.OTROS
        sku.categoria = elegida
        sku.save(update_fields=["categoria"])
        cambios.append({"sku": sku.codigo, "antes": antes, "ahora": elegida.nombre})
    if cambios:
        registrar_evento(
            "sku", "categorias_bulk", "categorias_actualizadas", actor=request.user,
            cliente=cliente, delta={"cambios": cambios},
            motivo="Edición masiva de categorías desde el catálogo",
        )
        return f"{len(cambios)} producto(s) recategorizado(s)."
    return "Sin cambios de categoría."


def _guardar_sku(request, cliente, form):
    """Alta o edición (por sku_id oculto) de un SKU. Unicidad por (cliente, codigo)."""
    from apps.catalogo.models import SKU

    datos = form.datos_sku()
    sku_id = form.cleaned_data.get("sku_id")
    duplicado = SKU.objects.filter(cliente=cliente, codigo=datos["codigo"])
    if sku_id:
        sku = get_object_or_404(SKU, pk=sku_id, cliente=cliente)
        if duplicado.exclude(pk=sku.pk).exists():
            raise ValueError(
                f"Ya existe el SKU '{datos['codigo']}' para {cliente.nombre}; "
                "los códigos no se repiten por cliente."
            )
        for campo, valor in datos.items():
            setattr(sku, campo, valor)
        sku.save()
        registrar_evento(
            "sku", sku.codigo, "edicion", actor=request.user, cliente=cliente,
            motivo="Edición de SKU desde Mesa de Control",
        )
        return f"SKU {sku.codigo} actualizado."

    if duplicado.exists():
        raise ValueError(
            f"Ya existe el SKU '{datos['codigo']}' para {cliente.nombre}; "
            "los códigos no se repiten por cliente."
        )
    sku = SKU.objects.create(cliente=cliente, **datos)
    registrar_evento(
        "sku", sku.codigo, "alta", actor=request.user, cliente=cliente,
        motivo="Alta de SKU desde Mesa de Control",
    )
    return f"SKU {sku.codigo} dado de alta."


# Columnas enteras opcionales del CSV de catálogo.
_COLUMNAS_CSV_ENTERAS = (
    "peso_gr", "largo_cm", "ancho_cm", "alto_cm", "punto_reorden", "empaques_divisibles",
)

# Contrato del CSV de catálogo (export e import hablan el MISMO formato:
# exportar → editar en Excel → importar sin fricción).
COLUMNAS_CSV_CATALOGO = (
    "codigo", "descripcion", "variante", "codigo_barras", "categoria",
    "peso_gr", "largo_cm", "ancho_cm", "alto_cm",
    "precio_declarado", "punto_reorden", "requiere_lote", "empaques_divisibles", "es_kit",
)


def _transformar_shopify(request, cliente):
    """Convierte el products_export.csv de Shopify al CSV del import (preview)."""
    from apps.catalogo.transformador import filas_a_csv, transformar_export_shopify

    archivo = request.FILES.get("archivo")
    if archivo is None:
        raise ValueError("Adjunta el export de productos de Shopify (CSV).")
    max_mb = settings.TORRE["IMPORT_CSV_MAX_MB"]
    if archivo.size > max_mb * 1024 * 1024:
        raise ValueError(f"El CSV no debe pasar de {max_mb} MB.")
    try:
        texto = archivo.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(
            "El archivo no se pudo leer. Descárgalo de Shopify sin modificarlo y súbelo de nuevo."
        )
    resultado = transformar_export_shopify(texto)
    return {
        "seccion": "clientes",
        "cliente": cliente,
        "avisos": resultado["avisos"],
        "filas": resultado["filas"][:50],
        "total": len(resultado["filas"]),
        "csv_transformado": filas_a_csv(resultado["filas"]),
    }


def _descargar_transformado(request, cliente):
    """Descarga del CSV ya transformado (viaja en el hidden del preview: sin estado)."""
    contenido = request.POST.get("csv_transformado") or ""
    if not contenido.strip():
        messages.error(request, "No llegó el CSV transformado; vuelve a subir el export.")
        return redirect("mesa:cliente_skus", pk=cliente.pk)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = (
        f'attachment; filename="catalogo-{cliente.slug}-desde-shopify.csv"'
    )
    respuesta.write("﻿")
    respuesta.write(contenido)
    return respuesta


@rol_requerido("mesa")
def cliente_skus_exportar(request, pk):
    """Catálogo completo del cliente en el formato del import (round-trip)."""
    from apps.catalogo.models import SKU, Categoria

    cliente = get_object_or_404(Cliente, pk=pk)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = f'attachment; filename="catalogo-{cliente.slug}.csv"'
    respuesta.write("﻿")  # BOM: Excel abre los acentos bien (espejo del decode)
    escritor = csv.writer(respuesta)
    escritor.writerow(COLUMNAS_CSV_CATALOGO)
    for sku in SKU.objects.filter(cliente=cliente).select_related("categoria").order_by("codigo"):
        escritor.writerow([
            sku.codigo, sku.descripcion, sku.variante, sku.codigo_barras,
            sku.categoria.nombre if sku.categoria else Categoria.OTROS,
            sku.peso_gr, sku.largo_cm, sku.ancho_cm, sku.alto_cm,
            sku.precio_declarado, sku.punto_reorden,
            "si" if sku.requiere_lote else "no", sku.empaques_divisibles,
            "si" if sku.es_kit else "no",
        ])
    return respuesta


def _parsear_fila_csv(fila):
    """Valida una fila del CSV y regresa los campos presentes. ValueError si algo falla."""
    def celda(clave):
        return (fila.get(clave) or "").strip()

    codigo = celda("codigo")
    descripcion = celda("descripcion")
    if not codigo:
        raise ValueError("falta el codigo")
    if not descripcion:
        raise ValueError("falta la descripcion")
    datos = {"codigo": codigo, "descripcion": descripcion}
    if celda("variante"):
        datos["variante"] = celda("variante")
    if celda("codigo_barras"):
        datos["codigo_barras"] = celda("codigo_barras")
    for campo in _COLUMNAS_CSV_ENTERAS:
        crudo = celda(campo)
        if not crudo:
            continue
        try:
            valor = int(crudo)
        except ValueError:
            raise ValueError(f"{campo} no es número")
        if campo == "empaques_divisibles" and valor < 1:
            raise ValueError("empaques_divisibles es mínimo 1")
        if valor < 0:
            raise ValueError(f"{campo} no puede ser negativo")
        datos[campo] = valor
    precio = celda("precio_declarado")
    if precio:
        try:
            valor = Decimal(precio)
        except InvalidOperation:
            raise ValueError("precio_declarado no es número")
        # "NaN"/"Infinity" parsean como Decimal sin excepción, pero no son
        # montos: comparar o guardar truena después. Misma fila con error.
        if not valor.is_finite():
            raise ValueError("precio_declarado no es número")
        if valor < 0:
            raise ValueError("precio_declarado no puede ser negativo")
        datos["precio_declarado"] = valor
    lote = celda("requiere_lote").lower()
    if lote:
        if lote in ("si", "sí", "1"):
            datos["requiere_lote"] = True
        elif lote in ("no", "0"):
            datos["requiere_lote"] = False
        else:
            raise ValueError("requiere_lote acepta si/no/1/0")
    kit = celda("es_kit").lower()
    if kit:
        if kit in ("si", "sí", "1"):
            datos["es_kit"] = True
        elif kit in ("no", "0"):
            datos["es_kit"] = False
        else:
            raise ValueError("es_kit acepta si/no/1/0")
    if celda("categoria"):
        datos["categoria_nombre"] = celda("categoria")
    return datos


def _categoria_por_nombre(cliente, nombre):
    """Categoría del cliente por nombre (insensible a mayúsculas); se crea si no existe."""
    from apps.catalogo.models import Categoria

    existente = Categoria.objects.filter(cliente=cliente, nombre__iexact=nombre).first()
    return existente or Categoria.objects.create(cliente=cliente, nombre=nombre)


def _importar_csv_skus(request, cliente):
    """Import masivo de catálogo: update_or_create por (cliente, codigo).

    Las filas con error no detienen a las demás. Un solo evento de auditoría
    resume el import completo. Todo corre dentro de una transacción: un error
    inesperado revierte el import entero — nunca mutación de catálogo sin evento.
    """
    from apps.catalogo.models import SKU, Categoria

    archivo = request.FILES.get("archivo")
    if archivo is None:
        raise ValueError("Adjunta el archivo CSV con el catálogo.")
    max_mb = settings.TORRE["IMPORT_CSV_MAX_MB"]
    if archivo.size > max_mb * 1024 * 1024:
        raise ValueError(f"El CSV no debe pasar de {max_mb} MB.")
    with transaction.atomic():
        try:
            texto = archivo.read().decode("utf-8-sig")  # BOM de Excel incluido
        except UnicodeDecodeError:
            raise ValueError("El archivo no se pudo leer. Guárdalo como CSV UTF-8 y súbelo de nuevo.")
        lector = csv.DictReader(io.StringIO(texto))
        # Encabezados con espacios ("codigo, descripcion"): normalizarlos en el
        # lector muta las claves con las que DictReader arma TODAS las filas.
        lector.fieldnames = [e.strip() for e in (lector.fieldnames or [])]
        if "codigo" not in lector.fieldnames or "descripcion" not in lector.fieldnames:
            raise ValueError(
                "El CSV debe traer al menos las columnas codigo y descripcion "
                "(revisa el renglón de encabezados)."
            )

        max_filas = settings.TORRE["IMPORT_CSV_MAX_FILAS"]
        creados = 0
        actualizados = 0
        errores = []
        # La fila 1 es el encabezado: los datos empiezan en la fila 2 (como en Excel).
        for numero, fila in enumerate(lector, start=2):
            if numero - 1 > max_filas:
                # El ValueError revienta el atomic: no se importa nada a medias.
                raise ValueError(
                    f"El CSV trae más de {max_filas} filas de datos; "
                    "divídelo en archivos más chicos y súbelos por partes."
                )
            try:
                datos = _parsear_fila_csv(fila)
            except ValueError as exc:
                errores.append(f"fila {numero}: {exc}")
                continue
            codigo = datos.pop("codigo")
            nombre_categoria = datos.pop("categoria_nombre", "")
            if nombre_categoria:
                datos["categoria"] = _categoria_por_nombre(cliente, nombre_categoria)
            sku, creado = SKU.objects.update_or_create(
                cliente=cliente, codigo=codigo, defaults=datos
            )
            if creado and sku.categoria_id is None:
                sku.categoria = Categoria.otros_de(cliente)
                sku.save(update_fields=["categoria"])
            if creado:
                creados += 1
            else:
                actualizados += 1

        registrar_evento(
            "sku", "import_csv", "import_csv", actor=request.user, cliente=cliente,
            delta={"creados": creados, "actualizados": actualizados, "errores": errores},
            motivo=f"Import de catálogo por CSV ({archivo.name})",
        )
    resumen = f"{creados} SKUs creados, {actualizados} actualizados"
    if errores:
        plural = "s" if len(errores) != 1 else ""
        resumen += f", {len(errores)} fila{plural} con error ({'; '.join(errores)})"
    return resumen + "."

# ─────────────────────────────────────────────────────────────────────────────
# Finanzas: factura por tarifario vs costos reales (visibilidad de margen)
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def finanzas(request):
    """Estado de resultados del mes: cuánto facturamos, cuánto nos costó.

    El ingreso se simula con el tarifario vigente (settings + override por
    cliente); el costo de envío es el REAL de las guías del mes. Los fijos
    (renta, sueldos, servicios) se restan una sola vez, a nivel global.
    """
    from datetime import date, datetime

    from apps.mesa import finanzas as motor

    hoy = timezone.localdate()
    try:
        anio, mes = (int(p) for p in (request.GET.get("mes") or "").split("-"))
        date(anio, mes, 1)
    except (ValueError, TypeError):
        anio, mes = hoy.year, hoy.month

    tz = timezone.get_current_timezone()
    inicio = datetime(anio, mes, 1, tzinfo=tz)
    fin = datetime(anio + 1, 1, 1, tzinfo=tz) if mes == 12 else datetime(anio, mes + 1, 1, tzinfo=tz)
    mes_prev = f"{anio - 1}-12" if mes == 1 else f"{anio}-{mes - 1:02d}"
    mes_sig = f"{anio + 1}-01" if mes == 12 else f"{anio}-{mes + 1:02d}"

    # Activos siempre; inactivos solo si tuvieron guías o recepciones ese mes
    # (el histórico no se reescribe cuando un cliente churnea; un mes de SOLO
    # recepción también es facturable).
    from apps.inventario.models import OrdenEntrada  # lazy por contrato

    clientes = (
        Cliente.objects.filter(
            Q(activo=True)
            | Q(pedidos__guias__creado__gte=inicio, pedidos__guias__creado__lt=fin)
            | Q(
                ordenes_entrada__ts_descarga_fin__gte=inicio,
                ordenes_entrada__ts_descarga_fin__lt=fin,
                ordenes_entrada__estado__in=(OrdenEntrada.RECIBIDA, OrdenEntrada.CERRADA),
            )
        )
        .distinct()
        .order_by("nombre")
    )
    filas = [motor.resumen_mes(cliente, inicio, fin) for cliente in clientes]

    torre = settings.TORRE
    ingreso_total = sum((f["ingresos"]["total"] for f in filas), Decimal("0"))
    costo_envio = sum((f["costos"]["carrier"] for f in filas), Decimal("0"))
    costo_insumos = sum((f["costos"]["insumos"] for f in filas), Decimal("0"))
    margen_bruto = sum((f["margen_bruto"] for f in filas), Decimal("0"))

    # Desglose por estado destino (todas las marcas), con datos reales de guías.
    acumulado = {}
    for f in filas:
        for nombre, e in f["estados"].items():
            m = acumulado.setdefault(nombre, {
                "ordenes": 0, "peso": 0.0, "guias": 0, "zona": e["zona"],
                "costo": Decimal("0"), "facturado": Decimal("0"),
            })
            m["ordenes"] += e["ordenes"]
            m["peso"] += e["peso"]
            m["guias"] += e["guias"]
            m["costo"] += e["costo"]
            m["facturado"] += e["facturado"]
    total_ordenes = sum(e["ordenes"] for e in acumulado.values())
    tabla_estados = [
        {
            "nombre": nombre,
            "zona": e["zona"],
            "ordenes": e["ordenes"],
            "pct": round(e["ordenes"] * 100 / total_ordenes, 1) if total_ordenes else 0,
            "peso_prom": round(e["peso"] / e["ordenes"], 1) if e["ordenes"] else 0,
            "guias": e["guias"],
            "costo": e["costo"],
            "facturado": e["facturado"],
            "margen_envio": e["facturado"] - e["costo"],
        }
        for nombre, e in sorted(acumulado.items(), key=lambda kv: (-kv[1]["ordenes"], kv[0]))
    ]
    peso_total = sum(e["peso"] for e in acumulado.values())
    totales_estados = {
        "ordenes": total_ordenes,
        "peso_prom": round(peso_total / total_ordenes, 1) if total_ordenes else 0,
        "guias": sum(e["guias"] for e in acumulado.values()),
        "costo": sum((e["costo"] for e in acumulado.values()), Decimal("0")),
        "facturado": sum((e["facturado"] for e in acumulado.values()), Decimal("0")),
    }
    totales_estados["margen_envio"] = totales_estados["facturado"] - totales_estados["costo"]
    fijos = Decimal(str(torre["COSTOS_FIJOS_MES_MXN"]))
    profit = margen_bruto - fijos
    meta = Decimal(str(torre["META_PROFIT_MES_MXN"]))
    if profit >= meta:
        profit_pill = "ok"
    elif profit >= meta / 2:
        profit_pill = "warn"
    else:
        profit_pill = "crit"

    return render(request, "mesa/finanzas.html", {
        "seccion": "finanzas",
        "mes_texto": f"{anio}-{mes:02d}",
        "mes_prev": mes_prev,
        "mes_sig": mes_sig,
        "filas": filas,
        "tabla_estados": tabla_estados,
        "totales_estados": totales_estados,
        "ingreso_total": ingreso_total,
        "costo_envio": costo_envio,
        "costo_insumos": costo_insumos,
        "margen_bruto": margen_bruto,
        "fijos": fijos,
        "profit": profit,
        "profit_pill": profit_pill,
        "meta_profit": meta,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Manuales (SOPs) — pre-renderizados por `manage.py render_manuales`
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa", "piso")
def manuales(request):
    """Índice de manuales. El piso también los consulta desde su tablet.

    La Mesa ve además las últimas sugerencias de clientes (llegan del portal
    como EventoAuditoria: entidad "manual", acción "sugerencia_cliente").
    """
    from apps.core.manuales import agrupar, manuales_publicados, portada

    publicados = manuales_publicados()
    sugerencias = []
    if request.rol == "mesa" or request.user.is_superuser:
        slug_por_codigo = {m["codigo"]: m["slug"] for m in publicados}
        sugerencias = list(
            EventoAuditoria.objects.filter(
                entidad="manual", accion="sugerencia_cliente"
            ).select_related("cliente")[:10]
        )
        for evento in sugerencias:
            evento.manual_slug = slug_por_codigo.get(evento.entidad_id)

    return render(request, "manuales/lista.html", {
        "seccion": "manuales",
        "grupos": agrupar(publicados),
        "portada": portada(),
        "sugerencias": sugerencias,
        "url_detalle": "mesa:manual_detalle",
    })


@rol_requerido("mesa", "piso")
def manual_detalle(request, slug):
    from apps.core.manuales import obtener_manual

    manual = obtener_manual(slug)
    if manual is None:
        raise Http404("Ese manual no existe.")
    return render(request, "manuales/detalle.html", {
        "seccion": "manuales",
        "manual": manual,
        "cuerpo_template": f"manuales/{slug}.html",
        "url_lista": "mesa:manuales",
    })
