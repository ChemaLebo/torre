"""Vistas públicas de rastreo — la cara del CLIENTE ante su comprador.

Sin login: la llave es el token no enumerable. Sin datos sensibles: nombre de
pila, nunca dirección/teléfono/precios. Estados siempre en lenguaje humano.
El 3PL es invisible: la página es 100% de la marca (branding del Cliente).
"""
from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento

from .models import AccesoEtiqueta, AccesoRastreo
from .services import referencias_direccion, url_maps

BRANDING_DEFAULT = {
    "nombre_publico": "",
    "color_primario": "#9E2B25",   # rojo profundo Colima
    "color_fondo": "#F5EFE0",      # crema
    "color_texto": "#2B2118",
    "logo_url": "",
    "whatsapp_soporte": "",
}

ESTADOS_HUMANOS = {
    "PENDIENTE": ("Recibimos tu pedido", "Ya está en nuestras manos y en fila para prepararse."),
    "EN_PICKING": ("Preparando tu pedido", "Estamos armando tu pedido pieza por pieza."),
    "EMPACADO": ("Empacado con cuidado", "Tu pedido está empacado y protegido, listo para salir."),
    "GUIA_GENERADA": ("Listo para salir", "La paquetería ya tiene tu envío asignado."),
    "RECOLECTADO": ("¡Va en camino!", "Tu pedido salió de nuestra bodega."),
    "EN_TRANSITO": ("En camino", "Tu pedido viaja hacia ti."),
    "ENTREGADO": ("Entregado", "Tu pedido llegó. ¡Salud!"),
    "ENTREGA_PRESUNTA": ("Entregado", "La paquetería reporta tu pedido como entregado."),
    "PARCIALMENTE_DESPACHADO": ("En camino por partes", "Parte de tu pedido ya va en camino."),
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

PASOS_TIMELINE = [
    ("Recibido", None),
    ("Preparado", "ts_empacado"),
    ("En camino", "ts_recolectado"),
    ("Entregado", "ts_entregado"),
]

TIPOS_REPORTE = [
    ("DAN", "Llegó dañado"),
    ("RET", "No ha llegado"),
    ("FAL", "Llegó incompleto"),
    ("OTRO", "Otro problema"),
]

MAX_REPORTES = 3

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


def _contexto(pedido):
    branding = {**BRANDING_DEFAULT, **(pedido.cliente.branding or {})}
    branding["nombre_publico"] = branding["nombre_publico"] or pedido.cliente.nombre

    titulo, descripcion = ESTADOS_HUMANOS.get(
        pedido.estado, ("Tu pedido", "Estamos trabajando en tu pedido.")
    )

    hechos = {
        "Recibido": pedido.creado if hasattr(pedido, "creado") else None,
        "Preparado": pedido.ts_empacado,
        "En camino": pedido.ts_recolectado,
        "Entregado": pedido.ts_entregado,
    }
    timeline, alcanzado = [], True
    for paso, _ in PASOS_TIMELINE:
        ts = hechos.get(paso)
        hecho = ts is not None or (paso == "Recibido")
        timeline.append({"paso": paso, "hecho": hecho and alcanzado, "ts": ts})
        if not hecho:
            alcanzado = False

    paquetes = []
    for paquete in pedido.paquetes.prefetch_related("lineas__linea_pedido__sku", "guias"):
        guia = paquete.guia_activa
        contenido = []
        for pl in paquete.lineas.all():
            nombre = pl.linea_pedido.sku.descripcion or pl.linea_pedido.sku.codigo
            if pl.fraccion_de > 1:
                contenido.append(f"{pl.cantidad}/{pl.fraccion_de} de {nombre}")
            else:
                contenido.append(f"{pl.cantidad}× {nombre}")
        paquetes.append({
            "numero": paquete.numero,
            "total": None,  # se llena abajo
            "contenido": contenido,
            "guia": guia.numero if guia else "",
            "carrier": (guia.carrier if guia else paquete.carrier).replace("puntopost", "PuntoPost")
                        .replace("estafeta", "Estafeta").replace("paquetexpress", "Paquetexpress")
                        .replace("fedex", "FedEx").replace("local", "Entrega local"),
            "estado": ESTADO_GUIA_HUMANO.get(guia.estado, "En proceso") if guia
                       else ("Empacado" if paquete.estado == "EMPACADO" else "En preparación"),
            "entregado": bool(guia and guia.estado == "ENTREGADO"),
        })
    if not paquetes:  # pedido sin plan: se muestra como un solo envío (legacy)
        guia = next((g for g in pedido.guias.all() if g.es_activa), None)
        if guia or pedido.estado not in ("CANCELADO",):
            paquetes.append({
                "numero": 1, "total": 1,
                "contenido": [
                    f"{l.cantidad}× {l.sku.descripcion or l.sku.codigo}" for l in pedido.lineas.select_related("sku")
                ],
                "guia": guia.numero if guia else "",
                "carrier": guia.carrier.title() if guia else "",
                "estado": ESTADO_GUIA_HUMANO.get(guia.estado, "En proceso") if guia else "En preparación",
                "entregado": bool(guia and guia.estado == "ENTREGADO"),
            })
    total = len(paquetes)
    for p in paquetes:
        p["total"] = total

    pod = None
    if pedido.estado in ("ENTREGADO", "ENTREGA_PRESUNTA"):
        pod = (EvidenciaFoto.objects
               .filter(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod")
               .first())

    nombre_pila = (pedido.comprador_nombre or "").split(" ")[0]

    return {
        "b": branding,
        "pedido": pedido,
        "nombre_pila": nombre_pila,
        "estado_titulo": titulo,
        "estado_descripcion": descripcion,
        "timeline": timeline,
        "paquetes": paquetes,
        "pod": pod,
        "tipos_reporte": TIPOS_REPORTE,
        "sla_min": settings.TORRE["SLA_PRIMERA_RESPUESTA_COMPRADOR_MIN"],
    }


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


@require_POST
def reporte(request, token):
    _throttle(request)
    acceso = get_object_or_404(
        AccesoRastreo.objects.select_related("pedido__cliente"), token=token
    )
    pedido = acceso.pedido

    from apps.incidencias.models import Incidencia  # lazy
    from apps.incidencias.services import abrir_incidencia, responder  # lazy

    previas = Incidencia.objects.filter(pedido=pedido, origen="comprador").count()
    if previas >= MAX_REPORTES:
        return redirect(f"/r/{token}/?reportado=1")

    tipo = request.POST.get("tipo", "OTRO")
    tipo = tipo if tipo in dict(TIPOS_REPORTE) else "OTRO"
    tipo_incidencia = tipo if tipo != "OTRO" else "DIR"
    texto = (request.POST.get("texto") or "").strip()[:1000]
    etiqueta = dict(TIPOS_REPORTE).get(tipo, "Otro problema")

    incidencia = abrir_incidencia(
        pedido.cliente, tipo_incidencia, "comprador", pedido=pedido,
        texto=f"[Reporte del comprador vía página de rastreo] {etiqueta}. {texto}",
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

    branding = {**BRANDING_DEFAULT, **(pedido.cliente.branding or {})}
    branding["nombre_publico"] = branding["nombre_publico"] or pedido.cliente.nombre

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
