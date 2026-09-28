"""Servicios de dominio de incidencias (contrato CONVENTIONS.md §incidencias).

Consumidores conocidos (llaman lazy a este módulo):
- inventario.registrar_conteo → abrir_incidencia(tipo=DES) al exceder umbral.
- pedidos.ingerir_pedido_shopify → abrir_incidencia(tipo=FAL) sin stock.
- envios.poll_tracking → abrir_incidencia (RET/RF) por intento fallido,
  retorno o silencio del carrier.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento

from .models import Compensacion, Incidencia, MensajeIncidencia

# Prioridad default por tipo cuando quien abre no la especifica.
PRIORIDAD_DEFAULT_POR_TIPO = {
    Incidencia.TIPO_DAN: Incidencia.P1,  # regalo roto = ocasión perdida
    Incidencia.TIPO_RF: Incidencia.P1,   # comprador dice que no recibió
    Incidencia.TIPO_CAN: Incidencia.P1,  # hay que frenar el paquete YA
    Incidencia.TIPO_RET: Incidencia.P2,
    Incidencia.TIPO_FAL: Incidencia.P2,
    Incidencia.TIPO_DIR: Incidencia.P2,
    Incidencia.TIPO_DES: Incidencia.P3,
    Incidencia.TIPO_CDR: Incidencia.P1,  # hay que cancelar la guía ANTES de que salga
    Incidencia.TIPO_PAQ: Incidencia.P1,  # el pedido no puede salir hasta elegir paquetería
}


def _nombre_actor(actor):
    """Nombre legible de un actor (User, str o None) para el timeline."""
    if actor is None:
        return "Mesa de Control"
    return getattr(actor, "username", None) or str(actor)


def _autor_inicial(origen, cliente):
    """(autor, rol_autor) del mensaje de apertura según el origen."""
    if origen == Incidencia.ORIGEN_COMPRADOR:
        return "Comprador final", MensajeIncidencia.ROL_COMPRADOR
    if origen == Incidencia.ORIGEN_CLIENTE:
        return (cliente.contacto_nombre or cliente.nombre), MensajeIncidencia.ROL_CLIENTE
    if origen == Incidencia.ORIGEN_AUTO:
        return "Torre", MensajeIncidencia.ROL_SISTEMA
    return "Mesa de Control", MensajeIncidencia.ROL_MESA


def _congelar_evidencia_pedido(pedido):
    """Congela TODA la EvidenciaFoto del pedido: pertenece al expediente.

    EvidenciaFoto liga por (entidad, entidad_id) con entidad_id string;
    cubrimos pk y folio porque ambos identificadores son válidos en captura.
    """
    identificadores = {str(pedido.pk)}
    folio = getattr(pedido, "folio", "")
    if folio:
        identificadores.add(str(folio))
    return EvidenciaFoto.objects.filter(
        entidad="pedido", entidad_id__in=identificadores
    ).update(congelada=True)


def auto_pausadas(cliente):
    """True si hoy el cliente tiene pausadas las incidencias automáticas
    (Cliente.incidencias_auto_pausadas_hasta, inclusive). Las manuales, las del
    cliente y las del comprador nunca se pausan."""
    hasta = getattr(cliente, "incidencias_auto_pausadas_hasta", None)
    return bool(hasta) and timezone.localdate() <= hasta


def abrir_incidencia(cliente, tipo, origen, pedido=None, sku=None, texto="", prioridad=None, orden=None,
                     interna=False):
    """Abre una incidencia con folio y relojes SLA. Un caso por tipo y pedido
    (Chema 2026-09-28): si ya hay una del mismo tipo sobre el pedido sin
    cerrar, el reporte nuevo se suma a ese caso (_agrupar_reporte) y se
    regresa esa incidencia con `agrupada=True`; tipos distintos conviven.
    `orden`: la recepción (OrdenEntrada) de la que nace, para las DES de recepción.
    Con las automáticas pausadas (auto_pausadas) una de origen "auto" NO nace:
    regresa None, no toca el pedido y deja el evento "auto_omitida" con el texto.
    `interna` (Chema 2026-09-24): incidencia de la bodega, no del cliente: no se
    pausa, no le avisa al cliente, no marca pedido.incidencia_activa (el
    portal muestra ese flag) y el portal jamás la lista.

    - SLA de primera respuesta: 30 min si origen=comprador, 2 h en los demás
      casos (valores canónicos de settings.TORRE).
    - SLA de resolución: 48 h (settings.TORRE).
    - Congela la evidencia del pedido y marca pedido.incidencia_activa.
    - Notifica al cliente vía mensajeria (lazy; tolera módulo ausente).
    """
    if origen == Incidencia.ORIGEN_AUTO and not interna and auto_pausadas(cliente):
        referencia = (
            getattr(pedido, "folio", None) or getattr(orden, "folio", None)
            or getattr(sku, "codigo", None) or cliente.slug
        )
        registrar_evento(
            "incidencia", referencia, "auto_omitida", cliente=cliente,
            delta={
                "tipo": tipo,
                "pedido": getattr(pedido, "folio", None) if pedido else None,
                "sku": getattr(sku, "codigo", None) if sku else None,
                "orden": getattr(orden, "folio", None) if orden else None,
                "pausadas_hasta": cliente.incidencias_auto_pausadas_hasta.isoformat(),
            },
            motivo=texto[:300],
        )
        return None
    abierta = _abierta_del_mismo_tipo(pedido, tipo, interna)
    if abierta is not None:
        return _agrupar_reporte(abierta, origen, texto, prioridad)
    ahora = timezone.now()
    torre = settings.TORRE
    if origen == Incidencia.ORIGEN_COMPRADOR:
        limite_respuesta = ahora + timedelta(minutes=torre["SLA_PRIMERA_RESPUESTA_COMPRADOR_MIN"])
    else:
        limite_respuesta = ahora + timedelta(hours=torre["SLA_PRIMERA_RESPUESTA_CLIENTE_HORAS"])

    incidencia = Incidencia.objects.create(
        cliente=cliente,
        pedido=pedido,
        sku=sku,
        orden=orden,
        tipo=tipo,
        origen=origen,
        interna=interna,
        prioridad=prioridad or PRIORIDAD_DEFAULT_POR_TIPO.get(tipo, Incidencia.P2),
        ts_apertura=ahora,
        sla_respuesta_limite=limite_respuesta,
        sla_resolucion_limite=ahora + timedelta(hours=torre["SLA_RESOLUCION_HORAS"]),
    )

    if texto:
        autor, rol_autor = _autor_inicial(origen, cliente)
        MensajeIncidencia.objects.create(
            incidencia=incidencia, autor=autor, rol_autor=rol_autor, texto=texto, ts=ahora
        )

    if pedido is not None:
        _congelar_evidencia_pedido(pedido)
        if not interna and not pedido.incidencia_activa:
            pedido.incidencia_activa = True
            pedido.save(update_fields=["incidencia_activa"])

    registrar_evento(
        "incidencia",
        incidencia.folio,
        "abrir",
        cliente=cliente,
        delta={
            "tipo": tipo,
            "origen": origen,
            "prioridad": incidencia.prioridad,
            "pedido": getattr(pedido, "folio", None) if pedido else None,
            "sku": getattr(sku, "codigo", None) if sku else None,
            "sla_respuesta_limite": limite_respuesta.isoformat(),
            "sla_resolucion_limite": incidencia.sla_resolucion_limite.isoformat(),
        },
        motivo=texto[:300],
    )

    if not interna:
        try:
            from apps.mensajeria.services import notificar_cliente_incidencia
        except ImportError:
            pass  # mensajeria aún no existe: la apertura no se bloquea por la notificación
        else:
            notificar_cliente_incidencia(incidencia)

    _push_mesa(incidencia, f"⚠️ Incidencia {incidencia.folio}")
    incidencia.agrupada = False
    return incidencia


def _push_mesa(incidencia, titulo):
    """Web Push a la Mesa, best-effort TOTAL: un push caído o sin VAPID JAMÁS
    bloquea la apertura. Sale en transaction.on_commit: abrir_incidencia corre
    DENTRO de la transacción del caller (p.ej. la ingesta Shopify con locks de
    Saldo tomados vía la incidencia FAL) — un push service colgado con el lock
    tomado apilaría workers. Mismo patrón que pedidos._avisar_piso."""
    pedido = incidencia.pedido
    sobre = f" · {pedido.folio}" if pedido is not None else ""

    def _avisar_mesa():
        try:
            from apps.mensajeria import push  # lazy por contrato

            push.enviar_push_a_rol(
                "mesa", titulo,
                f"{incidencia.get_tipo_display()} · {incidencia.cliente.nombre}{sobre}",
                url=f"/mesa/incidencias/{incidencia.pk}/",
            )
        except Exception:
            pass
    transaction.on_commit(_avisar_mesa)


def _abierta_del_mismo_tipo(pedido, tipo, interna):
    """La incidencia sin cerrar del mismo tipo sobre el pedido (las internas
    y las del cliente no se mezclan), o None. Sin pedido no hay con qué agrupar."""
    if pedido is None:
        return None
    return (
        Incidencia.objects.filter(pedido=pedido, tipo=tipo, interna=interna)
        .exclude(estado=Incidencia.CERRADA).order_by("-pk").first()
    )


def _agrupar_reporte(incidencia, origen, texto, prioridad):
    """Un reporte nuevo del mismo tipo sobre el mismo pedido vive en el caso
    abierto (Chema 2026-09-28): el texto entra al timeline con quién lo
    reportó (un texto idéntico al último no se repite: el poller insiste), la
    prioridad sube si la nueva es mayor, y un caso RESUELTO se reabre
    (EN_CURSO) porque el cliente aún no lo daba por cerrado. Mesa recibe push;
    al cliente no se le vuelve a avisar de un caso que ya conoce."""
    if texto:
        ultimo = incidencia.mensajes.order_by("-pk").first()
        if ultimo is None or ultimo.texto != texto:
            autor, rol_autor = _autor_inicial(origen, incidencia.cliente)
            MensajeIncidencia.objects.create(
                incidencia=incidencia, autor=autor, rol_autor=rol_autor, texto=texto, ts=timezone.now(),
            )
    nueva = prioridad or PRIORIDAD_DEFAULT_POR_TIPO.get(incidencia.tipo, Incidencia.P2)
    if nueva < incidencia.prioridad:  # "P1" < "P2" < "P3"
        incidencia.prioridad = nueva
        incidencia.save(update_fields=["prioridad"])
    if incidencia.estado == Incidencia.RESUELTA:
        incidencia.transicionar(Incidencia.EN_CURSO, motivo="Nuevo reporte del mismo tipo: el caso se reabre.")
    pedido = incidencia.pedido
    if not incidencia.interna and not pedido.incidencia_activa:
        pedido.incidencia_activa = True
        pedido.save(update_fields=["incidencia_activa"])
    registrar_evento(
        "incidencia", incidencia.folio, "reporte_agrupado", cliente=incidencia.cliente,
        delta={"tipo": incidencia.tipo, "origen": origen, "pedido": pedido.folio, "prioridad": incidencia.prioridad},
        motivo=(texto or "")[:300],
    )
    _push_mesa(incidencia, f"🔁 Nuevo reporte en {incidencia.folio}")
    incidencia.agrupada = True
    return incidencia


def responder(incidencia, autor, rol_autor, texto, interno=False):
    """Agrega un mensaje al timeline y estampa ts_primera_respuesta cuando
    corresponde: la PRIMERA respuesta humana de la Mesa NO interna.

    Ni las notas internas ni los auto-acuses del sistema ni los mensajes
    entrantes (cliente/comprador) paran el reloj del SLA 4a/4b.
    """
    mensaje = MensajeIncidencia.objects.create(
        incidencia=incidencia,
        autor=autor,
        rol_autor=rol_autor,
        texto=texto,
        interno=interno,
    )
    if (
        not interno
        and rol_autor == MensajeIncidencia.ROL_MESA
        and incidencia.ts_primera_respuesta is None
    ):
        incidencia.ts_primera_respuesta = mensaje.ts
        incidencia.save(update_fields=["ts_primera_respuesta"])

    registrar_evento(
        "incidencia",
        incidencia.folio,
        "responder",
        actor=autor,
        cliente=incidencia.cliente,
        delta={"rol_autor": rol_autor, "interno": interno, "mensaje_id": mensaje.pk},
    )
    return mensaje


def resolver(incidencia, resolucion_texto, actor):
    """Marca la incidencia RESUELTA con su texto de resolución en el timeline.

    La resolución cuenta como respuesta de la Mesa: si aún no había primera
    respuesta humana, este mensaje estampa ts_primera_respuesta.
    """
    incidencia.transicionar(Incidencia.RESUELTA, actor=actor, motivo=(resolucion_texto or "")[:300])
    if resolucion_texto:
        responder(
            incidencia,
            autor=_nombre_actor(actor),
            rol_autor=MensajeIncidencia.ROL_MESA,
            texto=resolucion_texto,
        )
    if incidencia.tipo == Incidencia.TIPO_CAN and incidencia.pedido_id:
        # Resolver la CAN de una cancelación tardía cierra el pedido como CANCELADO
        # (si Mesa no lo cerró antes decidiendo el reingreso).
        from apps.pedidos.services import cerrar_cancelacion_tardia  # lazy por contrato

        incidencia.pedido.refresh_from_db(fields=["estado", "cancelacion_tardia"])
        cerrar_cancelacion_tardia(
            incidencia.pedido, actor,
            f"Cancelación tardía cerrada al resolver {incidencia.folio}.",
        )
    return incidencia


def cerrar(incidencia, actor):
    """Cierra la incidencia y, si el pedido ya no tiene incidencias abiertas
    (ninguna sin cerrar), desmarca pedido.incidencia_activa."""
    incidencia.transicionar(Incidencia.CERRADA, actor=actor)
    pedido = incidencia.pedido
    if pedido is not None:
        quedan_abiertas = (
            Incidencia.objects.filter(pedido=pedido, interna=False)  # las internas no cuentan para el flag
            .exclude(estado=Incidencia.CERRADA)
            .exists()
        )
        if not quedan_abiertas and pedido.incidencia_activa:
            pedido.incidencia_activa = False
            pedido.save(update_fields=["incidencia_activa"])
    return incidencia


# ── Compensaciones que ejecutan (Chema 2026-09-28) ──
# Reposición física: desde daño, faltante y retorno/no entregado; reembolso:
# esos más retraso y cancelación tardía; cupón: registro en cualquier caso.
TIPOS_CON_REPOSICION = (Incidencia.TIPO_DAN, Incidencia.TIPO_FAL, Incidencia.TIPO_RF)
TIPOS_CON_REEMBOLSO = (
    Incidencia.TIPO_DAN, Incidencia.TIPO_FAL, Incidencia.TIPO_RF, Incidencia.TIPO_RET, Incidencia.TIPO_CAN,
)


def opciones_compensacion(incidencia):
    """[(tipo, nombre)] de compensación que aplican a esta incidencia."""
    nombres = dict(Compensacion.TIPOS)
    opciones = []
    if incidencia.pedido_id:
        if incidencia.tipo in TIPOS_CON_REPOSICION:
            opciones.append((Compensacion.TIPO_REPOSICION, nombres[Compensacion.TIPO_REPOSICION]))
        if incidencia.tipo in TIPOS_CON_REEMBOLSO:
            opciones.append((Compensacion.TIPO_REEMBOLSO, nombres[Compensacion.TIPO_REEMBOLSO]))
    opciones.append((Compensacion.TIPO_CUPON, nombres[Compensacion.TIPO_CUPON]))
    return opciones


def lineas_para_compensar(incidencia):
    """Los line items originales del pedido (sin reposiciones previas ni
    componentes de kit): lo que se puede reponer o reembolsar."""
    if incidencia.pedido_id is None:
        return []
    return list(
        incidencia.pedido.lineas.filter(reposicion_de__isnull=True, parte_de_kit__isnull=True)
        .select_related("sku").order_by("pk")
    )


def seleccion_desde_post(post, incidencia):
    """[(línea, cantidad)] marcadas en el formulario (linea_<pk> + cantidad_<pk>)."""
    seleccion = []
    for linea in lineas_para_compensar(incidencia):
        if not post.get(f"linea_{linea.pk}"):
            continue
        try:
            cantidad = int(post.get(f"cantidad_{linea.pk}") or linea.cantidad)
        except (TypeError, ValueError):
            raise ValueError(f"Cantidad inválida para {linea.sku.codigo}.") from None
        seleccion.append((linea, cantidad))
    return seleccion


def _precio_linea(linea):
    """Lo que se vendió (precio real de la tienda) o, sin venta, el declarado del catálogo."""
    if linea.precio_unitario is not None:
        return Decimal(linea.precio_unitario)
    return Decimal(linea.sku.precio_declarado or 0)


def crear_compensacion(incidencia, tipo, actor, rol, lineas=None, monto=None, reembolsar_envio=False,
                       avisar_comprador=True, aprobar=False):
    """Propone una compensación (COTIZADA) y, con `aprobar`, la ejecuta ya.
    `rol` = "mesa" | "cliente" (quién la propone). Reposición: líneas
    obligatorias, monto = valor declarado del catálogo (informativo).
    Reembolso: líneas y/o envío (Shopify calcula el monto real al ejecutar) o
    un monto libre sin líneas. Cupón: monto obligatorio, solo registro."""
    permitidos = dict(opciones_compensacion(incidencia))
    if tipo not in permitidos:
        raise ValueError("Esa compensación no aplica a este tipo de incidencia.")
    try:
        monto = Decimal(str(monto).strip()) if monto not in (None, "") else None
    except InvalidOperation:
        raise ValueError("Captura el monto en MXN (ej. 450.00).") from None
    seleccion = [(linea, int(cantidad)) for linea, cantidad in (lineas or []) if int(cantidad or 0) > 0]
    for linea, cantidad in seleccion:
        if cantidad > linea.cantidad:
            raise ValueError(f"{linea.sku.codigo}: el pedido lleva {linea.cantidad} pieza(s), no {cantidad}.")
    if tipo == Compensacion.TIPO_REPOSICION:
        if not seleccion:
            raise ValueError("Elige qué productos se reponen.")
        monto = sum((Decimal(linea.sku.precio_declarado or 0) * cantidad for linea, cantidad in seleccion), Decimal(0))
        reembolsar_envio = False
    elif tipo == Compensacion.TIPO_REEMBOLSO:
        if not seleccion and not reembolsar_envio and not (monto and monto > 0):
            raise ValueError("Elige qué se reembolsa: productos, el envío o un monto.")
        if seleccion:
            monto = sum((_precio_linea(linea) * cantidad for linea, cantidad in seleccion), Decimal(0))
        elif monto is None:
            monto = Decimal(0)  # solo envío: Shopify dice cuánto
    else:
        if not monto or monto <= 0:
            raise ValueError("Captura el monto del cupón en MXN.")
        reembolsar_envio = False
    comp = Compensacion.objects.create(
        incidencia=incidencia, tipo=tipo, monto=monto,
        lineas=[{"linea_id": linea.pk, "sku": linea.sku.codigo, "cantidad": cantidad} for linea, cantidad in seleccion],
        reembolsar_envio=bool(reembolsar_envio), avisar_comprador=bool(avisar_comprador),
        creada_por=Compensacion.CREADA_CLIENTE if rol == "cliente" else Compensacion.CREADA_MESA,
    )
    registrar_evento(
        "compensacion", comp.pk, "creada", actor=actor, cliente=incidencia.cliente,
        delta={"incidencia": incidencia.folio, "tipo": tipo, "monto": str(monto), "lineas": comp.lineas,
               "envio": comp.reembolsar_envio, "por": comp.creada_por},
    )
    rol_autor = MensajeIncidencia.ROL_CLIENTE if rol == "cliente" else MensajeIncidencia.ROL_MESA
    detalle = comp.resumen_lineas + (" + envío" if comp.reembolsar_envio else "") if (comp.lineas or comp.reembolsar_envio) else f"${monto}"
    responder(incidencia, _nombre_actor(actor), rol_autor, f"Propuso {permitidos[tipo].lower()}: {detalle}.")
    if aprobar:
        aprobar_compensacion(comp, actor, rol)
    return comp


def _seleccion_de(comp):
    from apps.pedidos.models import LineaPedido  # lazy por contrato

    por_id = {l.pk: l for l in LineaPedido.objects.filter(pk__in=[x.get("linea_id") for x in comp.lineas]).select_related("sku")}
    return [(por_id[x["linea_id"]], int(x["cantidad"])) for x in comp.lineas if x.get("linea_id") in por_id]


def aprobar_compensacion(comp, actor, rol="mesa"):
    """COTIZADA → APROBADA y se ejecuta: la reposición regresa el pedido a
    picking (si falla, nada cambia: ValueError al que aprueba); el reembolso
    va a Shopify fuera de la transacción (ejecutar_reembolso); el cupón es
    registro. Queda en el timeline de la incidencia."""
    incidencia = comp.incidencia
    with transaction.atomic():
        comp.transicionar(Compensacion.APROBADA, actor=actor, motivo=f"Aprobada ({incidencia.folio})")
        if comp.tipo == Compensacion.TIPO_REPOSICION:
            from apps.pedidos.services import reponer_lineas  # lazy por contrato

            nuevas = reponer_lineas(incidencia.pedido, _seleccion_de(comp), actor, incidencia=incidencia)
            comp.nota = f"{incidencia.pedido.folio} regresó a picking con {sum(n.cantidad for n in nuevas)} pieza(s) por reponer."
            comp.save(update_fields=["nota"])
    if comp.tipo == Compensacion.TIPO_REEMBOLSO:
        ejecutar_reembolso(comp, actor)
    responder(
        incidencia, _nombre_actor(actor),
        MensajeIncidencia.ROL_CLIENTE if rol == "cliente" else MensajeIncidencia.ROL_MESA, resumen_ejecucion(comp),
    )
    return comp


def ejecutar_reembolso(comp, actor):
    """Refund en Shopify de una compensación APROBADA (o reintento): con el
    id del refund pasa a PAGADA; un rechazo queda en `nota` y se reintenta
    desde Mesa. Sin tienda de Shopify no hay a dónde: se paga por fuera y se
    marca PAGADA con su referencia."""
    incidencia = comp.incidencia
    pedido = incidencia.pedido
    if comp.estado != Compensacion.APROBADA or comp.referencia_pago:
        return comp
    if pedido is None or pedido.tienda_id is None or not pedido.shopify_order_id:
        comp.nota = "Sin tienda de Shopify: haz el reembolso por tu medio y márcalo pagado con su referencia."
        comp.save(update_fields=["nota"])
        return comp
    from apps.integraciones.services import reembolsar_en_shopify  # lazy por contrato
    from apps.integraciones.shopify import ShopifyError  # lazy por contrato

    try:
        referencia, monto = reembolsar_en_shopify(
            pedido, lineas=[(x["sku"], int(x["cantidad"])) for x in comp.lineas],
            reembolsar_envio=comp.reembolsar_envio, monto=None if comp.lineas else comp.monto,
            nota=f"{incidencia.folio} · Torre", avisar=comp.avisar_comprador,
        )
    except ShopifyError as exc:
        comp.nota = f"Shopify rechazó el reembolso: {str(exc)[:240]}"
        comp.save(update_fields=["nota"])
        registrar_evento("compensacion", comp.pk, "reembolso_rechazado", actor=actor, cliente=incidencia.cliente,
                         delta={"incidencia": incidencia.folio}, motivo=comp.nota)
        return comp
    comp.referencia_pago = referencia
    comp.monto = monto
    comp.nota = "Reembolso hecho en Shopify."
    comp.save(update_fields=["referencia_pago", "monto", "nota"])
    comp.transicionar(Compensacion.PAGADA, actor=actor, motivo=f"Reembolso en Shopify ({referencia})")
    return comp


def compensaciones_por_entrega(pedido):
    """La reposición se entregó (el pedido volvió a ENTREGADO): sus
    compensaciones aprobadas pasan a PAGADA con nota en el timeline."""
    comps = Compensacion.objects.filter(
        incidencia__pedido=pedido, tipo=Compensacion.TIPO_REPOSICION, estado=Compensacion.APROBADA,
    ).select_related("incidencia")
    for comp in comps:
        comp.referencia_pago = f"{pedido.folio} entregado"
        comp.nota = "Reposición entregada."
        comp.save(update_fields=["referencia_pago", "nota"])
        comp.transicionar(Compensacion.PAGADA, motivo="La reposición se entregó.")
        responder(comp.incidencia, "Torre", MensajeIncidencia.ROL_SISTEMA, f"La reposición de {pedido.folio} se entregó.")
    return len(comps)


def resumen_ejecucion(comp):
    """Frase para el flash de Mesa/portal tras crear, aprobar o ejecutar."""
    nombre = comp.get_tipo_display()
    detalle = comp.resumen_lineas or f"${comp.monto}"
    if comp.estado == Compensacion.COTIZADA:
        return f"{nombre} propuesta ({detalle}): falta aprobarla."
    if comp.tipo == Compensacion.TIPO_REPOSICION:
        return f"Reposición aprobada: {comp.nota or detalle}"
    if comp.tipo == Compensacion.TIPO_REEMBOLSO:
        if comp.estado == Compensacion.PAGADA:
            return f"Reembolso de ${comp.monto} hecho en Shopify ({comp.referencia_pago})."
        return f"Reembolso aprobado, pero no se ejecutó: {comp.nota or 'sin detalle'}"
    return f"{nombre} aprobado por ${comp.monto}."


def sin_paqueteria_abierta(pedido):
    """La incidencia interna "Sin paquetería que cotice" abierta del pedido, o None."""
    return (
        Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_PAQ, interna=True)
        .exclude(estado=Incidencia.CERRADA).order_by("-pk").first()
    )


def abrir_sin_paqueteria(pedido, detalle):
    """Ningún carrier cotiza el pedido (plan de cajas imposible; Chema
    2026-09-24): incidencia interna automática P1, una por pedido. Si ya hay
    una abierta, solo se agrega el detalle nuevo al timeline (un reintento no
    duplica). Nunca se pausa: es de la bodega. Mesa la resuelve eligiendo
    paquetería (pedidos.services.replanear_con_carrier)."""
    texto = (detalle or "Ningún carrier cotiza el pedido.")[:900]
    abierta = sin_paqueteria_abierta(pedido)
    if abierta is not None:
        ultimo = abierta.mensajes.order_by("-pk").first()
        if ultimo is None or ultimo.texto != texto:
            responder(abierta, "Torre", MensajeIncidencia.ROL_SISTEMA, texto)
        return abierta
    incidencia = abrir_incidencia(
        pedido.cliente, Incidencia.TIPO_PAQ, Incidencia.ORIGEN_AUTO, pedido=pedido, texto=texto, interna=True,
    )
    registrar_evento(
        "pedido", pedido.pk, "sin_paqueteria", cliente=pedido.cliente,
        delta={"incidencia": str(getattr(incidencia, "folio", "") or ""), "cp": pedido.cp}, motivo=texto[:300],
    )
    return incidencia


def abiertas_fuera_de_sla():
    """Incidencias abiertas con algún reloj SLA vencido, para el dashboard Mesa.

    Vencida = sin primera respuesta pasado sla_respuesta_limite, o sin
    resolución pasado sla_resolucion_limite.
    """
    ahora = timezone.now()
    return (
        Incidencia.objects.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
        .filter(
            Q(ts_primera_respuesta__isnull=True, sla_respuesta_limite__lt=ahora)
            | Q(ts_resolucion__isnull=True, sla_resolucion_limite__lt=ahora)
        )
        .select_related("cliente", "pedido", "sku")
        .order_by("prioridad", "sla_resolucion_limite")
    )
