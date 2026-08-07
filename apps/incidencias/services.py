"""Servicios de dominio de incidencias (contrato CONVENTIONS.md §incidencias).

Consumidores conocidos (llaman lazy a este módulo):
- inventario.registrar_conteo → abrir_incidencia(tipo=DES) al exceder umbral.
- pedidos.ingerir_pedido_shopify → abrir_incidencia(tipo=FAL) sin stock.
- envios.poll_tracking → abrir_incidencia (RET/RF) por intento fallido,
  retorno o silencio del carrier.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento

from .models import Incidencia, MensajeIncidencia

# Prioridad default por tipo cuando quien abre no la especifica.
PRIORIDAD_DEFAULT_POR_TIPO = {
    Incidencia.TIPO_DAN: Incidencia.P1,  # regalo roto = ocasión perdida
    Incidencia.TIPO_RF: Incidencia.P1,   # comprador dice que no recibió
    Incidencia.TIPO_CAN: Incidencia.P1,  # hay que frenar el paquete YA
    Incidencia.TIPO_RET: Incidencia.P2,
    Incidencia.TIPO_FAL: Incidencia.P2,
    Incidencia.TIPO_DIR: Incidencia.P2,
    Incidencia.TIPO_DES: Incidencia.P3,
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


def abrir_incidencia(cliente, tipo, origen, pedido=None, sku=None, texto="", prioridad=None):
    """Abre una incidencia con folio y relojes SLA. N por pedido permitidas.

    - SLA de primera respuesta: 30 min si origen=comprador, 2 h en los demás
      casos (valores canónicos de settings.TORRE).
    - SLA de resolución: 48 h (settings.TORRE).
    - Congela la evidencia del pedido y marca pedido.incidencia_activa.
    - Notifica al cliente vía mensajeria (lazy; tolera módulo ausente).
    """
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
        tipo=tipo,
        origen=origen,
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
        if not pedido.incidencia_activa:
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

    try:
        from apps.mensajeria.services import notificar_cliente_incidencia
    except ImportError:
        pass  # mensajeria aún no existe: la apertura no se bloquea por la notificación
    else:
        notificar_cliente_incidencia(incidencia)

    # Web Push a la Mesa, best-effort TOTAL: un push caído o sin VAPID
    # JAMÁS bloquea la apertura de la incidencia.
    try:
        from apps.mensajeria import push  # lazy por contrato

        sobre = f" · {pedido.folio}" if pedido is not None else ""
        push.enviar_push_a_rol(
            "mesa",
            f"⚠️ Incidencia {incidencia.folio}",
            f"{incidencia.get_tipo_display()} · {cliente.nombre}{sobre}",
            url=f"/mesa/incidencias/{incidencia.pk}/",
        )
    except Exception:
        pass

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
    return incidencia


def cerrar(incidencia, actor):
    """Cierra la incidencia y, si el pedido ya no tiene incidencias abiertas
    (ninguna sin cerrar), desmarca pedido.incidencia_activa."""
    incidencia.transicionar(Incidencia.CERRADA, actor=actor)
    pedido = incidencia.pedido
    if pedido is not None:
        quedan_abiertas = (
            Incidencia.objects.filter(pedido=pedido)
            .exclude(estado=Incidencia.CERRADA)
            .exists()
        )
        if not quedan_abiertas and pedido.incidencia_activa:
            pedido.incidencia_activa = False
            pedido.save(update_fields=["incidencia_activa"])
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
