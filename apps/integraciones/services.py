"""Servicios de integración (contrato CONVENTIONS.md § integraciones).

- procesar_webhook(evento)      → ruteo de orders/* a pedidos (lazy).
- encolar_push_inventario(sku)  → cola en BD, idempotente por SKU.
- push_inventario()             → drena la cola: on_hand a TODAS las tiendas del cliente.
- reconciliar_pedidos(tienda)   → polling de respaldo con checkpoint.

Todo job puede correr dos veces sin duplicar efecto (BLUEPRINT §2.2.7).
"""
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone

from apps.core.services import registrar_evento

from .models import PushInventarioPendiente, SyncLog, Tienda, WebhookEvento
from .shopify import ErrorItemNoStockeado, ShopifyClient, ShopifyError

TOPICS_PEDIDOS = {"orders/create", "orders/updated", "orders/cancelled"}


# ── Ingesta ──────────────────────────────────────────────────────────────────

def procesar_webhook(evento):
    """Procesa un WebhookEvento guardado: orders/create|updated|cancelled → upsert
    de Pedido vía `apps.pedidos.services.ingerir_pedido_shopify(tienda, payload, origen)`.

    Idempotente: un evento ya procesado es no-op. Nunca truena hacia la vista
    (Shopify reintenta ante non-200; el error queda en SyncLog y el evento queda
    sin procesar para replay).
    """
    if evento.procesado:
        return None

    tienda = evento.tienda
    if evento.topic not in TOPICS_PEDIDOS:
        evento.procesado = True
        evento.save(update_fields=["procesado"])
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_OK,
            detalle=f"topic '{evento.topic}' ignorado (webhook {evento.webhook_id})",
        )
        return None

    try:
        # Lazy: pedidos se construye en paralelo; en integración siempre existe.
        from apps.pedidos.services import ingerir_pedido_shopify
    except ImportError:
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_ERROR,
            detalle=f"módulo pedidos no disponible; webhook {evento.webhook_id} queda para replay",
        )
        return None

    try:
        pedido = ingerir_pedido_shopify(tienda, evento.payload, origen=evento.origen)
    except Exception as exc:  # noqa: BLE001 — el error se registra, el evento queda para replay
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_ERROR,
            detalle=f"{evento.topic} {evento.webhook_id}: {exc}",
        )
        return None

    evento.procesado = True
    evento.save(update_fields=["procesado"])
    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_OK,
        detalle=f"{evento.topic} procesado (webhook {evento.webhook_id}, origen {evento.origen})",
    )
    registrar_evento(
        "webhook", evento.webhook_id, "procesado",
        actor=f"shopify:{tienda.dominio}", cliente=tienda.cliente,
        delta={"topic": evento.topic, "origen": evento.origen},
    )
    return pedido


def registrar_webhook(tienda, webhook_id, topic, payload, origen=WebhookEvento.ORIGEN_WEBHOOK):
    """Guarda el evento con idempotencia por webhook_id.

    Regresa (evento, creado). Si ya existía (reintento de Shopify o replay),
    creado=False y NO se vuelve a procesar.
    """
    try:
        evento, creado = WebhookEvento.objects.get_or_create(
            webhook_id=webhook_id,
            defaults={"tienda": tienda, "topic": topic, "payload": payload, "origen": origen},
        )
    except IntegrityError:
        # Carrera entre dos entregas simultáneas del mismo webhook: gana una sola.
        return WebhookEvento.objects.get(webhook_id=webhook_id), False
    if creado:
        registrar_evento(
            "webhook", webhook_id, "recibido",
            actor=f"shopify:{tienda.dominio}", cliente=tienda.cliente,
            delta={"topic": topic, "origen": origen},
        )
    return evento, creado


# ── Push de inventario ───────────────────────────────────────────────────────

def encolar_push_inventario(sku):
    """Encola un push de inventario para el SKU. Idempotente: una sola entrada
    pendiente por SKU (constraint único); encolar dos veces = no-op."""
    try:
        pendiente, _ = PushInventarioPendiente.objects.get_or_create(sku=sku)
    except IntegrityError:
        pendiente = PushInventarioPendiente.objects.get(sku=sku)
    return pendiente


def calcular_on_hand(sku):
    """on_hand publicado = vendible − cuarentena − buffer del cliente (mínimo 0).

    Shopify deriva available = on_hand − committed; por eso NUNCA restamos aquí
    lo reservado por pedidos que Shopify también descuenta (anti doble descuento).
    Cola baja (≤ UMBRAL_COLA_BAJA): buffer defensivo extra (BUFFER_COLA_BAJA).
    """
    vendible = 0
    cuarentena = 0
    try:
        # Lazy: inventario se construye en paralelo.
        from apps.inventario.models import Saldo
    except ImportError:
        Saldo = None
    if Saldo is not None:
        filas = Saldo.objects.filter(
            sku=sku, estado__in=["ubicado_vendible", "cuarentena"],
        ).values("estado").annotate(total=Sum("cantidad"))
        por_estado = {f["estado"]: f["total"] or 0 for f in filas}
        vendible = por_estado.get("ubicado_vendible", 0)
        cuarentena = por_estado.get("cuarentena", 0)

    buffer_cliente = getattr(sku.cliente, "buffer_stock", 0) or 0
    on_hand = vendible - cuarentena - buffer_cliente
    torre = settings.TORRE
    if on_hand <= torre["UMBRAL_COLA_BAJA"]:
        on_hand -= torre["BUFFER_COLA_BAJA"]
    return max(on_hand, 0)


def _push_a_tienda(tienda, sku, on_hand):
    """Empuja on_hand de un SKU a una tienda. Regresa True si quedó registrado ok."""
    if not tienda.token:
        if not settings.DEBUG:
            # Producción sin token = misconfiguración: error visible (pill roja
            # en Mesa → Sync) y el pendiente SOBREVIVE al siguiente drenado.
            # Jamás "ok (mock)": eso descartaba cambios de stock en silencio.
            SyncLog.objects.create(
                tienda=tienda, direccion=SyncLog.DIRECCION_PUSH,
                resultado=SyncLog.RESULTADO_ERROR,
                detalle=(
                    f"sin token: {sku.codigo} on_hand={on_hand} NO se empujó "
                    "(configura el token en Mesa → Clientes → tiendas)"
                ),
            )
            return False
        # Dev sin credenciales: el efecto externo se simula pero el rastro es real.
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
            detalle=f"ok (mock): {sku.codigo} on_hand={on_hand}",
        )
        return True
    activado = False
    try:
        api = ShopifyClient(tienda)
        item_gid, on_hand_actual = api.consultar_inventario_sku(sku.codigo)
        try:
            api.set_on_hand(item_gid, on_hand, compare_quantity=on_hand_actual)
        except ErrorItemNoStockeado:
            # Producto creado/reactivado después del alta masiva: recibirlo =
            # lo fulfilleamos. Se activa en nuestra location y se reintenta;
            # tras activar el on_hand es 0 (una carrera falla el compare y el
            # siguiente drenado trae snapshot fresco).
            api.activar_inventario(item_gid)
            api.set_on_hand(item_gid, on_hand, compare_quantity=0)
            activado = True
            on_hand_actual = 0
    except Exception as exc:  # noqa: BLE001 — un push caído (ShopifyError, red) no tumba el drenado
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_ERROR,
            detalle=f"{sku.codigo} on_hand={on_hand}: {exc}",
        )
        return False
    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
        detalle=(
            f"{sku.codigo} on_hand={on_hand} (compareQuantity={on_hand_actual})"
            + (" · item activado en la location" if activado else "")
        ),
    )
    return True


def push_inventario():
    """Drena la cola: por cada SKU pendiente empuja on_hand a TODAS las tiendas
    activas de su cliente. SyncLog por tienda; el pendiente solo se borra si
    todas las tiendas quedaron ok (si no, se queda para el siguiente drenado).

    Idempotente: correrlo dos veces con la cola vacía es no-op; con token real,
    compareQuantity evita pisar un snapshot más nuevo.
    """
    resumen = {"skus": 0, "pushes_ok": 0, "pushes_error": 0}
    pendientes = list(
        PushInventarioPendiente.objects.select_related("sku", "sku__cliente").order_by("creado")
    )
    for pendiente in pendientes:
        sku = pendiente.sku
        on_hand = calcular_on_hand(sku)
        tiendas = list(Tienda.objects.filter(cliente=sku.cliente, activo=True))
        todo_ok = True
        for tienda in tiendas:
            if _push_a_tienda(tienda, sku, on_hand):
                resumen["pushes_ok"] += 1
            else:
                todo_ok = False
                resumen["pushes_error"] += 1
        registrar_evento(
            "sku", sku.codigo, "push_inventario",
            cliente=sku.cliente,
            delta={"on_hand": on_hand, "tiendas": [t.dominio for t in tiendas], "ok": todo_ok},
        )
        if todo_ok:
            pendiente.delete()
        resumen["skus"] += 1
    return resumen


# ── Fulfillment (write-back al firmar el manifiesto) ─────────────────────────

# Estados de fulfillment order que SÍ se pueden fulfillear. SCHEDULED y ON_HOLD
# requieren acciones previas del cliente; CLOSED ya está hecho.
_FO_FULFILLEABLES = ("OPEN", "IN_PROGRESS")


def marcar_fulfillment(pedido):
    """Marca el pedido como fulfilled en Shopify, con tracking y notifyCustomer.

    Hermano del "va en camino" de mensajería (mismo momento canónico:
    marcar_recolectado, vía on_commit) — con esto Shopify manda SU correo
    nativo de envío (link a NUESTRA página brandeada) y el admin del cliente
    muestra Fulfilled en vez de quedarse Unfulfilled para siempre.

    Best-effort: el resultado queda en SyncLog; jamás levanta hacia el caller.
    Idempotente sin campo nuevo: sin fulfillment orders abiertas = ya estaba.
    """
    tienda = pedido.tienda
    if tienda is None or not pedido.shopify_order_id:
        return False  # pedido manual: no existe en Shopify

    numeros, carrier = [], ""
    for guia in pedido.guias.all().order_by("pk"):  # orden estable: caja 1 primero
        if guia.es_activa and guia.numero:
            numeros.append(guia.numero)
            carrier = carrier or guia.carrier

    if not tienda.token:
        if not settings.DEBUG:
            # Producción sin token = misconfiguración visible, jamás "ok" falso.
            SyncLog.objects.create(
                tienda=tienda, direccion=SyncLog.DIRECCION_PUSH,
                resultado=SyncLog.RESULTADO_ERROR,
                detalle=f"fulfillment: {pedido.folio} NO se marcó (tienda sin token)",
            )
            return False
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
            detalle=f"ok (mock): fulfillment de {pedido.folio} guías {', '.join(numeros) or '—'}",
        )
        return True

    try:
        from apps.rastreo.services import url_publica  # lazy por contrato
        url_rastreo = url_publica(pedido)
    except ImportError:
        url_rastreo = ""

    try:
        api = ShopifyClient(tienda)
        estados = api.fulfillment_orders(pedido.shopify_order_id)
        abiertas = [fid for fid, estado in estados if estado in _FO_FULFILLEABLES]
        if not abiertas:
            SyncLog.objects.create(
                tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
                detalle=(
                    f"fulfillment: {pedido.folio} sin fulfillment orders abiertas "
                    f"(ya fulfilled o retenido: {[e for _, e in estados] or 'sin FOs'})"
                ),
            )
            return True
        api.crear_fulfillment(abiertas, numeros, url_rastreo, carrier)
    except Exception as exc:  # noqa: BLE001 — best-effort: Shopify caído no bloquea nada
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_ERROR,
            detalle=f"fulfillment {pedido.folio}: {exc}",
        )
        return False

    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
        detalle=f"fulfillment: {pedido.folio} marcado ({len(abiertas)} FO, guías {', '.join(numeros) or '—'})",
    )
    registrar_evento(
        "pedido", pedido.pk, "fulfillment_shopify", cliente=pedido.cliente,
        delta={"tienda": tienda.dominio, "guias": numeros},
        motivo="Fulfillment escrito en Shopify al firmar el manifiesto (notifyCustomer).",
    )
    return True


# ── Reconciliación (polling de respaldo) ─────────────────────────────────────

def _topic_desde_payload(payload):
    """Infere el topic de un pedido traído por polling (sin header de Shopify)."""
    if payload.get("cancelled_at"):
        return "orders/cancelled"
    return "orders/updated"


def reconciliar_pedidos(tienda):
    """Polling `orders.json?updated_at_min=checkpoint` de la tienda. Cada pedido
    entra por el MISMO camino que un webhook (WebhookEvento con webhook_id
    determinista → procesar_webhook), así la idempotencia es una sola.

    Sin token → mock: registra SyncLog y avanza el checkpoint. Regresa cuántos
    pedidos nuevos se procesaron.
    """
    ahora = timezone.now()

    if not tienda.token:
        if not settings.DEBUG:
            # Producción sin token: NO avanzar el checkpoint — avanzarlo quema
            # la ventana de reconciliación y los pedidos de ese lapso quedarían
            # fuera del radar para siempre cuando el token por fin exista.
            SyncLog.objects.create(
                tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA,
                resultado=SyncLog.RESULTADO_ERROR,
                detalle="sin token: reconciliación omitida (checkpoint intacto)",
            )
            return 0
        tienda.checkpoint_reconciliacion = ahora
        tienda.save(update_fields=["checkpoint_reconciliacion"])
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_OK,
            detalle="ok (mock): reconciliación sin token, 0 pedidos",
        )
        return 0

    try:
        api = ShopifyClient(tienda)
        pedidos = api.listar_pedidos(updated_at_min=tienda.checkpoint_reconciliacion)
    except ShopifyError as exc:
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_ERROR,
            detalle=f"reconciliación falló: {exc}",
        )
        return 0

    nuevos = 0
    for payload in pedidos:
        webhook_id = f"recon:{tienda.pk}:{payload.get('id')}:{payload.get('updated_at', '')}"
        evento, creado = registrar_webhook(
            tienda, webhook_id, _topic_desde_payload(payload), payload,
            origen=WebhookEvento.ORIGEN_RECONCILIACION,
        )
        if creado:
            procesar_webhook(evento)
            nuevos += 1

    tienda.checkpoint_reconciliacion = ahora
    tienda.save(update_fields=["checkpoint_reconciliacion"])
    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_OK,
        detalle=f"reconciliación: {len(pedidos)} pedidos revisados, {nuevos} nuevos",
    )
    return nuevos


def reprocesar_pendientes(tienda=None):
    """Replay: reintenta eventos guardados que quedaron sin procesar (p. ej. por
    una caída de pedidos o un error transitorio). Idempotente por diseño."""
    qs = WebhookEvento.objects.filter(procesado=False).order_by("ts")
    if tienda is not None:
        qs = qs.filter(tienda=tienda)
    reprocesados = 0
    for evento in qs:
        if procesar_webhook(evento) is not None or evento.procesado:
            reprocesados += 1
    return reprocesados
