"""Servicios de integración (contrato CONVENTIONS.md § integraciones).

- procesar_webhook(evento)      → ruteo de orders/* a pedidos (lazy).
- encolar_push_inventario(sku)  → cola en BD, idempotente por SKU.
- push_inventario()             → drena la cola: on_hand a TODAS las tiendas del cliente.
- reconciliar_pedidos(tienda)   → polling de respaldo con checkpoint.

Todo job puede correr dos veces sin duplicar efecto (BLUEPRINT §2.2.7).
"""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone

from apps.core.services import registrar_evento

from .models import PushInventarioPendiente, SyncLog, Tienda, WebhookEvento
from .shopify import ErrorItemNoStockeado, ShopifyClient, ShopifyError

TOPICS_PEDIDOS = {"orders/create", "orders/updated", "orders/cancelled"}
# Webhook creado por UI (misma firma de Notifications) AL DESPLEGAR este código
# — un topic sin handler queda "ignorado" (procesado=True) y no es replayable.
TOPICS_FOS = {"fulfillment_orders/moved"}


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
    if evento.topic in TOPICS_FOS:
        return _webhook_fo_movida(evento)
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


def _procesar_fo_movida(evento):
    """fulfillment_orders/moved — matriz de política (moves = acto manual y raro).

    HACIA nosotros → se trae la orden y se re-evalúa por el carril normal
    (upsert idempotente crea si antes se omitió por ajena). A OTRA location →
    la matriz de cancelación decide: sin despachar cancela/libera reservas;
    ya despachado abre incidencia CAN. Payload defensivo — shape se confirma
    en el checklist de conexión de Colima.
    """
    tienda = evento.tienda
    payload = evento.payload or {}
    movido = payload.get("moved_fulfillment_order") or payload.get("fulfillment_order") or {}
    order_id = str(movido.get("order_id") or payload.get("order_id") or "").strip()
    if not order_id:
        return "moved sin order_id: ignorado"
    destino = str(movido.get("assigned_location_id") or "").strip().rsplit("/", 1)[-1]
    nuestra = str(tienda.location_id or "").strip().rsplit("/", 1)[-1]

    if nuestra and destino and destino == nuestra:
        api = ShopifyClient(tienda)
        payload_orden = api.obtener_pedido(order_id)
        if not payload_orden:
            raise ShopifyError(f"orden {order_id} vino vacía tras el move")
        nuevo, creado = registrar_webhook(
            tienda, f"moved:{tienda.pk}:{order_id}:{evento.webhook_id}",
            "orders/updated", payload_orden, origen=WebhookEvento.ORIGEN_RECONCILIACION,
        )
        if creado:
            procesar_webhook(nuevo)
        return f"ticket movido HACIA nosotros: orden {order_id} re-evaluada"

    try:
        from apps.pedidos.models import Pedido  # lazy por contrato
        from apps.pedidos.services import cancelar  # lazy por contrato
    except ImportError:
        return f"moved: módulo pedidos no disponible (orden {order_id})"
    pedido = Pedido.objects.filter(tienda=tienda, shopify_order_id=order_id).first()
    if pedido is None:
        return f"moved a otra location: la orden {order_id} no estaba en Torre"
    try:
        cancelar(pedido, actor="sistema", motivo="Ticket de fulfillment movido a otra location")
    except ValueError as exc:
        registrar_evento(
            "pedido", pedido.pk, "movida_no_aplicable", actor="sistema",
            cliente=pedido.cliente, motivo=str(exc),
        )
        return f"moved: {pedido.folio} en estado no cancelable ({pedido.estado})"
    return f"moved a otra location: {pedido.folio} pasó por la matriz de cancelación"


def _webhook_fo_movida(evento):
    """Mismo contrato de errores que la ingesta: la falla queda en SyncLog y
    el evento sin procesar para replay; el éxito marca procesado + auditoría."""
    tienda = evento.tienda
    try:
        detalle = _procesar_fo_movida(evento)
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
        detalle=f"{evento.topic}: {detalle}",
    )
    registrar_evento(
        "webhook", evento.webhook_id, "procesado", actor=f"shopify:{tienda.dominio}",
        cliente=tienda.cliente, delta={"topic": evento.topic, "origen": evento.origen},
    )
    return None


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


def lineas_fulfillment_nuestras(tienda, order_id):
    """Qué líneas/cantidades de la orden amparan NUESTROS tickets de fulfillment.

    None → sin datos para filtrar (tienda sin token o sin location_id, o la
    orden aún no trae FOs): el caller ingiere la orden completa, el
    comportamiento de siempre. Dict → {"parcial": bool (hay tickets ajenos),
    "cantidades": {line_item_id: piezas}, "fos": [gids nuestros]};
    cantidades vacías = ningún ticket es nuestro. ShopifyError se propaga:
    el webhook queda sin procesar para replay (fail-closed).
    """
    if not tienda.token or not (tienda.location_id or "").strip():
        return None
    api = ShopifyClient(tienda)
    nuestra = api.location_gid
    fos = api.fulfillment_orders_lineas(order_id)
    if not fos:
        return None  # sin FOs (¿routing en curso?): mejor completo que perder la orden
    cantidades, nuestros = {}, []
    ajenos = False
    for fo in fos:
        ubicacion = fo["location_gid"]
        if ubicacion and ubicacion != nuestra:
            ajenos = True
            continue
        nuestros.append(fo["gid"])
        for linea in fo["lineas"]:
            if linea["line_item_id"]:
                cantidades[linea["line_item_id"]] = (
                    cantidades.get(linea["line_item_id"], 0) + linea["cantidad"]
                )
    return {"parcial": ajenos, "cantidades": cantidades, "fos": nuestros}


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
        # Stage 1 multi-location: SOLO se cierran tickets de NUESTRA location.
        # Location nula (borrada en Shopify) cuenta como nuestra — comportamiento
        # legado, jamás estrangula una tienda de una sola bodega. Tienda sin
        # location_id configurado → sin filtro (compat total).
        nuestra = api.location_gid if (tienda.location_id or "").strip() else ""
        estados = api.fulfillment_orders(pedido.shopify_order_id)
        abiertas, ajenas = [], []
        for fid, estado, ubicacion in estados:
            if estado not in _FO_FULFILLEABLES:
                continue
            if nuestra and ubicacion and ubicacion != nuestra:
                ajenas.append(fid)
            else:
                abiertas.append(fid)
        if not abiertas:
            detalle_ajenas = f"; {len(ajenas)} FO de otra location (no se tocan)" if ajenas else ""
            SyncLog.objects.create(
                tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
                detalle=(
                    f"fulfillment: {pedido.folio} sin fulfillment orders nuestras abiertas "
                    f"(estados: {[e for _, e, _ in estados] or 'sin FOs'}){detalle_ajenas}"
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

    detalle_ajenas = f"; {len(ajenas)} FO de otra location intactas" if ajenas else ""
    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_OK,
        detalle=(
            f"fulfillment: {pedido.folio} marcado ({len(abiertas)} FO, "
            f"guías {', '.join(numeros) or '—'}){detalle_ajenas}"
        ),
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

    # Primer sync (checkpoint nulo) = backfill acotado: pagadas + sin
    # fulfillear + ventana BACKFILL_DIAS. Retira el ritual de fijar el
    # checkpoint por consola y el riesgo de jalar años de historia.
    primera = tienda.checkpoint_reconciliacion is None
    try:
        api = ShopifyClient(tienda)
        if primera:
            desde = ahora - timedelta(days=settings.TORRE["BACKFILL_DIAS"])
            pedidos = api.listar_pedidos_backfill(created_at_min=desde)
        else:
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
    etiqueta = (
        f"backfill inicial ({settings.TORRE['BACKFILL_DIAS']}d)" if primera else "reconciliación"
    )
    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_INGESTA, resultado=SyncLog.RESULTADO_OK,
        detalle=f"{etiqueta}: {len(pedidos)} pedidos revisados, {nuevos} nuevos",
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
