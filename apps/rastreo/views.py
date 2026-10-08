"""Vistas públicas de rastreo — la cara del CLIENTE ante su comprador.

Sin login: la llave es el token no enumerable. Sin datos sensibles: nombre de
pila, nunca dirección/teléfono/precios. Estados siempre en lenguaje humano.
El 3PL es invisible: la página es 100% de la marca (branding del Cliente).
"""
from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento
from apps.envios.models import Guia, LineaManifiesto, Paquete
from apps.envios.services import calendario_todos_los_dias, dias_promesa_de
from apps.pedidos.linea_tiempo import construir
from apps.pedidos.models import Pedido

from .models import AccesoEtiqueta, AccesoRastreo
from .services import referencias_direccion, url_maps

BRANDING_DEFAULT = {
    "nombre_publico": "",
    "color_primario": "#9E2B25",   # rojo profundo Colima
    "color_fondo": "#F5EFE0",      # crema
    "color_texto": "#2B2118",
    "logo_url": "",
    "whatsapp_soporte": "",
    "dominio_tienda": "",           # link "Volver a la tienda" en la página
    "lema": "Seguimiento de tu pedido",  # bajo el logo
    "pie": "",                      # pie de página, después del nombre (ej. "Hecho con cariño en Colima")
}


def _branding(cliente):
    """Branding del cliente sobre los defaults, listo para la plantilla: nombre
    público (o el nombre del cliente) y url_tienda a partir de dominio_tienda.
    Cada cliente trae lo suyo — nada de la casa se cuela en la página de otro
    (el pie "Hecho con cariño en Colima" salía en pedidos de Infinitea)."""
    b = {**BRANDING_DEFAULT, **(cliente.branding or {})}
    b["nombre_publico"] = b["nombre_publico"] or cliente.nombre
    dominio = str(b.get("dominio_tienda") or "").strip()
    if dominio and not dominio.startswith("http"):
        dominio = f"https://{dominio}"
    b["url_tienda"] = dominio
    return b

ESTADOS_HUMANOS = {
    "PENDIENTE": ("Recibimos tu pedido", "Ya está en nuestras manos y en fila para prepararse."),
    "EN_PICKING": ("Preparando tu pedido", "Estamos armando tu pedido pieza por pieza."),
    "EMPACADO": ("Empacado con cuidado", "Tu pedido está empacado y protegido, listo para salir."),
    "GUIA_GENERADA": ("Listo para salir", "La paquetería ya tiene tu envío asignado."),
    "RECOLECTADO": ("¡Va en camino!", "Tu pedido salió de nuestra bodega."),
    "EN_TRANSITO": ("En camino", "Tu pedido viaja hacia ti."),
    "ENTREGADO": ("Entregado", "Tu pedido llegó. ¡Salud!"),
    "ENTREGA_PRESUNTA": ("Entregado", "La paquetería reporta tu pedido como entregado."),
    "PARCIALMENTE_DESPACHADO": ("En camino por partes", "Una parte de tu pedido ya salió; el resto sale en cuanto esté listo."),
    "CANCELACION_PENDIENTE": ("Cancelación en proceso", "Estamos procesando la cancelación."),
    "CANCELADO": ("Pedido cancelado", "Este pedido fue cancelado."),
    "RETORNADO": ("De regreso con nosotros", "Tu pedido regresó; ya estamos atendiéndolo."),
}

ESTADO_GUIA_HUMANO = {
    "GUIA_CREADA": "Listo para salir",
    "RECOLECTADO": "Va en camino",
    "EN_TRANSITO": "En camino",
    "EN_RUTA": "En reparto — llega hoy",
    "ENTREGADO": "Entregado",
    "INTENTO_FALLIDO": "Intentamos entregarlo — lo reintentaremos",
    "RETENIDO": "En revisión con la paquetería",
    "RETORNO": "De regreso con nosotros",
    "EXCEPCION": "En revisión con la paquetería",
}

# Línea de tiempo POR PAQUETE (Chema 2026-10-08): nuestros pasos y los del
# carrier, cada uno con su hora, de la misma construcción que usa Mesa
# (pedidos.linea_tiempo). Los tres de la paquetería no aplican a la entrega local.
PASOS_PAQUETE = [
    ("recibido", "Recibimos tu pedido"),
    ("picking", "Preparando tu pedido"),
    ("empacado", "Empacado con cuidado"),
    ("guia", "Listo para salir"),
    ("salida", "Salió de nuestra bodega"),
    ("recolectado_carrier", "Recolectado por la paquetería"),
    ("en_transito", "En camino"),
    ("en_ruta", "En reparto"),
    ("entregado", "Entregado"),
]
PASOS_SOLO_PAQUETERIA = {"recolectado_carrier", "en_transito", "en_ruta"}

CARRIER_HUMANO = {
    "puntopost": "PuntoPost", "estafeta": "Estafeta", "paquetexpress": "Paquetexpress", "fedex": "FedEx",
    "dhl": "DHL", "noventa9Minutos": "99minutos", "imile": "iMile", "amPm": "amPm", "local": "Entrega local",
}

TIPOS_REPORTE = [
    ("DAN", "Llegó dañado"),
    ("RET", "No ha llegado"),
    ("FAL", "Llegó incompleto"),
    ("OTRO", "Otro problema"),
]

MAX_REPORTES = 3
PREFIJO_REPORTE = "[Reporte del comprador vía página de rastreo]"

# Página del repartidor: dedupe de escaneo y tope de avisos de auxilio.
ESCANEO_DEDUPE_SEG = 600        # máx 1 evento qr_escaneado por token por 10 min
MAX_AVISOS_NO_ENCONTRADO = 3    # máx 3 avisos por token por hora
AVISOS_VENTANA_SEG = 3600


def _throttle(request):
    """10 requests/min por IP en páginas públicas."""
    ip = request.META.get("REMOTE_ADDR", "?")
    llave = f"rastreo:{ip}"
    cuenta = cache.get(llave, 0)
    if cuenta >= 10:
        raise Http404
    cache.set(llave, cuenta + 1, 60)


def _carrier_humano(codigo):
    return CARRIER_HUMANO.get(codigo or "", (codigo or "").title())


def _contenido(caja, pedido):
    """Qué va en la caja (o en el pedido entero, sin plan de cajas), sin precios."""
    if caja is None:
        return [f"{l.cantidad}× {l.sku.descripcion or l.sku.codigo}" for l in pedido.lineas.select_related("sku")]
    contenido = []
    for pl in caja.lineas.all():
        nombre = pl.linea_pedido.sku.descripcion or pl.linea_pedido.sku.codigo
        texto = f"{pl.cantidad}/{pl.fraccion_de} de {nombre}" if pl.fraccion_de > 1 else f"{pl.cantidad}× {nombre}"
        if pl.repone_a_id:  # reposición (2026-10-05): repone piezas que viajaron en otro paquete
            texto += f" (reposición del paquete {pl.repone_a.numero})"
        contenido.append(texto)
    return contenido


def _pasos(ts, local):
    """Pasos del paquete con su hora. Un paso sin hora cuenta como hecho si
    uno posterior ya la tiene (el carrier no siempre reporta todos); el último
    hecho es el actual. "Recibido" siempre está hecho: el pedido existe."""
    pasos = [
        {"clave": clave, "nombre": nombre, "ts": ts.get(clave), "hecho": False, "actual": False}
        for clave, nombre in PASOS_PAQUETE if not (local and clave in PASOS_SOLO_PAQUETERIA)
    ]
    visto = False
    for paso in reversed(pasos):
        visto = visto or paso["ts"] is not None
        paso["hecho"] = visto
    pasos[0]["hecho"] = True
    actual = next(p for p in reversed(pasos) if p["hecho"])
    actual["actual"] = True
    return pasos


def _estado_paquete(fila, caja, guia, quitada):
    """Chip del paquete en palabras del comprador. Cubre nuestros estados
    (preparación, empaque, listo, salió, se quedó en bodega, reingreso) y los
    del carrier. Regresa (texto, tono): tono "entregado", "alerta" o ""."""
    ts = fila["ts"]
    if guia is not None:
        if guia.estado == Guia.ENTREGADO:
            return "Entregado", "entregado"
        if guia.estado in (Guia.INTENTO_FALLIDO, Guia.RETENIDO, Guia.RETORNO, Guia.EXCEPCION):
            return ESTADO_GUIA_HUMANO[guia.estado], "alerta"
    if caja is not None and caja.reingreso_estado == Paquete.REINGRESADO:
        return "Lo recibimos de vuelta", "alerta"
    if quitada and ts["salida"] is None:
        return "Se quedó en bodega; sale en la siguiente salida", "alerta"
    if guia is not None and guia.estado in (Guia.RECOLECTADO, Guia.EN_TRANSITO, Guia.EN_RUTA):
        return ESTADO_GUIA_HUMANO[guia.estado], ""
    if ts["salida"] is not None:
        return "Salió de nuestra bodega", ""
    if guia is not None:
        return "Listo para salir", ""
    if caja is not None and caja.estado == Paquete.EMPACADO:
        return "Empacado", ""
    if caja is not None and caja.estado == Paquete.EN_EMPAQUE:
        return "Empacando", ""
    if fila["pedido"].estado == Pedido.EN_PICKING:
        return "Preparando tu pedido", ""
    return "En preparación", ""


def _promesa(fila, caja, guia, pedido):
    """(fecha programada, retraso, texto antes de salir). Regla de Chema
    2026-10-08: antes de salir, "N días después de que salga" (los días
    prometidos de la guía o los que se prometerían); ya que salió, la fecha
    programada (salida + promesa, estampada en la guía); vencida sin entrega,
    retraso; entregado, nada."""
    if guia is not None and guia.estado == Guia.ENTREGADO:
        return None, False, ""
    if fila["ts"]["salida"] is not None and fila["compromiso"] is not None:
        return fila["compromiso"], fila["vencido"], ""
    carrier = guia.carrier if guia is not None else (caja.carrier if caja is not None else "")
    if not carrier:
        return None, False, ""
    dias = guia.dias_promesa if guia is not None and guia.dias_promesa is not None else dias_promesa_de(pedido, carrier, caja)
    if dias <= 0:
        return None, False, ""
    if dias == 1:
        return None, False, "Tu paquete llega al día siguiente de que salga."
    domingos = "" if calendario_todos_los_dias(carrier) else ", sin contar domingos"
    return None, False, f"Tu paquete está programado para llegar {dias} días después de que salga{domingos}."


def _paquetes(pedido):
    """Una tarjeta por caja (o por guía, en pedidos sin plan): contenido,
    estado, pasos con hora, link de rastreo del carrier, fecha programada y
    notas. Las horas salen de pedidos.linea_tiempo, como en Mesa."""
    cajas = {
        c.numero: c
        for c in pedido.paquetes.prefetch_related("lineas__linea_pedido__sku", "lineas__repone_a", "guias")
    }
    quitadas = set(LineaManifiesto.objects.filter(pedido=pedido, no_salio=True).values_list("paquete_id", flat=True))
    sueltas = [g for g in pedido.guias.all() if g.paquete_id is None]
    paquetes = []
    for fila in construir(Pedido.objects.filter(pk=pedido.pk)):
        caja = cajas.get(fila["caja"]) if fila["caja"] is not None else None
        guia = fila["guia"]
        if caja is None and guia is None and pedido.estado == Pedido.CANCELADO:
            continue
        guias_caja = list(caja.guias.all()) if caja is not None else sueltas
        cambiada = any(
            (g.estado == Guia.CANCELADA or g.sustituida) and (guia is None or g.pk != guia.pk) for g in guias_caja
        )
        if guia is not None and guia.estado == Guia.CANCELADA:
            guia = None  # cancelada en bodega: la caja espera guía nueva
        carrier = guia.carrier if guia is not None else (caja.carrier if caja is not None else "")
        estado, tono = _estado_paquete(fila, caja, guia, caja is not None and caja.pk in quitadas)
        fecha, retraso, promesa = _promesa(fila, caja, guia, pedido)
        numero_guia = guia.numero if guia is not None and not guia.numero.startswith("LOCAL-") else ""
        paquetes.append({
            "numero": fila["caja"] or 1, "total": fila["total_cajas"],
            "contenido": _contenido(caja, pedido),
            "carrier": _carrier_humano(carrier),
            "guia": numero_guia,
            "url_rastreo": fila["url_rastreo"] if numero_guia else "",
            "estado": estado, "tono": tono, "entregado": tono == "entregado",
            "pasos": _pasos(fila["ts"], carrier == "local"),
            "fecha_programada": fecha, "retraso": retraso, "promesa": promesa,
            "notas": ["Cambiamos la guía de este paquete; la anterior ya no vale."] if cambiada and guia is not None else [],
        })
    return paquetes


def _contexto(pedido):
    branding = _branding(pedido.cliente)

    titulo, descripcion = ESTADOS_HUMANOS.get(
        pedido.estado, ("Tu pedido", "Estamos trabajando en tu pedido.")
    )
    if pedido.detenido and pedido.estado in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        descripcion = "Estamos revisando tu pedido antes de que salga; en cuanto esté listo sigue su camino."

    paquetes = _paquetes(pedido)

    pod = None
    if pedido.estado in ("ENTREGADO", "ENTREGA_PRESUNTA"):
        pod = (EvidenciaFoto.objects
               .filter(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod")
               .first())

    nombre_pila = (pedido.comprador_nombre or "").split(" ")[0]

    return {
        "b": branding,
        "url_whatsapp": _url_whatsapp(branding, pedido),
        "formulario_reporte": bool(settings.TORRE.get("RASTREO_REPORTE_FORMULARIO", False)),
        "pedido": pedido,
        "nombre_pila": nombre_pila,
        "estado_titulo": titulo,
        "estado_descripcion": descripcion,
        "paquetes": paquetes,
        "pod": pod,
        "tipos_reporte": TIPOS_REPORTE,
        "sla_min": settings.TORRE["SLA_PRIMERA_RESPUESTA_COMPRADOR_MIN"],
    }


def _url_whatsapp(branding, pedido):
    """Link wa.me al WhatsApp de soporte del cliente con el mensaje prellenado
    (número de orden o folio y guías); "" si el cliente no capturó número."""
    from urllib.parse import quote  # lazy: solo aquí

    numero = "".join(ch for ch in str(branding.get("whatsapp_soporte") or "") if ch.isdigit())
    if not numero:
        return ""
    guias = [g.numero for g in pedido.guias.all() if g.es_activa and g.numero and not g.numero.startswith("LOCAL-")]
    orden = getattr(pedido, "shopify_order_name", "") or pedido.folio
    texto = f"Hola, escribo por mi pedido {orden}" + (f" (guía {', '.join(guias)})" if guias else "") + "."
    return f"https://wa.me/{numero}?text={quote(texto)}"


def pagina(request, token):
    _throttle(request)
    acceso = get_object_or_404(
        AccesoRastreo.objects.select_related("pedido__cliente"), token=token
    )
    contexto = _contexto(acceso.pedido)
    contexto["token"] = token
    contexto["embed"] = request.GET.get("embed") == "1"
    contexto["reportado"] = request.GET.get("reportado") == "1"
    return render(request, "rastreo/pagina.html", contexto)


def pod(request, token):
    """Foto de entrega (POD) del pedido del token — el token ES la credencial.

    MEDIA no se sirve público: la foto sale por aquí y solo cuando el pedido
    ya está entregado (mismo criterio que _contexto para mostrarla).
    """
    _throttle(request)
    acceso = get_object_or_404(AccesoRastreo.objects.select_related("pedido"), token=token)
    pedido = acceso.pedido
    if pedido.estado not in ("ENTREGADO", "ENTREGA_PRESUNTA"):
        raise Http404
    foto = (EvidenciaFoto.objects
            .filter(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod")
            .first())
    if foto is None:
        raise Http404
    try:
        return FileResponse(foto.archivo.open("rb"))
    except (FileNotFoundError, ValueError):
        raise Http404


@require_POST
def reporte(request, token):
    _throttle(request)
    acceso = get_object_or_404(
        AccesoRastreo.objects.select_related("pedido__cliente"), token=token
    )
    pedido = acceso.pedido
    if not settings.TORRE.get("RASTREO_REPORTE_FORMULARIO", False):
        return redirect(f"/r/{token}/")  # el formulario está apagado: el comprador escribe por WhatsApp

    from apps.incidencias.models import MensajeIncidencia  # lazy
    from apps.incidencias.services import abrir_incidencia, responder  # lazy

    # Los reportes del mismo tipo viven en una sola incidencia (2026-09-28):
    # el tope se cuenta por reportes (mensajes de apertura del comprador).
    previas = MensajeIncidencia.objects.filter(
        incidencia__pedido=pedido, rol_autor=MensajeIncidencia.ROL_COMPRADOR, texto__startswith=PREFIJO_REPORTE,
    ).count()
    if previas >= MAX_REPORTES:
        return redirect(f"/r/{token}/?reportado=1")

    tipo = request.POST.get("tipo", "OTRO")
    tipo = tipo if tipo in dict(TIPOS_REPORTE) else "OTRO"
    tipo_incidencia = tipo if tipo != "OTRO" else "DIR"
    texto = (request.POST.get("texto") or "").strip()[:1000]
    etiqueta = dict(TIPOS_REPORTE).get(tipo, "Otro problema")

    # El registro de sistema lleva SOLO el tipo de reporte; las palabras del
    # comprador van UNA vez, como mensaje suyo (antes iban en ambos y la Mesa
    # veía la misma frase duplicada en el timeline).
    incidencia = abrir_incidencia(
        pedido.cliente, tipo_incidencia, "comprador", pedido=pedido,
        texto=f"{PREFIJO_REPORTE} {etiqueta}.",
    )
    if texto:
        responder(incidencia, pedido.comprador_nombre or "Comprador", "comprador", texto)

    foto = request.FILES.get("foto")
    if foto:
        EvidenciaFoto.objects.create(
            entidad="incidencia", entidad_id=str(incidencia.pk), tipo="dano",
            archivo=foto, tomada_por="comprador", congelada=True,
        )
    return redirect(f"/r/{token}/?reportado=1")


# ─────────────────────────────────────────────────────────────────────────────
# Etiqueta v2: página pública del REPARTIDOR (/r/e/<token>/)
# ─────────────────────────────────────────────────────────────────────────────


def etiqueta_repartidor(request, token):
    """La página a la que lleva el QR de la etiqueta impresa.

    GET: registra el escaneo (deduplicado por token) y muestra ubicación exacta,
    dirección con referencias y el botón de auxilio. POST accion=no_encontrado:
    abre/actualiza la incidencia DIR y avisa a servicio al cliente de la MARCA
    para que contacte al comprador por sus canales oficiales — el comprador
    jamás recibe mensajes de números desconocidos.

    Nunca muestra teléfono, apellidos ni valor declarado: solo lo que el
    repartidor necesita para entregar.
    """
    _throttle(request)
    acceso = get_object_or_404(
        AccesoEtiqueta.objects.select_related("guia__pedido__cliente", "guia__paquete"),
        token=token,
    )
    guia = acceso.guia
    pedido = guia.pedido

    if request.method == "POST":
        if request.POST.get("accion") == "no_encontrado":
            return _repartidor_no_encontrado(request, token, guia)
        return redirect(f"/r/e/{token}/")

    # Escaneo deduplicado: los reintentos del mismo QR no ensucian la auditoría.
    if cache.add(f"etiqueta:escaneo:{token}", 1, ESCANEO_DEDUPE_SEG):
        registrar_evento(
            "etiqueta", guia.numero, "qr_escaneado",
            cliente=pedido.cliente, delta={"folio": pedido.folio},
        )

    branding = _branding(pedido.cliente)

    if guia.paquete is not None:
        paquete_n = guia.paquete.numero
        paquete_m = pedido.paquetes.count() or 1
    else:
        paquete_n = paquete_m = 1

    direccion = pedido.direccion or {}
    ciudad_estado = ", ".join(
        parte for parte in (
            str(direccion.get("city") or "").strip(),
            str(direccion.get("province") or "").strip(),
        ) if parte
    )
    lineas_direccion = [
        linea for linea in (
            str(direccion.get("address1") or "").strip(),
            str(direccion.get("address2") or "").strip(),
            ciudad_estado,
        ) if linea
    ]

    contexto = {
        "b": branding,
        "token": token,
        "pedido_folio": pedido.folio,
        "nombre_pila": (pedido.comprador_nombre or "").split(" ")[0],
        "paquete_n": paquete_n,
        "paquete_m": paquete_m,
        "carrier": guia.carrier,
        "guia_numero": guia.numero,
        "maps_url": url_maps(pedido),
        "lineas_direccion": lineas_direccion,
        "cp": pedido.cp or str(direccion.get("zip") or ""),
        "referencias": referencias_direccion(direccion),
        "avisado": request.GET.get("avisado") == "1",
    }
    return render(request, "rastreo/etiqueta_repartidor.html", contexto)


def _repartidor_no_encontrado(request, token, guia):
    """Botón de auxilio: incidencia DIR + aviso urgente a servicio al cliente.

    Idempotente de punta a punta: la incidencia DIR abierta se reusa (el
    segundo tap agrega al timeline), la notificación va con clave por hora
    (mensajeria.notificar_domicilio_no_encontrado) y el throttle por token
    corta el spam de taps (máx 3 por hora; de ahí en adelante, no-op).
    """
    pedido = guia.pedido
    destino = redirect(f"/r/e/{token}/?avisado=1")

    llave = f"etiqueta:no_encontrado:{token}"
    avisos = cache.get(llave, 0)
    if avisos >= MAX_AVISOS_NO_ENCONTRADO:
        return destino  # el aviso ya salió: al repartidor se le confirma igual
    cache.set(llave, avisos + 1, AVISOS_VENTANA_SEG)

    from apps.incidencias.models import Incidencia, MensajeIncidencia  # lazy
    from apps.incidencias.services import abrir_incidencia, responder  # lazy

    referencias = referencias_direccion(pedido.direccion)
    texto = (
        f"El repartidor de {guia.carrier} no encuentra el domicilio (guía {guia.numero}). "
        f"Referencias impresas: {'; '.join(referencias) or 'sin referencias'}. "
        "Requiere contacto con el comprador por canal oficial."
    )

    abierta = (
        Incidencia.objects.filter(
            pedido=pedido, tipo=Incidencia.TIPO_DIR, estado__in=Incidencia.ESTADOS_ABIERTOS,
        )
        .order_by("-ts_apertura")
        .first()
    )
    if abierta is not None:
        responder(abierta, "Repartidor", MensajeIncidencia.ROL_SISTEMA, texto)
    else:
        abrir_incidencia(
            pedido.cliente, Incidencia.TIPO_DIR, Incidencia.ORIGEN_COMPRADOR,
            pedido=pedido, texto=texto,
        )

    from apps.mensajeria.services import notificar_domicilio_no_encontrado  # lazy
    notificar_domicilio_no_encontrado(guia)

    registrar_evento(
        "etiqueta", guia.numero, "domicilio_no_encontrado",
        cliente=pedido.cliente,
        delta={"folio": pedido.folio, "carrier": guia.carrier},
    )
    return destino
