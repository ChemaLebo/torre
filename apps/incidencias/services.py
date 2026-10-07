"""Servicios de dominio de incidencias (contrato CONVENTIONS.md §incidencias).

Consumidores conocidos (llaman lazy a este módulo):
- inventario.registrar_conteo → abrir_incidencia(tipo=DES) al exceder umbral.
- pedidos.ingerir_pedido_shopify → abrir_incidencia(tipo=FAL) sin stock.
- envios.poll_tracking → abrir_incidencia (RET/RF) por intento fallido,
  retorno o silencio del carrier.
"""
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import EventoAuditoria, EvidenciaFoto
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
    Incidencia.TIPO_DET: Incidencia.P1,  # el pedido está parado en el piso hasta que Mesa resuelva
    Incidencia.TIPO_SKU: Incidencia.P1,  # el pedido no puede surtirse: falta dar de alta el producto
}
# Tipos que detienen el pedido en piso: al resolverlos o cerrarlos, se reanuda.
TIPOS_QUE_DETIENEN = (Incidencia.TIPO_DET, Incidencia.TIPO_PAQ, Incidencia.TIPO_SKU)


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
                     interna=False, pese_a_pausa=False):
    """Abre una incidencia con folio y relojes SLA. Un caso por tipo y pedido
    (Chema 2026-09-28): si ya hay una del mismo tipo sobre el pedido sin
    cerrar, el reporte nuevo se suma a ese caso (_agrupar_reporte) y se
    regresa esa incidencia con `agrupada=True`; tipos distintos conviven.
    `orden`: la recepción (OrdenEntrada) de la que nace, para las DES de recepción.
    Con las automáticas pausadas (auto_pausadas) una de origen "auto" NO nace:
    regresa None, no toca el pedido y deja el evento "auto_omitida" con el texto;
    salvo `pese_a_pausa` (Chema 2026-10-06): el retorno al remitente abre su
    RF aunque la pausa esté activa, porque exige reingreso y reenvío (PED-00067).
    `interna` (Chema 2026-09-24): incidencia de la bodega, no del cliente: no se
    pausa, no le avisa al cliente, no marca pedido.incidencia_activa (el
    portal muestra ese flag) y el portal jamás la lista.

    - SLA de primera respuesta: 30 min si origen=comprador, 2 h en los demás
      casos (valores canónicos de settings.TORRE).
    - SLA de resolución: 48 h (settings.TORRE).
    - Congela la evidencia del pedido y marca pedido.incidencia_activa.
    - Notifica al cliente vía mensajeria (lazy; tolera módulo ausente).
    """
    if origen == Incidencia.ORIGEN_AUTO and not interna and not pese_a_pausa and auto_pausadas(cliente):
        referencia = (
            getattr(pedido, "folio", None) or getattr(orden, "folio", None)
            or getattr(sku, "codigo", None) or cliente.slug
        )
        # Sin repetir (Chema 2026-10-06): el poller vuelve a intentar la misma
        # incidencia cada corrida; con la pausa activa dejaba un auto_omitida
        # cada 15 min (cientos por guía, PED-00067). Un evento por referencia y
        # texto idénticos basta.
        if EventoAuditoria.objects.filter(
            entidad="incidencia", entidad_id=str(referencia), accion="auto_omitida", motivo=texto[:300],
        ).exists():
            return None
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
    avisar_por_correo(incidencia, "abierta", f"Se abrió la incidencia {incidencia.folio}", texto)
    incidencia.agrupada = False
    return incidencia


def avisar_por_correo(incidencia, evento, titulo, detalle=""):
    """Correo a la lista de correos de incidencias (configuracion) con pedido,
    folio, tipo, prioridad y motivo (Chema 2026-10-06: "vital" que los
    bloqueos se vean fuera de Torre). Una incidencia INTERNA solo avisa a la
    lista fija de Torre (Chema 2026-10-07: hablar con el cliente lo decide una
    persona); las demás, a la fija más la del cliente. Sale en
    transaction.on_commit y es best-effort: un SMTP caído jamás bloquea la
    operación (queda `correo_fallido`). Uno por incidencia y evento
    (`correo_enviado` en auditoría): reintentar no duplica. Sin destinatarios
    no manda nada y deja `correo_sin_destinatarios`."""
    from apps.configuracion.services import correos_incidencias  # lazy por contrato
    from apps.core.services import branding_correo, enviar_correo  # lazy por contrato

    def _mandar():
        clave = f"{evento}:{incidencia.folio}"
        if EventoAuditoria.objects.filter(entidad="incidencia", entidad_id=incidencia.folio, accion="correo_enviado", motivo=clave).exists():
            return
        destinatarios = correos_incidencias(None if incidencia.interna else incidencia.cliente)
        if not destinatarios:
            registrar_evento("incidencia", incidencia.folio, "correo_sin_destinatarios", cliente=incidencia.cliente, motivo=clave)
            return
        pedido = incidencia.pedido
        asunto = f"[Torre] {incidencia.folio} · {incidencia.tipo}" + (f" · {pedido.folio}" if pedido is not None else "") + f" · {titulo}"
        contexto = {
            "asunto": asunto, "titulo": titulo, "detalle": (detalle or "").strip(), "incidencia": incidencia,
            "pedido": pedido, "marca": branding_correo(None),
            "url": os.environ.get("BASE_URL_PUBLICA", "http://127.0.0.1:8380").rstrip("/") + f"/mesa/incidencias/{incidencia.pk}/",
        }
        try:
            enviar_correo(destinatarios, asunto, "incidencias/correo_incidencia", contexto)
        except Exception as exc:  # noqa: BLE001 — el correo es aviso; la incidencia ya quedó
            registrar_evento(
                "incidencia", incidencia.folio, "correo_fallido", cliente=incidencia.cliente,
                delta={"a": destinatarios, "error": str(exc)[:200]}, motivo=clave,
            )
            return
        registrar_evento(
            "incidencia", incidencia.folio, "correo_enviado", cliente=incidencia.cliente,
            delta={"a": destinatarios, "asunto": asunto}, motivo=clave,
        )
    transaction.on_commit(_mandar)


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
    _reanudar_si_detenia(incidencia, actor)
    return incidencia


def _reanudar_si_detenia(incidencia, actor):
    """Resolver o cerrar una DET/PAQ reanuda el pedido detenido (Chema
    2026-10-06): vuelve a la cola de Mi turno, el primero por antigüedad, y
    la lista de correos se entera. Si otra DET/PAQ del pedido sigue abierta,
    el pedido se queda detenido."""
    if incidencia.tipo not in TIPOS_QUE_DETIENEN or incidencia.pedido_id is None:
        return
    from apps.pedidos.services import reanudar_pedido  # lazy por contrato

    pedido = incidencia.pedido
    pedido.refresh_from_db(fields=["detenido", "estado"])
    if not pedido.detenido:
        return
    if Incidencia.objects.filter(
        pedido=pedido, tipo__in=TIPOS_QUE_DETIENEN, estado__in=Incidencia.ESTADOS_ABIERTOS,
    ).exclude(pk=incidencia.pk).exists():
        return
    if reanudar_pedido(pedido, actor, motivo=f"Reanudado al resolver {incidencia.folio}."):
        avisar_por_correo(
            incidencia, "reanudada", f"{pedido.folio} se reanudó: vuelve a la cola del piso",
            resolucion_de(incidencia) or "",
        )


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
    _reanudar_si_detenia(incidencia, actor)
    return incidencia


# ── Compensaciones que ejecutan (Chema 2026-09-28) ──
# Reposición física y reembolso en CUALQUIER incidencia ligada a un pedido
# (Chema: el caso de hoy no era daño/faltante); cupón siempre. Las de bodega
# (sin pedido) solo cupón.


def opciones_compensacion(incidencia):
    """[(tipo, nombre)] de compensación que aplican a esta incidencia."""
    nombres = dict(Compensacion.TIPOS)
    opciones = []
    if incidencia.pedido_id:
        opciones.append((Compensacion.TIPO_REPOSICION, nombres[Compensacion.TIPO_REPOSICION]))
        opciones.append((Compensacion.TIPO_REEMBOLSO, nombres[Compensacion.TIPO_REEMBOLSO]))
    opciones.append((Compensacion.TIPO_CUPON, nombres[Compensacion.TIPO_CUPON]))
    return opciones


def lineas_para_compensar(incidencia):
    """Los line items originales del pedido (sin reposiciones previas ni
    componentes de kit): lo que se puede reponer o reembolsar. Cada línea
    trae `salieron` (piezas que ya salieron en una caja despachada: lo único
    reponible, pedidos.piezas_reponibles) y `cajas_salida` ("1, 2") para el
    formulario."""
    from apps.pedidos.services import piezas_reponibles  # lazy por contrato

    if incidencia.pedido_id is None:
        return []
    reponibles = piezas_reponibles(incidencia.pedido)
    lineas = list(
        incidencia.pedido.lineas.filter(parte_de_kit__isnull=True)
        .select_related("sku").order_by("pk")
    )
    for linea in lineas:
        info = reponibles.get(linea.pk)
        linea.salieron = info["piezas"] if info else 0
        linea.cajas_salida = ", ".join(str(n) for n in info["cajas"]) if info else ""
        # Caja de origen por producto (2026-10-05): [(pk, "caja 1 · 2 pzas")] para el selector.
        linea.cajas_origen = [
            (pk, f"caja {c['numero']} · {c['piezas']} pza{'s' if c['piezas'] != 1 else ''}")
            for pk, c in (info["por_caja"].items() if info else [])
        ]
    return lineas


def seleccion_desde_post(post, incidencia):
    """[(línea, cantidad, caja_pk|None)] marcadas en el formulario (linea_<pk>
    + cantidad_<pk> + caja_<pk>: de qué caja salieron las piezas, solo para
    reposición)."""
    seleccion = []
    for linea in lineas_para_compensar(incidencia):
        if not post.get(f"linea_{linea.pk}"):
            continue
        try:
            cantidad = int(post.get(f"cantidad_{linea.pk}") or linea.cantidad)
        except (TypeError, ValueError):
            raise ValueError(f"Cantidad inválida para {linea.sku.codigo}.") from None
        caja = post.get(f"caja_{linea.pk}") or None
        seleccion.append((linea, cantidad, int(caja) if caja and str(caja).isdigit() else None))
    return seleccion


def _precio_linea(linea):
    """Lo que se vendió (precio real de la tienda) o, sin venta, el declarado del catálogo."""
    if linea.precio_unitario is not None:
        return Decimal(linea.precio_unitario)
    return Decimal(linea.sku.precio_declarado or 0)


def crear_compensacion(incidencia, tipo, actor, rol, lineas=None, monto=None, reembolsar_envio=False,
                       avisar_comprador=True, aprobar=False, motivo=""):
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
    # (línea, cantidad[, caja origen]): la caja solo importa para reponer.
    seleccion = [
        (item[0], int(item[1]), item[2] if len(item) > 2 else None)
        for item in (lineas or []) if int(item[1] or 0) > 0
    ]
    for linea, cantidad, _caja in seleccion:
        if cantidad > linea.cantidad:
            raise ValueError(f"{linea.sku.codigo}: el pedido lleva {linea.cantidad} pieza(s), no {cantidad}.")
    cajas_elegidas = {}
    if tipo == Compensacion.TIPO_REPOSICION:
        if not seleccion:
            raise ValueError("Elige qué productos se reponen.")
        from apps.pedidos.services import piezas_reponibles  # lazy por contrato

        reponibles = piezas_reponibles(incidencia.pedido)
        for linea, cantidad, caja in seleccion:
            info = reponibles.get(linea.pk)
            if info is None:
                raise ValueError(f"{linea.sku.codigo}: no ha salido de bodega; eso se corrige en el pedido, no se repone.")
            if cantidad > info["piezas"]:
                raise ValueError(f"{linea.sku.codigo}: salieron {info['piezas']} pieza(s); no se reponen {cantidad}.")
            if caja is None and info["por_caja"]:
                caja = next(iter(info["por_caja"]))  # la única que lo llevó, o la primera
            if caja is not None:
                en_caja = info["por_caja"].get(caja)
                if en_caja is None:
                    raise ValueError(f"{linea.sku.codigo}: esa caja no llevaba ese producto.")
                if cantidad > en_caja["piezas"]:
                    raise ValueError(
                        f"{linea.sku.codigo}: en la caja {en_caja['numero']} salieron {en_caja['piezas']} pieza(s); "
                        f"no se reponen {cantidad} de esa caja (registra otra reposición para la otra caja)."
                    )
                cajas_elegidas[linea.pk] = (caja, en_caja["numero"])
        monto = sum((Decimal(linea.sku.precio_declarado or 0) * cantidad for linea, cantidad, _ in seleccion), Decimal(0))
        reembolsar_envio = False
    elif tipo == Compensacion.TIPO_REEMBOLSO:
        if not seleccion and not reembolsar_envio and not (monto and monto > 0):
            raise ValueError("Elige qué se reembolsa: productos, el envío o un monto.")
        if seleccion:
            monto = sum((_precio_linea(linea) * cantidad for linea, cantidad, _ in seleccion), Decimal(0))
        elif monto is None:
            monto = Decimal(0)  # solo envío: Shopify dice cuánto
    else:
        if not monto or monto <= 0:
            raise ValueError("Captura el monto del cupón en MXN.")
        reembolsar_envio = False
    comp = Compensacion.objects.create(
        incidencia=incidencia, tipo=tipo, monto=monto,
        lineas=[
            {
                "linea_id": linea.pk, "sku": linea.sku.codigo, "cantidad": cantidad,
                "caja_id": cajas_elegidas.get(linea.pk, (None, None))[0],
                "caja": cajas_elegidas.get(linea.pk, (None, None))[1],
            }
            for linea, cantidad, _ in seleccion
        ],
        reembolsar_envio=bool(reembolsar_envio), avisar_comprador=bool(avisar_comprador),
        creada_por=Compensacion.CREADA_CLIENTE if rol == "cliente" else Compensacion.CREADA_MESA,
        motivo=(motivo or "")[:20] if tipo == Compensacion.TIPO_REPOSICION else "",
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
    """[(línea, cantidad, caja origen pk|None)] guardados en la compensación."""
    from apps.pedidos.models import LineaPedido  # lazy por contrato

    por_id = {l.pk: l for l in LineaPedido.objects.filter(pk__in=[x.get("linea_id") for x in comp.lineas]).select_related("sku")}
    return [
        (por_id[x["linea_id"]], int(x["cantidad"]), x.get("caja_id"))
        for x in comp.lineas if x.get("linea_id") in por_id
    ]


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
            marcar_guias_sustituidas(comp, actor)
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


def marcar_guias_sustituidas(comp, actor):
    """Las guías de las cajas que contenían lo repuesto quedan "sustituidas"
    con el motivo de la compensación (Chema 2026-09-29): el poller las sigue,
    pero lo que reporten ya no mueve el pedido. Regresa las guías marcadas."""
    from apps.envios.models import Guia, PaqueteLinea  # lazy: modelos de otra app

    # Solo las cajas elegidas como origen (2026-10-05); compensaciones viejas
    # sin caja: todas las despachadas que llevaban el producto, sin contar las
    # que lo reponen.
    cajas_ids = {x["caja_id"] for x in comp.lineas if x.get("caja_id")}
    if not cajas_ids:
        lineas_ids = [x.get("linea_id") for x in comp.lineas if x.get("linea_id")]
        cajas_ids = set(
            PaqueteLinea.objects.filter(linea_pedido_id__in=lineas_ids, repone_a__isnull=True, paquete__estado="DESPACHADO")
            .values_list("paquete_id", flat=True)
        )
    guias = list(
        Guia.objects.filter(paquete_id__in=cajas_ids, sustituida_motivo="")
        .exclude(estado=Guia.CANCELADA).exclude(carrier="local")
    )
    ahora = timezone.now()
    for g in guias:
        g.sustituida_motivo = comp.motivo or "otro"
        g.ts_sustituida = ahora
        g.save(update_fields=["sustituida_motivo", "ts_sustituida"])
        registrar_evento(
            "guia", g.pk, "guia_sustituida", actor=actor, cliente=comp.incidencia.cliente,
            delta={"numero": g.numero, "motivo": g.sustituida_motivo, "incidencia": comp.incidencia.folio},
            motivo="Su contenido se repone en otra caja; la guía se sigue rastreando aparte.",
        )
    return guias


def entrega_duplicada(guia, descripcion=""):
    """El carrier entregó una guía ya sustituida: nota en las incidencias
    abiertas del pedido (o en la última) para que el cliente decida si
    recupera el producto; el pedido no se mueve."""
    pedido = guia.pedido
    casos = list(Incidencia.objects.filter(pedido=pedido).exclude(estado=Incidencia.CERRADA).order_by("-pk")) \
        or list(Incidencia.objects.filter(pedido=pedido).order_by("-pk")[:1])
    texto = (
        f"Entrega duplicada: el carrier entregó la guía {guia.numero} ({guia.carrier}) después de que su "
        f"contenido se repuso ({guia.get_sustituida_motivo_display().lower()}). {descripcion}".strip()
    )[:900]
    for inc in casos:
        responder(inc, "Torre", MensajeIncidencia.ROL_SISTEMA, texto)
    registrar_evento("guia", guia.pk, "entrega_duplicada", cliente=pedido.cliente,
                     delta={"numero": guia.numero, "incidencias": [i.folio for i in casos]}, motivo=texto[:300])
    return casos


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


# ── Lista por pedido (Chema 2026-09-29) ──

def resolucion_de(incidencia):
    """Cómo se resolvió: los tipos de compensación del caso ("Reposición
    física, Reembolso"); "" sin compensaciones. La reposición es solución,
    no tipo de incidencia."""
    return ", ".join(sorted({c.get_tipo_display() for c in incidencia.compensaciones.all()}))


def _ultima_actividad(incidencia):
    marcas = [incidencia.ts_apertura, getattr(incidencia, "ultimo_mensaje", None),
              incidencia.ts_resolucion, incidencia.ts_cierre]
    return max(m for m in marcas if m is not None)


def orden_incidencia(incidencia):
    """Abiertas primero (P1 antes que P3), cerradas siempre al fondo; dentro
    de cada bloque, la de actividad más reciente arriba."""
    return (not incidencia.abierta, incidencia.prioridad if incidencia.abierta else "P9",
            -incidencia.ultima_actividad.timestamp())


def agrupar_por_pedido(incidencias):
    """(grupos, sueltas): un grupo por pedido con sus incidencias ordenadas
    (orden_incidencia), las abiertas, la prioridad más alta abierta, si hay
    alguna interna abierta y la última actividad; `sueltas` = sin pedido
    (descuadres de inventario). Los grupos con algo abierto van primero, por
    prioridad y actividad. Cada incidencia queda con `resolucion` y
    `ultima_actividad` colgadas (el caller manda el queryset con
    prefetch de compensaciones y la anotación `ultimo_mensaje`)."""
    grupos, sueltas = {}, []
    for inc in incidencias:
        inc.resolucion = resolucion_de(inc)
        inc.ultima_actividad = _ultima_actividad(inc)
        if inc.pedido_id is None:
            sueltas.append(inc)
            continue
        grupos.setdefault(inc.pedido_id, {"pedido": inc.pedido, "incidencias": []})["incidencias"].append(inc)
    for g in grupos.values():
        g["incidencias"].sort(key=orden_incidencia)
        abiertas = [i for i in g["incidencias"] if i.abierta]
        g["abiertas"] = abiertas
        g["cerradas"] = len(g["incidencias"]) - len(abiertas)
        g["prioridad"] = min((i.prioridad for i in abiertas), default="")
        g["interna"] = any(i.interna for i in abiertas)
        g["ultima_actividad"] = max(i.ultima_actividad for i in g["incidencias"])
    ordenados = sorted(grupos.values(), key=lambda g: (not g["abiertas"], g["prioridad"] or "P9", -g["ultima_actividad"].timestamp()))
    sueltas.sort(key=orden_incidencia)
    return ordenados, sueltas


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
    # Sin paquetería el pedido no puede salir: detenido en piso hasta que Mesa elija (2026-10-06).
    if not pedido.detenido and pedido.estado in ("PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA", "PARCIALMENTE_DESPACHADO"):
        pedido.detenido = True
        pedido.asignado_a = None
        pedido.save(update_fields=["detenido", "asignado_a", "actualizado"])
        registrar_evento(
            "pedido", pedido.pk, "pedido_detenido", cliente=pedido.cliente,
            delta={"origen": "auto", "estado": pedido.estado, "de": None}, motivo=texto[:300],
        )
    return incidencia


def producto_no_registrado_abierta(pedido):
    """La incidencia interna "Producto no registrado" abierta del pedido, o None."""
    return (
        Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_SKU, interna=True)
        .exclude(estado=Incidencia.CERRADA).order_by("-pk").first()
    )


def texto_productos_no_registrados(tienda, desconocidos):
    """Texto del caso: un renglón por producto con título, cantidad, ids y el
    link al producto en el admin de Shopify (para darlo de alta desde ahí)."""
    renglones = []
    for d in desconocidos:
        link = ""
        if tienda is not None and d.get("product_id"):
            link = f"https://{tienda.dominio}/admin/products/{d['product_id']}"
            if d.get("variant_id"):
                link += f"/variants/{d['variant_id']}"
        renglones.append(
            f"{d.get('titulo') or '?'} × {d.get('cantidad') or '?'}"
            + (f" · SKU en Shopify: {d['sku']}" if d.get("sku") else " · sin SKU en Shopify")
            + (f" · variante {d['variant_id']}" if d.get("variant_id") else "")
            + (f" · {link}" if link else "")
        )
    return "Producto(s) no registrado(s) en Torre: " + "; ".join(renglones) + ". Dar de alta el producto (y recibirlo) y luego «Volver a leer la orden»."


def resolver_producto_no_registrado(pedido, actor, motivo=""):
    """Todas las líneas del pedido existen y reservaron: la incidencia SKU se
    resuelve y el pedido se reanuda (hook _reanudar_si_detenia). Regresa la
    incidencia resuelta o None si no había."""
    inc = producto_no_registrado_abierta(pedido)
    if inc is None:
        return None
    resolver(inc, motivo or "Producto dado de alta y con existencias: el pedido vuelve a la cola.", actor)
    return inc


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
