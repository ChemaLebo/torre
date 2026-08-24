"""Vistas del portal del cliente (contrato CONVENTIONS.md §portal).

Reglas duras de este módulo:
- TODO filtrado por request.cliente (decorator portal_requerido). Los detalles
  hacen get_object_or_404 con cliente=request.cliente: lo ajeno es un 404.
- El portal le habla a Karina: cero tecnicismos, frescura honesta ("hace X min",
  jamás "tiempo real") y parámetros canónicos desde settings.TORRE.
- Servicios de otras apps SIEMPRE con import lazy (dentro de la función).
"""
import csv
from datetime import time as dt_time

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.decorators import portal_requerido
from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento
from apps.incidencias.models import Incidencia, MensajeIncidencia
from apps.integraciones.models import SyncLog
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada
from apps.pedidos.models import Pedido

from .forms import (
    FormAnuncioASN, FormNuevaIncidencia, FormRespuestaIncidencia, FormSugerenciaManual,
)

# ─────────────────────────────────────────────────────────────────────────────
# Vocabulario del portal (estados → palabras de cliente)
# ─────────────────────────────────────────────────────────────────────────────

_ESTADOS_PIPELINE = [
    Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO,
    Pedido.GUIA_GENERADA, Pedido.RECOLECTADO, Pedido.EN_TRANSITO,
]
_ESTADOS_EN_PROCESO = _ESTADOS_PIPELINE + [
    Pedido.PARCIALMENTE_DESPACHADO, Pedido.CANCELACION_PENDIENTE, Pedido.ENTREGA_PRESUNTA,
]

_EMBUDO = [
    (Pedido.PENDIENTE, "Por preparar"),
    (Pedido.EN_PICKING, "En preparación"),
    (Pedido.EMPACADO, "Empacados y verificados"),
    (Pedido.GUIA_GENERADA, "Con guía lista"),
    (Pedido.RECOLECTADO, "Salieron de bodega"),
    (Pedido.EN_TRANSITO, "En camino"),
    (Pedido.ENTREGADO, "Entregados hoy"),
]

_PILL_PEDIDO = {
    Pedido.ENTREGADO: "ok",
    Pedido.GUIA_GENERADA: "accent",
    Pedido.RECOLECTADO: "accent",
    Pedido.EN_TRANSITO: "accent",
    Pedido.ENTREGA_PRESUNTA: "warn",
    Pedido.PARCIALMENTE_DESPACHADO: "warn",
    Pedido.CANCELACION_PENDIENTE: "warn",
    Pedido.RETORNADO: "crit",
    Pedido.CANCELADO: "",
}

# Estados fuera del camino feliz → banner con explicación en palabras llanas.
_ESTADO_ESPECIAL = {
    Pedido.ENTREGA_PRESUNTA: (
        "warn",
        "La paquetería dejó de reportar movimientos, así que damos el paquete por "
        "entregado mientras lo confirmamos directamente con el comprador.",
    ),
    Pedido.PARCIALMENTE_DESPACHADO: (
        "warn",
        "Una parte del pedido ya va en camino; el resto sale en cuanto haya producto.",
    ),
    Pedido.CANCELACION_PENDIENTE: (
        "warn",
        "El pedido se canceló y estamos regresando el producto a su lugar en bodega.",
    ),
    Pedido.CANCELADO: (
        "crit",
        "Este pedido quedó cancelado. El producto regresó a tu inventario disponible.",
    ),
    Pedido.RETORNADO: (
        "crit",
        "El paquete regresó a la bodega. Ya hay una incidencia dándole seguimiento.",
    ),
}

_PILL_ASN = {
    OrdenEntrada.ANUNCIADA: "",
    OrdenEntrada.EN_RECEPCION: "accent",
    OrdenEntrada.RECIBIDA: "warn",
    OrdenEntrada.CERRADA: "ok",
}

# Kardex en palabras de cliente (nunca claves internas).
_KARDEX_LEGIBLE = {
    Movimiento.RECEPCION: "Llegó a bodega",
    Movimiento.PUTAWAY: "Se acomodó en anaquel",
    Movimiento.RESERVA: "Se apartó para un pedido",
    Movimiento.PICK: "Se tomó para empacar",
    Movimiento.SALIDA: "Salió a entrega",
    Movimiento.AJUSTE: "Ajuste con doble firma",
    Movimiento.RETORNO: "Regresó a bodega",
    Movimiento.MERMA: "Merma registrada",
    Movimiento.CONTEO: "Conteo físico",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _plural(n, singular, plural):
    return singular if n == 1 else plural


def _hace(ts):
    """Frescura honesta: 'hace X min' — nunca la palabra 'tiempo real'."""
    if ts is None:
        return "sin datos"
    minutos = int((timezone.now() - ts).total_seconds() // 60)
    if minutos < 1:
        return "hace un momento"
    if minutos < 60:
        return f"hace {minutos} min"
    horas = minutos // 60
    if horas < 48:
        return f"hace {horas} h"
    dias = horas // 24
    return f"hace {dias} {_plural(dias, 'día', 'días')}"


def _hora_corte():
    crudo = str(settings.TORRE["CORTE_CONTRACTUAL"])
    hora, minuto = crudo.split(":")
    return dt_time(int(hora), int(minuto))


def _fecha_hora(ts):
    """dd/mm/aaaa HH:MM en horario local, o cadena vacía (para CSVs)."""
    if ts is None:
        return ""
    return timezone.localtime(ts).strftime("%d/%m/%Y %H:%M")


def _reloj_sla(incidencia):
    """(clase de pill, texto) del reloj SLA más apretado de la incidencia."""
    if not incidencia.abierta:
        texto = "Cerrada" if incidencia.estado == Incidencia.CERRADA else "Resuelta"
        return "ok", texto
    ahora = timezone.now()
    relojes = []
    if incidencia.ts_primera_respuesta is None and incidencia.sla_respuesta_limite:
        relojes.append(incidencia.sla_respuesta_limite)
    if incidencia.ts_resolucion is None and incidencia.sla_resolucion_limite:
        relojes.append(incidencia.sla_resolucion_limite)
    if not relojes:
        return "ok", "En tiempo"
    limite = min(relojes)
    restante = (limite - ahora).total_seconds()
    if restante < 0:
        return "crit", f"Vencido {_hace(limite)}"
    minutos = int(restante // 60)
    texto = f"Quedan {minutos} min" if minutos < 120 else f"Quedan {minutos // 60} h"
    total = (limite - incidencia.ts_apertura).total_seconds() or 1
    if restante < total * 0.25:
        return "warn", texto
    return "ok", texto


def _quien_tiene_la_pelota(incidencia):
    """'Quién tiene la pelota' en palabras de cliente."""
    if incidencia.estado == Incidencia.RESOLUCION_PROPUESTA:
        return "Tu equipo — hay una propuesta esperando tu visto bueno"
    if incidencia.estado in (Incidencia.RESUELTA, Incidencia.CERRADA):
        return "Nadie — quedó resuelta"
    return incidencia.dueno or "Mesa de Control"


def _decorar_incidencia(incidencia):
    incidencia.reloj_clase, incidencia.reloj_texto = _reloj_sla(incidencia)
    incidencia.pelota = _quien_tiene_la_pelota(incidencia)
    incidencia.abierta_hace = _hace(incidencia.ts_apertura)
    return incidencia


def _filas_inventario(cliente):
    """Resumen por SKU (servicio de inventario) + bandera de resurtido."""
    from apps.inventario.services import resumen_sku  # lazy: servicio de otra app

    filas = []
    for sku in SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo"):
        fila = resumen_sku(sku)
        fila["bajo_reorden"] = sku.punto_reorden > 0 and fila["disponible"] <= sku.punto_reorden
        fila["ultimo_conteo_hace"] = (
            _hace(fila["ultimo_conteo"]) if fila["ultimo_conteo"] else "sin conteo aún"
        )
        filas.append(fila)
    return filas


def _salud_sync(cliente):
    """Último latido de cada conexión con la tienda, con frescura 'hace X min'."""
    etiquetas = [
        (SyncLog.DIRECCION_INGESTA, "Pedidos que llegan de tu tienda"),
        (SyncLog.DIRECCION_PUSH, "Inventario que publicamos en tu tienda"),
    ]
    filas, con_error = [], False
    for tienda in cliente.tiendas.filter(activo=True):
        for direccion, etiqueta in etiquetas:
            log = tienda.sync_logs.filter(direccion=direccion).order_by("-ts").first()
            if log is None:
                filas.append({
                    "tienda": tienda.dominio, "etiqueta": etiqueta,
                    "frescura": "sin actividad todavía", "clase": "warn",
                })
                continue
            ok = log.resultado == SyncLog.RESULTADO_OK
            if not ok:
                con_error = True
            filas.append({
                "tienda": tienda.dominio, "etiqueta": etiqueta,
                "frescura": _hace(log.ts), "clase": "ok" if ok else "crit",
            })
    return filas, con_error


def _semaforo(en_proceso, corte, abiertas, vencidas, criticos, sync_con_error):
    """Banner del día: verde/ámbar/rojo + mensaje en palabras de Karina."""
    if vencidas or sync_con_error:
        partes = []
        if vencidas:
            n = len(vencidas)
            partes.append(f"{n} {_plural(n, 'incidencia', 'incidencias')} con el reloj vencido")
        if sync_con_error:
            partes.append("la conexión con tu tienda reportó un detalle que ya estamos revisando")
        return "crit", "Necesitamos tu atención: " + " y ".join(partes) + "."
    if abiertas or criticos:
        partes = []
        if abiertas:
            n = len(abiertas)
            partes.append(
                f"{n} {_plural(n, 'incidencia abierta', 'incidencias abiertas')} en atención"
            )
        if criticos:
            n = len(criticos)
            partes.append(f"{n} {_plural(n, 'producto', 'productos')} por resurtir")
        return "warn", "Atención: " + " y ".join(partes) + f". Corte de hoy a las {corte}."
    n = en_proceso
    return "ok", (
        f"Todo en orden. {n} {_plural(n, 'pedido', 'pedidos')} en proceso, "
        f"corte a las {corte}."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard "Hoy"
# ─────────────────────────────────────────────────────────────────────────────


@portal_requerido
def dashboard(request):
    cliente = request.cliente
    hoy = timezone.localdate()
    ahora = timezone.localtime()
    corte = str(settings.TORRE["CORTE_CONTRACTUAL"])
    corte_hora = _hora_corte()

    base = Pedido.objects.filter(cliente=cliente)
    en_proceso = base.filter(estado__in=_ESTADOS_EN_PROCESO).count()
    salieron_hoy = base.filter(ts_recolectado__date=hoy).count()
    entregados_hoy = base.filter(estado=Pedido.ENTREGADO, ts_entregado__date=hoy).count()

    conteos = {f["estado"]: f["n"] for f in base.values("estado").annotate(n=Count("id"))}
    embudo = []
    for estado, etiqueta in _EMBUDO:
        n = entregados_hoy if estado == Pedido.ENTREGADO else conteos.get(estado, 0)
        embudo.append({"etiqueta": etiqueta, "n": n})
    maximo = max([fila["n"] for fila in embudo] + [1])
    for fila in embudo:
        fila["pct"] = int(fila["n"] * 100 / maximo)

    abiertas = list(
        Incidencia.objects.filter(cliente=cliente, estado__in=Incidencia.ESTADOS_ABIERTOS)
        .select_related("pedido")
        .order_by("prioridad", "ts_apertura")
    )
    for incidencia in abiertas:
        _decorar_incidencia(incidencia)
    vencidas = [i for i in abiertas if i.vencida_respuesta or i.vencida_resolucion]

    criticos = [fila for fila in _filas_inventario(cliente) if fila["bajo_reorden"]]
    salud_sync, sync_con_error = _salud_sync(cliente)

    antes_corte = ahora.time() < corte_hora
    a_tiempo_hoy = base.filter(creado__date=hoy, creado__time__lt=corte_hora).count()
    if antes_corte:
        mensaje_corte = (
            f"Lo que entre antes de las {corte} sale hoy mismo. "
            f"Van {a_tiempo_hoy} {_plural(a_tiempo_hoy, 'pedido', 'pedidos')} de hoy dentro del corte."
        )
    else:
        mensaje_corte = (
            f"El corte de hoy ({corte}) ya pasó: entraron {a_tiempo_hoy} "
            f"{_plural(a_tiempo_hoy, 'pedido', 'pedidos')} a tiempo. "
            "Lo que entre ahora sale mañana."
        )

    semaforo, mensaje_dia = _semaforo(
        en_proceso, corte, abiertas, vencidas, criticos, sync_con_error
    )

    return render(request, "portal/dashboard.html", {
        "seccion": "dashboard",
        "hoy": hoy,
        "corte": corte,
        "semaforo": semaforo,
        "mensaje_dia": mensaje_dia,
        "en_proceso": en_proceso,
        "salieron_hoy": salieron_hoy,
        "embudo": embudo,
        "incidencias_abiertas": abiertas[:6],
        "total_abiertas": len(abiertas),
        "criticos": criticos,
        "salud_sync": salud_sync,
        "antes_corte": antes_corte,
        "mensaje_corte": mensaje_corte,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Pedidos
# ─────────────────────────────────────────────────────────────────────────────


@portal_requerido
def pedidos(request):
    ver = request.GET.get("ver", "proceso")
    qs = Pedido.objects.filter(cliente=request.cliente)
    if ver == "entregados":
        qs = qs.filter(estado__in=[Pedido.ENTREGADO, Pedido.ENTREGA_PRESUNTA])
    elif ver == "incidencias":
        qs = qs.filter(incidencia_activa=True)
    elif ver == "todos":
        pass
    else:
        ver = "proceso"
        qs = qs.filter(estado__in=_ESTADOS_EN_PROCESO)
    lista = list(qs.annotate(piezas=Sum("lineas__cantidad"))[:200])
    for pedido in lista:
        pedido.pill = _PILL_PEDIDO.get(pedido.estado, "")
    return render(request, "portal/pedidos.html", {
        "seccion": "pedidos",
        "ver": ver,
        "pedidos_lista": lista,
    })


def _pipeline(pedido):
    """Pasos del camino feliz con su timestamp real (o pendiente)."""
    marcas = [
        ("Pedido recibido", pedido.creado, "Lo tomamos de tu tienda y apartamos el producto."),
        ("En preparación", pedido.ts_picking, "El equipo junta el pedido pieza por pieza, con escáner."),
        ("Empacado y verificado", pedido.ts_empacado, "Báscula y dos fotos antes de cerrar la caja."),
        ("Guía lista", pedido.ts_guia, "La etiqueta de envío quedó impresa y pegada."),
        ("Salió de bodega", pedido.ts_recolectado, "Se entregó a la paquetería con manifiesto firmado."),
        ("En camino", pedido.ts_en_transito, "La paquetería lo lleva con el comprador."),
        ("Entregado", pedido.ts_entregado, "El paquete llegó a su destino."),
    ]
    pasos = [
        {"etiqueta": etiqueta, "ts": ts, "hecho": ts is not None, "nota": nota, "actual": False}
        for etiqueta, ts, nota in marcas
    ]
    for paso in reversed(pasos):
        if paso["hecho"]:
            paso["actual"] = True
            break
    return pasos


@portal_requerido
def pedido_detalle(request, pk):
    pedido = get_object_or_404(
        Pedido.objects.prefetch_related("lineas__sku", "incidencias", "guias"),
        pk=pk, cliente=request.cliente,
    )
    especial = _ESTADO_ESPECIAL.get(pedido.estado)
    guia = next((g for g in pedido.guias.all() if g.es_activa), None) or pedido.guias.first()
    if guia is not None:
        guia.movio_hace = _hace(guia.ts_ultimo_movimiento or guia.creado)
    fotos = EvidenciaFoto.objects.filter(
        entidad="pedido", entidad_id__in=[str(pedido.pk), pedido.folio],
    )
    incidencias_pedido = [_decorar_incidencia(i) for i in pedido.incidencias.all()]
    direccion = pedido.direccion or {}
    paquetes = list(
        pedido.paquetes.prefetch_related("lineas__linea_pedido__sku", "guias").order_by("numero")
    )
    ahorro_division = paquetes[0].ahorro_plan_mxn if paquetes else 0
    return render(request, "portal/pedido_detalle.html", {
        "seccion": "pedidos",
        "pedido": pedido,
        "paquetes": paquetes,
        "ahorro_division": ahorro_division,
        "pill": _PILL_PEDIDO.get(pedido.estado, ""),
        "pasos": _pipeline(pedido),
        "especial": especial,
        "guia": guia,
        "fotos": fotos,
        "incidencias_pedido": incidencias_pedido,
        "direccion": direccion,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Inventario (+ kardex por SKU)
# ─────────────────────────────────────────────────────────────────────────────


@portal_requerido
def inventario(request):
    filas = _filas_inventario(request.cliente)
    kardex = None
    codigo = request.GET.get("sku")
    if codigo:
        sku = get_object_or_404(SKU, cliente=request.cliente, codigo=codigo)
        movimientos = []
        for mov in Movimiento.objects.filter(sku=sku)[:120]:
            movimientos.append({
                "ts": mov.ts,
                "que_paso": _movimiento_legible(mov),
                "delta": mov.delta,
                "referencia": mov.referencia,
            })
        kardex = {"sku": sku, "movimientos": movimientos}
    return render(request, "portal/inventario.html", {
        "seccion": "inventario",
        "filas": filas,
        "kardex": kardex,
    })


def _movimiento_legible(mov):
    if mov.tipo == Movimiento.RESERVA and mov.delta < 0:
        return "Se liberó un apartado"
    return _KARDEX_LEGIBLE.get(mov.tipo, mov.get_tipo_display())


# ─────────────────────────────────────────────────────────────────────────────
# Incidencias
# ─────────────────────────────────────────────────────────────────────────────


@portal_requerido
def incidencias(request):
    todas = list(
        Incidencia.objects.filter(cliente=request.cliente)
        .select_related("pedido")
        .order_by("-ts_apertura")
    )
    for incidencia in todas:
        _decorar_incidencia(incidencia)
    abiertas = [i for i in todas if i.abierta]
    abiertas.sort(key=lambda i: (i.prioridad, i.ts_apertura))
    historial = [i for i in todas if not i.abierta]
    return render(request, "portal/incidencias.html", {
        "seccion": "incidencias",
        "abiertas": abiertas,
        "historial": historial,
    })


@portal_requerido
def incidencia_detalle(request, pk):
    incidencia = get_object_or_404(
        Incidencia.objects.select_related("pedido", "sku").prefetch_related("compensaciones"),
        pk=pk, cliente=request.cliente,
    )
    nombre = request.user.get_full_name() or request.user.username
    form = FormRespuestaIncidencia()

    if request.method == "POST":
        from apps.incidencias.services import responder  # lazy: servicio de otra app

        if request.POST.get("accion") == "urgente":
            if incidencia.prioridad == Incidencia.P1:
                messages.info(request, "Esta incidencia ya está marcada como urgente.")
            elif not incidencia.abierta:
                messages.error(
                    request,
                    "Esta incidencia ya quedó resuelta. Si algo sigue mal, levanta una nueva.",
                )
            else:
                anterior = incidencia.prioridad
                incidencia.prioridad = Incidencia.P1
                incidencia.save(update_fields=["prioridad"])
                registrar_evento(
                    "incidencia", incidencia.folio, "marcar_urgente",
                    actor=request.user, cliente=request.cliente,
                    delta={"de": anterior, "a": Incidencia.P1},
                    motivo=f"{nombre} la marcó urgente desde el portal.",
                )
                responder(
                    incidencia, autor="Torre", rol_autor=MensajeIncidencia.ROL_SISTEMA,
                    texto=(
                        f"{nombre} marcó esta incidencia como urgente. "
                        "Sube al frente de la fila de la Mesa de Control."
                    ),
                )
                messages.success(
                    request, "Listo: la marcamos urgente y la Mesa de Control la toma de inmediato.",
                )
            return redirect("portal:incidencia_detalle", pk=incidencia.pk)

        form = FormRespuestaIncidencia(request.POST)
        if form.is_valid():
            responder(
                incidencia, autor=nombre, rol_autor=MensajeIncidencia.ROL_CLIENTE,
                texto=form.cleaned_data["texto"],
            )
            messages.success(
                request, "Tu mensaje quedó en el expediente; la Mesa de Control te responde en breve.",
            )
            return redirect("portal:incidencia_detalle", pk=incidencia.pk)

    _decorar_incidencia(incidencia)
    timeline = incidencia.mensajes.filter(interno=False)
    return render(request, "portal/incidencia_detalle.html", {
        "seccion": "incidencias",
        "incidencia": incidencia,
        "timeline": timeline,
        "compensaciones": incidencia.compensaciones.all(),
        "form": form,
        "puede_urgente": incidencia.abierta and incidencia.prioridad != Incidencia.P1,
    })


@portal_requerido
def incidencia_nueva(request):
    form = FormNuevaIncidencia(request.cliente, request.POST or None)
    if request.method == "POST" and form.is_valid():
        from apps.incidencias.services import abrir_incidencia  # lazy: servicio de otra app

        incidencia = abrir_incidencia(
            cliente=request.cliente,
            tipo=form.cleaned_data["tipo"],
            origen=Incidencia.ORIGEN_CLIENTE,
            pedido=form.cleaned_data.get("pedido"),
            texto=form.cleaned_data["descripcion"],
        )
        horas = settings.TORRE["SLA_PRIMERA_RESPUESTA_CLIENTE_HORAS"]
        messages.success(
            request,
            f"Quedó registrada con el folio {incidencia.folio}. Una persona de la "
            f"Mesa de Control te responde en un máximo de {horas} horas hábiles.",
        )
        return redirect("portal:incidencia_detalle", pk=incidencia.pk)
    return render(request, "portal/incidencia_nueva.html", {
        "seccion": "incidencias",
        "form": form,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Recepciones (ASN)
# ─────────────────────────────────────────────────────────────────────────────


def _decorar_orden(orden):
    lineas = list(orden.lineas.all())
    orden.total_anunciado = sum(l.cantidad_anunciada for l in lineas)
    orden.total_recibido = sum(l.cantidad_recibida for l in lineas)
    orden.total_danado = sum(l.cantidad_danada for l in lineas)
    orden.pill = _PILL_ASN.get(orden.estado, "")
    if orden.estado == OrdenEntrada.CERRADA:
        orden.reloj_clase, orden.reloj_texto = "ok", "Lista y a la venta"
    elif orden.estado == OrdenEntrada.ANUNCIADA:
        orden.reloj_clase, orden.reloj_texto = "", "Esperando tu mercancía"
    elif orden.sla_vencido:
        orden.reloj_clase, orden.reloj_texto = "crit", "Nos estamos tardando más de lo prometido"
    elif orden.sla_restante is not None:
        horas = max(int(orden.sla_restante.total_seconds() // 3600), 0)
        orden.reloj_clase = "warn" if horas < 2 else "ok"
        orden.reloj_texto = f"En acomodo — quedan {horas} h de la promesa"
    else:
        orden.reloj_clase, orden.reloj_texto = "accent", "Descargando"
    return orden


def _avisar_piso_asn(orden):
    """Web Push al piso: se anunció una recepción. Best-effort TOTAL: un push
    caído o sin VAPID JAMÁS rompe el anuncio de la ASN."""
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


@portal_requerido
def recepciones(request):
    form = FormAnuncioASN(request.cliente, request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        fecha = form.cleaned_data["fecha_compromiso"]
        tarimas = form.cleaned_data.get("tarimas") or 0
        orden = OrdenEntrada.objects.create(
            cliente=request.cliente, fecha_compromiso=fecha, tarimas=tarimas,
        )
        for sku, cantidad in form.cleaned_data["lineas"]:
            LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=cantidad)
        registrar_evento(
            "asn", orden.folio, "anunciada_portal",
            actor=request.user, cliente=request.cliente,
            delta={
                "fecha_compromiso": str(fecha),
                "tarimas": tarimas,
                "lineas": [
                    {"sku": sku.codigo, "cantidad": cantidad}
                    for sku, cantidad in form.cleaned_data["lineas"]
                ],
            },
            motivo="Entrega anunciada desde el portal del cliente.",
        )
        _avisar_piso_asn(orden)
        horas = settings.TORRE["SLA_RECEPCION_HORAS_CONTRACTUAL"]
        messages.success(
            request,
            f"Tu entrega quedó anunciada con el folio {orden.folio} para el "
            f"{fecha.strftime('%d/%m/%Y')}. Al llegar la descargamos y la dejamos "
            f"lista para vender en un máximo de {horas} horas.",
        )
        return redirect("portal:recepciones")
    ordenes = [
        _decorar_orden(orden)
        for orden in OrdenEntrada.objects.filter(cliente=request.cliente)
        .prefetch_related("lineas__sku")
    ]
    return render(request, "portal/recepciones.html", {
        "seccion": "recepciones",
        "ordenes": ordenes,
        "form": form,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Bodega en vivo: el plano real del Local 380 E, filtrado al cliente
# ─────────────────────────────────────────────────────────────────────────────


def _copy_descarga(orden):
    """La entrega en palabras de Karina, según dónde va en la bodega."""
    if orden.estado == OrdenEntrada.EN_RECEPCION:
        tarimas = f" ({orden.tarimas} tarimas anunciadas)" if orden.tarimas else ""
        return (
            f"🚛 Estamos descargando tu entrega {orden.folio} — "
            f"{orden.total_recibido} de {orden.total_anunciado} piezas contadas{tarimas}."
        )
    if orden.estado == OrdenEntrada.RECIBIDA:
        return (
            f"✅ Tu entrega {orden.folio} ya bajó completa del camión; "
            "la estamos acomodando en anaqueles."
        )
    cita = (
        orden.fecha_compromiso.strftime("%d/%m") if orden.fecha_compromiso else "por confirmar"
    )
    return f"📦 Esperamos tu entrega {orden.folio} (cita: {cita})."


def _frase_bodega(zonas):
    """'Tu producto ahora mismo: …' — el resumen honesto en una línea."""
    vendible = zonas["almacen"]["total_vendible"]
    partes = [
        f"{vendible} {_plural(vendible, 'pieza lista', 'piezas listas')} para venderse"
    ]
    if zonas["en_picking_n"]:
        n = zonas["en_picking_n"]
        partes.append(f"{n} {_plural(n, 'pedido preparándose', 'pedidos preparándose')}")
    if zonas["paquetes_n"]:
        n = zonas["paquetes_n"]
        partes.append(f"{n} {_plural(n, 'caja por salir', 'cajas por salir')}")
    descargando = sum(
        1 for orden in zonas["ordenes"] if orden.estado == OrdenEntrada.EN_RECEPCION
    )
    if descargando:
        partes.append(
            f"{descargando} {_plural(descargando, 'entrega descargándose', 'entregas descargándose')}"
        )
    return "Tu producto ahora mismo: " + " · ".join(partes) + "."


@portal_requerido
def bodega(request):
    """El plano real de la bodega con SOLO lo del cliente, en sus palabras."""
    from apps.mesa.plano import racks_bodega, zonas_bodega  # lazy: módulo compartido

    zonas, badges, zona_activa = zonas_bodega(cliente=request.cliente)
    for orden in zonas["ordenes"]:
        _decorar_orden(orden)
        orden.copy_bodega = _copy_descarga(orden)
    for grupo in zonas["corrales"]:
        for pedido in grupo["pedidos"]:
            pedido.pill = _PILL_PEDIDO.get(pedido.estado, "")

    return render(request, "portal/bodega.html", {
        "seccion": "bodega",
        "actualizado": timezone.localtime(),
        "badges": badges,
        "racks": racks_bodega(),
        "zona_activa": zona_activa,
        "frase_bodega": _frase_bodega(zonas),
        **zonas,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Exportar (3 CSVs + kardex por SKU) — tus datos son tuyos, sin pedir permiso
# ─────────────────────────────────────────────────────────────────────────────


def _respuesta_csv(nombre):
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre}"'
    respuesta.write("\ufeff")  # BOM: que Excel abra los acentos bien
    return respuesta


def _csv_pedidos(request):
    hoy = timezone.localdate().strftime("%Y%m%d")
    respuesta = _respuesta_csv(f"pedidos_{hoy}.csv")
    w = csv.writer(respuesta)
    w.writerow([
        "Folio", "Ingresó", "Comprador", "Código postal", "Entrega local",
        "Estado", "Piezas", "Valor declarado (MXN)", "Paquetería", "Guía",
        "Salió de bodega", "Entregado", "Incidencia activa",
    ])
    qs = (
        Pedido.objects.filter(cliente=request.cliente)
        .prefetch_related("lineas", "guias")
        .order_by("-creado")
    )
    for pedido in qs:
        guia = next((g for g in pedido.guias.all() if g.es_activa), None) or pedido.guias.first()
        piezas = sum(l.cantidad for l in pedido.lineas.all())
        w.writerow([
            pedido.folio,
            _fecha_hora(pedido.creado),
            pedido.comprador_nombre,
            pedido.cp,
            "Sí" if pedido.es_local else "No",
            pedido.get_estado_display(),
            piezas,
            pedido.valor_declarado,
            guia.carrier if guia else "",
            guia.numero if guia else "",
            _fecha_hora(pedido.ts_recolectado),
            _fecha_hora(pedido.ts_entregado),
            "Sí" if pedido.incidencia_activa else "No",
        ])
    return respuesta


def _csv_inventario(request):
    hoy = timezone.localdate().strftime("%Y%m%d")
    respuesta = _respuesta_csv(f"inventario_{hoy}.csv")
    w = csv.writer(respuesta)
    w.writerow([
        "SKU", "Descripción", "Físico", "En recepción", "Apartado",
        "Cuarentena", "Disponible", "Punto de resurtido", "Último conteo",
    ])
    for fila in _filas_inventario(request.cliente):
        sku = fila["sku"]
        w.writerow([
            sku.codigo, sku.descripcion, fila["fisico"], fila["en_recepcion"],
            fila["apartado"], fila["cuarentena"], fila["disponible"],
            sku.punto_reorden, _fecha_hora(fila["ultimo_conteo"]),
        ])
    return respuesta


def _csv_kardex(request):
    codigo = request.GET.get("sku") or ""
    sku = get_object_or_404(SKU, cliente=request.cliente, codigo=codigo)
    hoy = timezone.localdate().strftime("%Y%m%d")
    respuesta = _respuesta_csv(f"kardex_{sku.codigo}_{hoy}.csv")
    w = csv.writer(respuesta)
    w.writerow(["Fecha", "Qué pasó", "Cantidad", "Referencia"])
    for mov in Movimiento.objects.filter(sku=sku):
        w.writerow([
            _fecha_hora(mov.ts), _movimiento_legible(mov),
            f"{mov.delta:+d}", mov.referencia,
        ])
    return respuesta


def _csv_incidencias(request):
    hoy = timezone.localdate().strftime("%Y%m%d")
    respuesta = _respuesta_csv(f"incidencias_{hoy}.csv")
    w = csv.writer(respuesta)
    w.writerow([
        "Folio", "Tipo", "Prioridad", "Estado", "Pedido", "Abierta",
        "Primera respuesta", "Resuelta", "Cerrada",
    ])
    qs = (
        Incidencia.objects.filter(cliente=request.cliente)
        .select_related("pedido")
        .order_by("-ts_apertura")
    )
    for incidencia in qs:
        w.writerow([
            incidencia.folio,
            incidencia.get_tipo_display(),
            incidencia.prioridad,
            incidencia.get_estado_display(),
            incidencia.pedido.folio if incidencia.pedido else "",
            _fecha_hora(incidencia.ts_apertura),
            _fecha_hora(incidencia.ts_primera_respuesta),
            _fecha_hora(incidencia.ts_resolucion),
            _fecha_hora(incidencia.ts_cierre),
        ])
    return respuesta


@portal_requerido
def exportar(request):
    tipo = request.GET.get("csv")
    generadores = {
        "pedidos": _csv_pedidos,
        "inventario": _csv_inventario,
        "incidencias": _csv_incidencias,
        "kardex": _csv_kardex,
    }
    if tipo:
        generador = generadores.get(tipo)
        if generador is None:
            raise Http404("Ese reporte no existe.")
        # Primero se genera (puede dar 404, p. ej. kardex de un SKU ajeno);
        # solo una descarga que sí salió queda en la auditoría.
        respuesta = generador(request)
        registrar_evento(
            "export", tipo, "descarga_csv",
            actor=request.user, cliente=request.cliente,
            delta={"sku": request.GET.get("sku")} if tipo == "kardex" else {},
            motivo="Descarga desde el portal del cliente.",
        )
        return respuesta
    return render(request, "portal/exportar.html", {"seccion": "exportar"})


# ─────────────────────────────────────────────────────────────────────────────
# Manuales (SOPs) — pre-renderizados por `manage.py render_manuales`
# ─────────────────────────────────────────────────────────────────────────────


@portal_requerido
def manuales(request):
    """Índice de manuales para el cliente: lectura abierta, sin datos sensibles."""
    from apps.core.manuales import agrupar, manuales_publicados, portada

    return render(request, "manuales/lista.html", {
        "seccion": "manuales",
        "grupos": agrupar(manuales_publicados()),
        "portada": portada(),
        "url_detalle": "portal:manual_detalle",
    })


@portal_requerido
def manual_detalle(request, slug):
    """Detalle de un manual + form "Sugerir una mejora".

    La sugerencia NO es un modelo nuevo: queda como EventoAuditoria (entidad
    "manual", acción "sugerencia_cliente") y la Mesa la ve en su índice.
    """
    from apps.core.manuales import obtener_manual

    manual = obtener_manual(slug)
    if manual is None:
        raise Http404("Ese manual no existe.")

    form = FormSugerenciaManual(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            registrar_evento(
                "manual", manual["codigo"], "sugerencia_cliente",
                actor=request.user, cliente=request.cliente,
                delta={"texto": form.cleaned_data["texto"]},
                motivo=f"Sugerencia desde el portal sobre {manual['codigo']}.",
            )
            # Web Push a la Mesa, best-effort: la sugerencia se guarda igual
            # aunque el push falle o no haya VAPID. El body lleva SOLO
            # metadatos — jamás el texto libre del cliente (phishing con la
            # marca Torre en una notificación del sistema) — y hay tope de
            # 1 push de sugerencia por cliente cada 10 min.
            try:
                from django.core.cache import cache

                from apps.mensajeria import push  # lazy por contrato

                puede = cache.add(f"push-sugerencia-{request.cliente.pk}", 1, 600)
                if puede:
                    push.enviar_push_a_rol(
                        "mesa",
                        "💡 Nueva sugerencia de manual",
                        f"Nueva sugerencia de {request.cliente.nombre} sobre {manual['codigo']}",
                        url="/mesa/manuales/",
                    )
            except Exception:
                pass
            messages.success(
                request, "Gracias — tu sugerencia llegó directo a la Mesa de Control.",
            )
            return redirect("portal:manual_detalle", slug=slug)
        messages.error(
            request, "Cuéntanos tu sugerencia antes de enviarla: el cuadro está vacío.",
        )

    return render(request, "manuales/detalle.html", {
        "seccion": "manuales",
        "manual": manual,
        "cuerpo_template": f"manuales/{slug}.html",
        "url_lista": "portal:manuales",
        "form": form,
    })
