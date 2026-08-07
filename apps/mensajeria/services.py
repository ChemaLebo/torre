"""Servicios de mensajería (contrato):

- enviar_plantilla(clave, pedido, contexto_extra=None)
- enviar_confirmacion(pedido)                     → plantilla A
- enviar_en_camino(pedido)                        → plantilla B, SOLO al RECOLECTADO
- enviar_retraso(pedido, nueva_fecha)             → plantilla E
- notificar_cliente_incidencia(incidencia)        → el cliente SIEMPRE se entera
- digest_diario()                                 → un solo resumen 17:30 por cliente

Idempotencia de salida: clave f"{evento}:{canal}:{destinatario}" única en
NotificacionEnviada. Reenviar el mismo evento es no-op silencioso (regresa
la notificación existente).
"""
import os
from datetime import datetime, time, timedelta

from django.apps import apps as django_apps
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Count, F
from django.urls import reverse
from django.utils import timezone

from apps.core.services import registrar_evento

from .adapters import ConsolaAdapter, get_adapter
from .models import NotificacionEnviada, PlantillaMensaje

# ── Fechas en español (sin depender del locale del sistema) ──────────────────

DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Orden del pipeline para presentar conteos de pedidos de forma legible.
ORDEN_ESTADOS_PEDIDO = [
    "PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA", "RECOLECTADO",
    "EN_TRANSITO", "ENTREGADO", "ENTREGA_PRESUNTA", "PARCIALMENTE_DESPACHADO",
    "CANCELACION_PENDIENTE", "CANCELADO", "RETORNADO",
]
ESTADO_PEDIDO_LEGIBLE = {
    "PENDIENTE": "pendientes",
    "EN_PICKING": "en picking",
    "EMPACADO": "empacados",
    "GUIA_GENERADA": "con guía generada",
    "RECOLECTADO": "recolectados",
    "EN_TRANSITO": "en tránsito",
    "ENTREGADO": "entregados",
    "ENTREGA_PRESUNTA": "con entrega presunta",
    "PARCIALMENTE_DESPACHADO": "parcialmente despachados",
    "CANCELACION_PENDIENTE": "con cancelación pendiente",
    "CANCELADO": "cancelados",
    "RETORNADO": "retornados",
}
TIPO_INCIDENCIA_LEGIBLE = {
    "DAN": "daño",
    "RET": "retraso",
    "RF": "retorno falso",
    "FAL": "pedido incompleto",
    "DIR": "dirección",
    "CAN": "cancelación",
    "DES": "descuadre de inventario",
}
ESTADOS_INCIDENCIA_ABIERTOS = ("ABIERTA", "EN_CURSO", "RESOLUCION_PROPUESTA")


def formatear_fecha(fecha):
    """'lunes 21 de julio' — español mexicano, sin locale del sistema."""
    return f"{DIAS_SEMANA[fecha.weekday()]} {fecha.day} de {MESES[fecha.month - 1]}"


def _hora_corte(pedido=None):
    """Corte vigente: el congelado en el pedido al ingresar, o el contractual."""
    if pedido is not None and getattr(pedido, "corte_vigente_al_ingreso", None):
        return pedido.corte_vigente_al_ingreso
    horas, minutos = settings.TORRE["CORTE_CONTRACTUAL"].split(":")
    return time(int(horas), int(minutos))


def _siguiente_dia_habil(fecha):
    """Día hábil siguiente (domingo no es hábil; sábado sí, con horario corto)."""
    siguiente = fecha + timedelta(days=1)
    while siguiente.weekday() == 6:
        siguiente += timedelta(days=1)
    return siguiente


def fecha_promesa(pedido):
    """Fecha HONESTA de salida de bodega, evaluada al momento de confirmar.

    Antes del corte vigente y en día hábil → sale hoy; si no → siguiente día
    hábil. Nada se "congela" en silencio: el comprador recibe la fecha real.
    """
    ahora = timezone.localtime()
    if ahora.weekday() != 6 and ahora.time() <= _hora_corte(pedido):
        return ahora.date()
    return _siguiente_dia_habil(ahora.date())


# ── Contexto de plantilla ────────────────────────────────────────────────────

def _guia_activa(pedido):
    """Última guía del pedido (lazy: envios puede no existir aún al importar)."""
    try:
        Guia = django_apps.get_model("envios", "Guia")
    except LookupError:
        return None
    return Guia.objects.filter(pedido=pedido).order_by("-id").first()


def _link_rastreo(guia):
    # Primero la página brandeada del cliente (la cara A+ ante el comprador);
    # el link crudo del carrier queda solo como último recurso.
    if guia is not None:
        try:
            from apps.rastreo.services import url_publica  # lazy
            return url_publica(guia.pedido)
        except ImportError:
            pass
    if guia is None or not guia.numero:
        return ""
    raw = getattr(guia, "raw", None) or {}
    if isinstance(raw, dict) and raw.get("tracking_url"):
        return raw["tracking_url"]
    return f"{settings.ENVIA_QUERIES_BASE}/guide/{guia.numero}"


def _primer_nombre(pedido):
    nombre = (getattr(pedido, "comprador_nombre", "") or "").strip()
    return nombre.split()[0] if nombre else "amigo(a)"


def _contexto_pedido(pedido):
    """Variables canónicas: {nombre} {folio} {guia} {link_rastreo} {fecha} {marca} {corte}."""
    guia = _guia_activa(pedido)
    corte = _hora_corte(pedido)
    return {
        "nombre": _primer_nombre(pedido),
        "folio": pedido.folio,
        "guia": guia.numero if guia else "",
        "link_rastreo": _link_rastreo(guia),
        "fecha": formatear_fecha(fecha_promesa(pedido)),
        "marca": pedido.cliente.nombre,
        "corte": corte.strftime("%H:%M"),
    }


def obtener_plantilla(clave, cliente=None):
    """Plantilla del cliente si existe; si no la default en BD; si no el cuerpo canónico."""
    plantilla = None
    if cliente is not None:
        plantilla = PlantillaMensaje.objects.filter(clave=clave, cliente=cliente).first()
    if plantilla is None:
        plantilla = PlantillaMensaje.objects.filter(clave=clave, cliente__isnull=True).first()
    if plantilla is None:
        plantilla = PlantillaMensaje.default_en_memoria(clave)
    return plantilla


# ── Envío idempotente (núcleo) ───────────────────────────────────────────────

def _enviar(evento, destinatario, cuerpo, *, cliente=None, plantilla_clave="", referencia="", adapter=None):
    """Envía por el canal activo con idempotencia de salida.

    Si la clave f"{evento}:{canal}:{destinatario}" ya existe → no-op silencioso,
    regresa la NotificacionEnviada previa. Si el canal falla, NO se crea registro
    (un reintento posterior sí podrá salir) y queda evento de auditoría.
    """
    adapter = adapter or get_adapter()
    clave_idempotencia = f"{evento}:{adapter.canal}:{destinatario}"
    existente = NotificacionEnviada.objects.filter(clave_idempotencia=clave_idempotencia).first()
    if existente is not None:
        return existente

    resultado = adapter.enviar(destinatario, cuerpo)
    if not resultado.get("ok"):
        registrar_evento(
            "notificacion", clave_idempotencia, "envio_fallido",
            cliente=cliente,
            delta={"canal": adapter.canal, "detalle": str(resultado.get("detalle", ""))[:300]},
            motivo="El canal de salida rechazó el mensaje; podrá reintentarse",
        )
        return None

    try:
        notificacion = NotificacionEnviada.objects.create(
            clave_idempotencia=clave_idempotencia,
            canal=adapter.canal,
            destinatario=destinatario,
            cuerpo=cuerpo,
            cliente=cliente,
            plantilla_clave=plantilla_clave,
            referencia=referencia,
        )
    except IntegrityError:
        # Carrera entre dos procesos: el otro ganó; regresamos la existente.
        return NotificacionEnviada.objects.get(clave_idempotencia=clave_idempotencia)

    registrar_evento(
        "notificacion", notificacion.pk, "enviada",
        cliente=cliente,
        delta={
            "plantilla": plantilla_clave,
            "canal": adapter.canal,
            "destinatario": destinatario,
            "referencia": referencia,
        },
    )
    return notificacion


# ── Servicios del contrato ───────────────────────────────────────────────────

def enviar_plantilla(clave, pedido, contexto_extra=None):
    """Renderiza la plantilla `clave` para el pedido y la envía al comprador.

    Idempotente por (evento, canal, destinatario). Regresa la NotificacionEnviada
    (nueva o existente) o None si no hubo teléfono / el canal falló.
    """
    destinatario = (getattr(pedido, "comprador_tel", "") or "").strip()
    if not destinatario:
        registrar_evento(
            "notificacion", pedido.folio, "envio_omitido",
            cliente=pedido.cliente,
            motivo=f"Pedido sin teléfono del comprador; plantilla {clave} no enviada",
        )
        return None

    contexto_extra = dict(contexto_extra or {})
    evento = contexto_extra.pop("_evento", f"{clave}:{pedido.folio}")
    contexto = _contexto_pedido(pedido)
    contexto.update(contexto_extra)

    plantilla = obtener_plantilla(clave, pedido.cliente)
    cuerpo = plantilla.render(contexto)
    return _enviar(
        evento, destinatario, cuerpo,
        cliente=pedido.cliente, plantilla_clave=clave, referencia=pedido.folio,
    )


def enviar_confirmacion(pedido):
    """Plantilla A: confirmación honesta, con corte del día y fecha real de salida."""
    return enviar_plantilla(PlantillaMensaje.CLAVE_A, pedido)


def enviar_en_camino(pedido):
    """Plantilla B: "va en camino" SOLO cuando de verdad salió de la bodega.

    Se dispara en RECOLECTADO (escaneo de salida + manifiesto), nunca en
    etiquetado. Jamás se marca enviado un paquete que sigue en la bodega.
    """
    estados_validos = {"RECOLECTADO", "EN_TRANSITO", "ENTREGADO"}
    if pedido.estado not in estados_validos:
        raise ValueError(
            f"La plantilla B (en camino) solo se envía cuando el pedido fue RECOLECTADO; "
            f"el pedido {pedido.folio} está en {pedido.estado}. "
            "Jamás avisamos 'va en camino' de un paquete que sigue en la bodega."
        )
    return enviar_plantilla(PlantillaMensaje.CLAVE_B, pedido)


def enviar_retraso(pedido, nueva_fecha):
    """Plantilla E: disculpa en primera persona de marca con la nueva fecha.

    Nunca culpa a la paquetería: la Guía de Voz del cliente aplica también
    a nuestras plantillas. Un retraso con fecha distinta genera un nuevo
    mensaje; el mismo retraso repetido es no-op.
    """
    if isinstance(nueva_fecha, datetime):
        if timezone.is_aware(nueva_fecha):
            nueva_fecha = timezone.localtime(nueva_fecha)
        nueva_fecha = nueva_fecha.date()
    return enviar_plantilla(
        PlantillaMensaje.CLAVE_E,
        pedido,
        contexto_extra={
            "fecha": formatear_fecha(nueva_fecha),
            "_evento": f"E:{pedido.folio}:{nueva_fecha.isoformat()}",
        },
    )


def notificar_cliente_incidencia(incidencia):
    """Avisa al contacto del cliente que existe una incidencia NUEVA sobre su marca.

    La anti-falla-crítica de Melonn: el cliente SIEMPRE se entera, en el momento,
    por su canal — jamás por Twitter. Si no hay WhatsApp de contacto, el aviso
    sale por consola para no perderse (y queda registrado igual).
    """
    cliente = incidencia.cliente
    contacto = (getattr(cliente, "contacto_nombre", "") or "").strip() or cliente.nombre
    tipo = getattr(incidencia, "tipo", "")
    tipo_legible = TIPO_INCIDENCIA_LEGIBLE.get(tipo, tipo or "incidencia")
    pedido = getattr(incidencia, "pedido", None)
    sobre = f" sobre el pedido {pedido.folio}" if pedido is not None else ""

    cuerpo = (
        f"Hola {contacto}, te escribimos de la Mesa de Control.\n"
        f"Acabamos de abrir la incidencia {incidencia.folio} ({tipo_legible}){sobre}. "
        "Ya tiene dueño asignado y el reloj de atención está corriendo; "
        "la estamos atendiendo en este momento.\n"
        "Puedes seguir todo el detalle y la conversación en tu portal. "
        "Cualquier cosa, respóndenos por aquí."
    )

    destinatario = (getattr(cliente, "contacto_whatsapp", "") or "").strip()
    adapter = None
    if not destinatario:
        destinatario = f"cliente:{cliente.slug}"
        adapter = ConsolaAdapter()

    return _enviar(
        f"INC_NUEVA:{incidencia.folio}", destinatario, cuerpo,
        cliente=cliente, plantilla_clave="INC_NUEVA", referencia=incidencia.folio,
        adapter=adapter,
    )


def notificar_domicilio_no_encontrado(guia):
    """🚨 URGENTE a servicio al cliente de la MARCA: el repartidor no encuentra
    el domicilio. Que contacten al comprador AHORA por su canal oficial — el
    comprador jamás recibe mensajes de números desconocidos.

    Idempotente por hora: clave DIR:{folio}:{hora redondeada}. El doble tap del
    repartidor no spamea, pero si vuelve a pasar horas después sí re-avisa.
    Mismo espíritu que notificar_cliente_incidencia: sin WhatsApp de contacto,
    el aviso sale por consola para no perderse.
    """
    pedido = guia.pedido
    cliente = pedido.cliente
    direccion = pedido.direccion or {}

    from apps.rastreo.services import referencias_direccion  # lazy por contrato
    referencias = "; ".join(referencias_direccion(direccion)) or "sin referencias"
    ciudad = str(direccion.get("city") or "").strip() or "ciudad no capturada"
    cp = pedido.cp or str(direccion.get("zip") or "") or "—"

    base = os.environ.get("BASE_URL_PUBLICA", "http://127.0.0.1:8380").rstrip("/")
    link_pedido = base + reverse("portal:pedido_detalle", args=[pedido.pk])

    cuerpo = (
        f"🚨 {guia.carrier} no encuentra el domicilio del pedido {pedido.folio} "
        f"({ciudad}, CP {cp}). "
        "Contacta al comprador AHORA desde tu canal oficial y confírmale la dirección. "
        f"Referencias que tenemos: '{referencias}'. "
        f"Detalle: {link_pedido}"
    )

    hora = timezone.localtime().strftime("%Y-%m-%dT%H")
    destinatario = (getattr(cliente, "contacto_whatsapp", "") or "").strip()
    adapter = None
    if not destinatario:
        registrar_evento(
            "notificacion", pedido.folio, "envio_omitido",
            cliente=cliente,
            motivo="Cliente sin WhatsApp de contacto; el aviso de domicilio no encontrado sale por consola",
        )
        destinatario = f"cliente:{cliente.slug}"
        adapter = ConsolaAdapter()

    return _enviar(
        f"DIR:{pedido.folio}:{hora}", destinatario, cuerpo,
        cliente=cliente, plantilla_clave="DIR", referencia=pedido.folio,
        adapter=adapter,
    )


def _cuerpo_recepcion_cerrada(orden):
    """Cuerpo del aviso de recepción cerrada, con las cifras reales por SKU.

    Lenguaje de Karina: piezas buenas, dañadas (en revisión), faltantes y
    sobrantes con nombre y apellido. Sin diferencias, el mensaje lo dice claro.
    """
    cliente = orden.cliente
    contacto = (getattr(cliente, "contacto_nombre", "") or "").strip() or cliente.nombre
    lineas = list(orden.lineas.select_related("sku"))
    total_ok = sum(l.cantidad_recibida for l in lineas)

    danadas = [(l.sku.codigo, l.cantidad_danada) for l in lineas if l.cantidad_danada]
    faltantes = []
    sobrantes = []
    for linea in lineas:
        llegado = linea.cantidad_recibida + linea.cantidad_danada
        if llegado < linea.cantidad_anunciada:
            faltantes.append((linea.sku.codigo, linea.cantidad_anunciada - llegado))
        elif llegado > linea.cantidad_anunciada:
            sobrantes.append((linea.sku.codigo, llegado - linea.cantidad_anunciada))

    partes = [f"{total_ok} piezas en buen estado"]
    if danadas:
        detalle = " y ".join(f"{n} de {codigo}" for codigo, n in danadas)
        partes.append(f"{detalle} dañadas (quedan en revisión)")
    if faltantes:
        partes.append(" y ".join(f"faltaron {n} de {codigo}" for codigo, n in faltantes))
    if sobrantes:
        partes.append(" y ".join(f"sobraron {n} de {codigo}" for codigo, n in sobrantes))

    cuerpo = (
        f"Hola {contacto}, te escribimos de tu bodega.\n"
        f"Recibimos tu entrega {orden.folio}: {', '.join(partes)}. "
        "Ya está todo acomodado y vendible.\n"
    )
    if faltantes or sobrantes:
        cuerpo += (
            "Abrimos un folio por las diferencias contra lo que anunciaste; "
            "el detalle te llega por aquí y lo puedes seguir en tu portal."
        )
    else:
        cuerpo += "Llegó completo, tal cual lo anunciaste: sin diferencias."
    return cuerpo


def enviar_recepcion_cerrada(orden):
    """Confirmación honesta al cerrar una recepción (ASN): el cliente sabe qué
    llegó, qué se dañó y qué faltó, con cifras reales.

    Idempotente por clave REC:{folio}:{canal}:{destinatario}. Sin WhatsApp de
    contacto queda evento "envio_omitido" y el aviso sale por consola para no
    perderse (mismo espíritu que notificar_cliente_incidencia).
    """
    cliente = orden.cliente
    cuerpo = _cuerpo_recepcion_cerrada(orden)

    destinatario = (getattr(cliente, "contacto_whatsapp", "") or "").strip()
    adapter = None
    if not destinatario:
        registrar_evento(
            "notificacion", orden.folio, "envio_omitido",
            cliente=cliente,
            motivo="Cliente sin WhatsApp de contacto; el aviso de recepción sale por consola",
        )
        destinatario = f"cliente:{cliente.slug}"
        adapter = ConsolaAdapter()

    return _enviar(
        f"REC:{orden.folio}", destinatario, cuerpo,
        cliente=cliente, plantilla_clave="REC", referencia=orden.folio,
        adapter=adapter,
    )


# ── Digest diario (17:30, un solo mensaje consolidado por cliente) ───────────

def _resumen_pedidos(cliente, fecha):
    """(total, [(etiqueta, n), ...]) de pedidos del día, o None si pedidos no existe aún."""
    try:
        Pedido = django_apps.get_model("pedidos", "Pedido")
    except LookupError:
        return None
    qs = Pedido.objects.filter(cliente=cliente)
    campos = {f.name for f in Pedido._meta.get_fields()}
    if "creado" in campos:
        qs = qs.filter(creado__date=fecha)
    elif "ts_creacion" in campos:
        qs = qs.filter(ts_creacion__date=fecha)
    else:
        # Sin campo de creación: foto del pipeline activo del día.
        qs = qs.exclude(estado__in=["ENTREGADO", "CANCELADO", "RETORNADO"])
    conteos = list(qs.values("estado").annotate(n=Count("id")))
    conteos.sort(key=lambda fila: (
        ORDEN_ESTADOS_PEDIDO.index(fila["estado"]) if fila["estado"] in ORDEN_ESTADOS_PEDIDO else 99
    ))
    total = sum(fila["n"] for fila in conteos)
    detalle = [
        (ESTADO_PEDIDO_LEGIBLE.get(fila["estado"], fila["estado"].lower()), fila["n"])
        for fila in conteos
    ]
    return total, detalle


def _incidencias_abiertas(cliente):
    """Lista de incidencias abiertas del cliente, o None si incidencias no existe aún."""
    try:
        Incidencia = django_apps.get_model("incidencias", "Incidencia")
    except LookupError:
        return None
    campos = {f.name for f in Incidencia._meta.get_fields()}
    orden = "ts_apertura" if "ts_apertura" in campos else "pk"
    abiertas = Incidencia.objects.filter(
        cliente=cliente, estado__in=ESTADOS_INCIDENCIA_ABIERTOS,
    ).order_by(orden)
    resumen = []
    for inc in abiertas:
        tipo = TIPO_INCIDENCIA_LEGIBLE.get(inc.tipo, inc.tipo)
        estado = inc.estado.replace("_", " ").lower()
        resumen.append(f"{inc.folio} · {tipo} · {estado}")
    return resumen


def _discrepancias_del_dia(cliente, fecha):
    """Conteos de hoy con diferencia, o None si inventario no existe aún."""
    try:
        Conteo = django_apps.get_model("inventario", "Conteo")
    except LookupError:
        return None
    con_diferencia = Conteo.objects.filter(
        sku__cliente=cliente, ts__date=fecha,
    ).exclude(contado=F("esperado"))
    resumen = []
    for conteo in con_diferencia:
        codigo = getattr(conteo.sku, "codigo", str(conteo.sku))
        resumen.append(f"{codigo}: esperábamos {conteo.esperado}, contamos {conteo.contado}")
    return resumen


def _componer_digest(cliente, fecha):
    """Texto consolidado del día: pedidos por estado, incidencias abiertas, discrepancias."""
    partes = [f"Resumen del día — {cliente.nombre} · {formatear_fecha(fecha)}", ""]

    pedidos = _resumen_pedidos(cliente, fecha)
    if pedidos is None:
        partes.append("Pedidos: información no disponible en este corte.")
    else:
        total, detalle = pedidos
        if total == 0:
            partes.append("Pedidos de hoy: no entraron pedidos nuevos.")
        else:
            desglose = ", ".join(f"{n} {etiqueta}" for etiqueta, n in detalle)
            partes.append(f"Pedidos de hoy: {total} en total — {desglose}.")

    incidencias = _incidencias_abiertas(cliente)
    if incidencias is None:
        partes.append("Incidencias: información no disponible en este corte.")
    elif not incidencias:
        partes.append("Incidencias abiertas: ninguna. Todo tranquilo por ese frente.")
    else:
        partes.append(f"Incidencias abiertas: {len(incidencias)}")
        partes.extend(f"- {linea}" for linea in incidencias)

    discrepancias = _discrepancias_del_dia(cliente, fecha)
    if discrepancias is None:
        partes.append("Inventario: información no disponible en este corte.")
    elif not discrepancias:
        partes.append("Inventario: conteos del día sin diferencias.")
    else:
        partes.append(f"Inventario: {len(discrepancias)} conteo(s) con diferencia hoy")
        partes.extend(f"- {linea}" for linea in discrepancias)

    partes.append("")
    partes.append("Cualquier duda, respóndenos por aquí. — Mesa de Control")
    return "\n".join(partes)


def digest_diario(fecha=None):
    """Digest 17:30: UN solo mensaje consolidado por cliente activo.

    Idempotente por día: correr el command dos veces no duplica mensajes.
    Regresa la lista de NotificacionEnviada del día.
    """
    from apps.core.models import Cliente

    fecha = fecha or timezone.localdate()
    notificaciones = []
    for cliente in Cliente.objects.filter(activo=True):
        cuerpo = _componer_digest(cliente, fecha)
        destinatario = (cliente.contacto_whatsapp or "").strip()
        adapter = None
        if not destinatario:
            destinatario = f"cliente:{cliente.slug}"
            adapter = ConsolaAdapter()
        notificacion = _enviar(
            f"DIGEST:{fecha.isoformat()}:{cliente.slug}", destinatario, cuerpo,
            cliente=cliente, plantilla_clave="DIGEST",
            referencia=f"DIGEST-{fecha.isoformat()}", adapter=adapter,
        )
        if notificacion is not None:
            notificaciones.append(notificacion)
    return notificaciones
