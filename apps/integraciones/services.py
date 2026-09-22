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


def resolver_variantes(tienda, ids):
    """{variant_id: {sku, titulo, producto}} vía GraphQL. None sin token (mock):
    el kit degrada a declararse en empaque, jamás se bloquea la orden."""
    if not tienda.token:
        return None
    return ShopifyClient(tienda).variantes(ids)


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
            api.set_on_hand(item_gid, on_hand, change_from_quantity=on_hand_actual)
        except ErrorItemNoStockeado:
            # Producto creado/reactivado después del alta masiva: recibirlo =
            # lo fulfilleamos. Se activa en nuestra location y se reintenta;
            # tras activar el on_hand es 0 (una carrera falla el compare y el
            # siguiente drenado trae snapshot fresco).
            api.activar_inventario(item_gid)
            api.set_on_hand(item_gid, on_hand, change_from_quantity=0)
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
            f"{sku.codigo} on_hand={on_hand} (changeFromQuantity={on_hand_actual})"
            + (" · item activado en la location" if activado else "")
        ),
    )
    return True


def push_inventario():
    """Drena la cola: por cada SKU pendiente empuja on_hand a TODAS las tiendas
    activas de su cliente. SyncLog por tienda; el pendiente solo se borra si
    todas las tiendas quedaron ok (si no, se queda para el siguiente drenado).

    Idempotente: correrlo dos veces con la cola vacía es no-op; con token real,
    changeFromQuantity evita pisar un snapshot más nuevo.
    """
    resumen = {"skus": 0, "pushes_ok": 0, "pushes_error": 0}
    pendientes = list(
        PushInventarioPendiente.objects.select_related("sku", "sku__cliente").order_by("creado")
    )
    for pendiente in pendientes:
        sku = pendiente.sku
        if getattr(sku, "es_kit", False):
            # Un kit no tiene stock físico: Torre publicaría 0 y estrangularía
            # sus ventas. Su disponibilidad es comercial, no de bodega.
            registrar_evento(
                "sku", sku.codigo, "push_omitido_kit", cliente=sku.cliente,
                motivo="Kit: no se publica inventario a Shopify.",
            )
            pendiente.delete()
            continue
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

# Avance del envío que Shopify muestra como "Delivery status": estado de la
# guía en Torre → FulfillmentEventStatus. Lo que no está aquí no viaja. El
# RECOLECTADO lo pone el manifiesto (CARRIER_PICKED_UP), no el carrier.
EVENTO_FULFILLMENT_POR_ESTADO = {
    "RECOLECTADO": "CARRIER_PICKED_UP",
    "EN_TRANSITO": "IN_TRANSIT",
    "EN_RUTA": "OUT_FOR_DELIVERY",
    "ENTREGADO": "DELIVERED",
    "INTENTO_FALLIDO": "ATTEMPTED_DELIVERY",
    "RETENIDO": "DELAYED",
    "EXCEPCION": "DELAYED",
    "RETORNO": "FAILURE",
}


def _log_push(tienda, ok, detalle):
    SyncLog.objects.create(
        tienda=tienda, direccion=SyncLog.DIRECCION_PUSH,
        resultado=SyncLog.RESULTADO_OK if ok else SyncLog.RESULTADO_ERROR, detalle=detalle,
    )


def _lineas_fulfillment_por_caja(cajas, fos):
    """{caja.pk: [{"fulfillmentOrderId", "fulfillmentOrderLineItems": [{"id", "quantity"}]}]}
    con las unidades de venta de cada caja repartidas sobre los line items
    restantes de NUESTRAS fulfillment orders (por SKU, en orden). Medias cajas
    (fraccion_de > 1): la unidad de venta viaja con la primera caja que lleva
    una parte; la segunda media queda sin líneas propias ([]) y comparte
    fulfillment. None si alguna unidad no encuentra line item (kits, SKU que
    Shopify no conoce, cantidades ya fulfilleadas): el caller cae al
    fulfillment único del pedido."""
    restante, por_sku = {}, {}
    for fo in fos:
        for linea in fo["lineas"]:
            if not linea.get("fo_line_item_id") or linea["cantidad"] <= 0:
                continue
            clave = (fo["gid"], linea["fo_line_item_id"])
            restante[clave] = linea["cantidad"]
            por_sku.setdefault(linea["sku"], []).append(clave)
    partes_vistas, resultado = {}, {}
    for caja in cajas:
        asignacion = {}
        for pl in caja.lineas.select_related("linea_pedido__sku"):
            sku = pl.linea_pedido.sku.codigo
            if pl.fraccion_de > 1:
                vistas = partes_vistas.get(pl.linea_pedido_id, 0)
                partes_vistas[pl.linea_pedido_id] = vistas + pl.cantidad
                unidades = (
                    -(-(vistas + pl.cantidad) // pl.fraccion_de) - (-(-vistas // pl.fraccion_de))
                )
            else:
                unidades = pl.cantidad
            while unidades > 0:
                clave = next((k for k in por_sku.get(sku, []) if restante.get(k, 0) > 0), None)
                if clave is None:
                    return None
                toma = min(unidades, restante[clave])
                restante[clave] -= toma
                unidades -= toma
                items = asignacion.setdefault(clave[0], {})
                items[clave[1]] = items.get(clave[1], 0) + toma
        resultado[caja.pk] = [
            {
                "fulfillmentOrderId": fo_gid,
                "fulfillmentOrderLineItems": [{"id": lid, "quantity": q} for lid, q in items.items()],
            }
            for fo_gid, items in asignacion.items()
        ]
    return resultado


def _id_de_fulfillment(respuesta):
    """gid del fulfillment que regresó fulfillmentCreate; "" si no vino (o mock)."""
    fid = respuesta.get("id") if isinstance(respuesta, dict) else ""
    return fid if isinstance(fid, str) else ""


def _evento_inicial(api, tienda, pedido, fid, status, donde):
    """Primer evento de avance tras crear el fulfillment (best-effort, con SyncLog)."""
    if not fid or not status:
        return
    try:
        api.crear_evento_fulfillment(fid, status, happened_at=timezone.now())
    except Exception as exc:  # noqa: BLE001 — best-effort: el fulfillment ya quedó
        _log_push(tienda, False, f"evento {status} de {pedido.folio} ({donde}): {exc}")
    else:
        _log_push(tienda, True, f"evento {status} de {pedido.folio} ({donde})")


def marcar_fulfillment(pedido, cajas=None, evento_inicial="CARRIER_PICKED_UP", notificar=None):
    """Escribe en Shopify el fulfillment del pedido (o de sus cajas), con tracking.

    Hermano del "va en camino" de mensajería (mismo momento canónico:
    marcar_recolectado, vía on_commit) — con esto Shopify manda SU correo
    nativo de envío (link a NUESTRA página brandeada) y el admin del cliente
    muestra Fulfilled en vez de quedarse Unfulfilled para siempre.

    Con `cajas` (las que subieron a ESTE manifiesto) hay UN fulfillment POR
    CAJA: sus líneas (PaqueteLinea → line items de nuestras fulfillment
    orders, por SKU), su guía y la página brandeada; Shopify muestra
    Partially fulfilled entre manifiestos y cada caja lleva su propio
    "Delivery status". Si las líneas no se pueden separar (kits, SKU que
    Shopify no conoce), el fulfillment del pedido entero se escribe cuando
    sale la última caja, como antes. Sin `cajas` (pedido sin plan, entrega
    en bodega): un fulfillment con todas las FOs abiertas y todas las guías
    activas. El id que regresa Shopify se guarda (Paquete.shopify_fulfillment_id
    / Pedido.shopify_fulfillment_id) y de él cuelgan los eventos de avance
    (registrar_evento_fulfillment); tras crearlo se manda `evento_inicial`
    (CARRIER_PICKED_UP al firmar el manifiesto, DELIVERED en la entrega en
    bodega, None = ninguno). Solo el PRIMER fulfillment del pedido notifica al
    comprador: un correo de envío, no uno por caja. `notificar` lo fija el
    caller cuando sabe más: marcar_recolectado manda True en el primer
    manifiesto de cada OLA (fulfillment parcial: la segunda salida, días
    después, sí merece su correo) y False en los siguientes; None = la regla
    de "primer fulfillment del pedido".

    Best-effort: el resultado queda en SyncLog; jamás levanta hacia el caller.
    Idempotente: caja con id guardado, pedido con id guardado o sin fulfillment
    orders abiertas = ya estaba.
    """
    tienda = pedido.tienda
    if tienda is None or not pedido.shopify_order_id:
        return False  # pedido manual: no existe en Shopify

    cajas = [c for c in (cajas or []) if not c.shopify_fulfillment_id]
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
        url_rastreo = url_publica(pedido) if numeros else ""  # sin guía (entrega en bodega): sin rastreo
    except ImportError:
        url_rastreo = ""

    if cajas:
        return _fulfillment_por_caja(pedido, tienda, cajas, url_rastreo, evento_inicial, notificar)
    if pedido.shopify_fulfillment_id:
        _log_push(tienda, True, f"fulfillment: {pedido.folio} ya tiene fulfillment ({pedido.shopify_fulfillment_id})")
        return True

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
        if notificar is None:
            ya_notificado = pedido.paquetes.exclude(shopify_fulfillment_id="").exists()
        else:
            ya_notificado = not notificar
        respuesta = api.crear_fulfillment(
            abiertas, numeros, url_rastreo, carrier, notificar=not ya_notificado,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: Shopify caído no bloquea nada
        SyncLog.objects.create(
            tienda=tienda, direccion=SyncLog.DIRECCION_PUSH, resultado=SyncLog.RESULTADO_ERROR,
            detalle=f"fulfillment {pedido.folio}: {exc}",
        )
        return False

    fid = _id_de_fulfillment(respuesta)
    if fid:
        type(pedido).objects.filter(pk=pedido.pk).update(shopify_fulfillment_id=fid)
        pedido.shopify_fulfillment_id = fid
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
        delta={"tienda": tienda.dominio, "guias": numeros, "fulfillment": fid},
        motivo="Fulfillment escrito en Shopify al firmar el manifiesto (notifyCustomer).",
    )
    _evento_inicial(api, tienda, pedido, fid, evento_inicial, "pedido entero")
    return True


def _fulfillment_por_caja(pedido, tienda, cajas, url_rastreo, evento_inicial, notificar=None):
    """Un fulfillment por caja que sale (ver marcar_fulfillment)."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    try:
        api = ShopifyClient(tienda)
        nuestra = api.location_gid if (tienda.location_id or "").strip() else ""
        abiertas, ajenas = [], []
        for fo in api.fulfillment_orders_lineas(pedido.shopify_order_id):
            if fo["status"] not in _FO_FULFILLEABLES:
                continue
            if nuestra and fo["location_gid"] and fo["location_gid"] != nuestra:
                ajenas.append(fo["gid"])
            else:
                abiertas.append(fo)
        if not abiertas:
            detalle_ajenas = f"; {len(ajenas)} FO de otra location (no se tocan)" if ajenas else ""
            _log_push(tienda, True, f"fulfillment: {pedido.folio} sin fulfillment orders nuestras abiertas{detalle_ajenas}")
            return True
        lineas_por_caja = _lineas_fulfillment_por_caja(cajas, abiertas)
        if lineas_por_caja is None:
            # Líneas no separables por caja (kits, SKU ajeno a Shopify): el
            # pedido entero se fulfillea cuando salga su última caja.
            if pedido.paquetes.filter(estado=Paquete.EMPACADO).exists():
                _log_push(tienda, True, f"fulfillment: {pedido.folio} espera a la última caja (líneas no separables por caja)")
                return True
            return marcar_fulfillment(pedido, evento_inicial=evento_inicial, notificar=notificar)
        if notificar is None:
            ya_notificado = bool(pedido.shopify_fulfillment_id) or pedido.paquetes.exclude(
                shopify_fulfillment_id="",
            ).exists()
        else:
            ya_notificado = not notificar
        for caja in cajas:
            lineas = lineas_por_caja.get(caja.pk) or []
            guia = caja.guia_activa
            if not lineas:
                # Segunda media de una caja de 24: sus unidades ya viajan en el
                # fulfillment de la caja hermana; comparte id para los eventos.
                hermana = pedido.paquetes.exclude(shopify_fulfillment_id="").order_by("numero").first()
                if hermana is not None:
                    Paquete.objects.filter(pk=caja.pk).update(shopify_fulfillment_id=hermana.shopify_fulfillment_id)
                    caja.shopify_fulfillment_id = hermana.shopify_fulfillment_id
                _log_push(tienda, True, f"fulfillment: caja {caja.numero} de {pedido.folio} sin líneas propias (comparte el de la caja {hermana.numero if hermana else '?'})")
                continue
            numeros = [guia.numero] if guia is not None and guia.numero else []
            respuesta = api.crear_fulfillment(
                [l["fulfillmentOrderId"] for l in lineas], numeros, url_rastreo if numeros else "",
                guia.carrier if guia is not None else "", notificar=not ya_notificado, lineas=lineas,
            )
            ya_notificado = True
            fid = _id_de_fulfillment(respuesta)
            if fid:
                Paquete.objects.filter(pk=caja.pk).update(shopify_fulfillment_id=fid)
                caja.shopify_fulfillment_id = fid
            _log_push(tienda, True, f"fulfillment: caja {caja.numero} de {pedido.folio} marcada (guía {', '.join(numeros) or '—'}, {fid or 'sin id'})")
            registrar_evento(
                "paquete", caja.pk, "fulfillment_shopify", cliente=pedido.cliente,
                delta={"tienda": tienda.dominio, "pedido": pedido.folio, "caja": caja.numero,
                       "guias": numeros, "fulfillment": fid},
                motivo=f"Fulfillment de la caja {caja.numero} de {pedido.folio} escrito en Shopify al firmar su manifiesto.",
            )
            _evento_inicial(api, tienda, pedido, fid, evento_inicial, f"caja {caja.numero}")
    except Exception as exc:  # noqa: BLE001 — best-effort: Shopify caído no bloquea nada
        _log_push(tienda, False, f"fulfillment por caja {pedido.folio}: {exc}")
        return False
    return True


def registrar_evento_fulfillment(pedido, guia, estado_guia, descripcion="", ts=None):
    """Avance de una guía → FulfillmentEvent en Shopify ("Delivery status").

    El evento cuelga del fulfillment de la caja de la guía
    (Paquete.shopify_fulfillment_id) o, sin caja, del fulfillment del pedido
    (Pedido.shopify_fulfillment_id). Sin id guardado (pedidos anteriores al
    backfill `shopify_eventos_backfill`, Shopify caído al fulfillear) no hay
    dónde colgarlo: queda en SyncLog y regresa False. Estados sin equivalente
    (EVENTO_FULFILLMENT_POR_ESTADO) no viajan. DELIVERED sobre el fulfillment
    del pedido entero (varias guías en uno) solo cuando TODAS sus guías
    activas están entregadas: una caja entregada no entrega el pedido.
    `ts` = hora real del carrier. Best-effort: jamás levanta.
    """
    tienda = pedido.tienda
    if tienda is None or not pedido.shopify_order_id or not tienda.token:
        return False
    status = EVENTO_FULFILLMENT_POR_ESTADO.get(estado_guia or "")
    if not status:
        return False
    fid = guia.paquete.shopify_fulfillment_id if guia is not None and guia.paquete_id else ""
    donde = f"caja {guia.paquete.numero}" if fid else "pedido entero"
    if not fid:
        fid = pedido.shopify_fulfillment_id
        if fid and status == "DELIVERED":
            activas = [g for g in pedido.guias.all() if g.es_activa]
            if any(g.estado != "ENTREGADO" for g in activas):
                _log_push(tienda, True, f"evento DELIVERED de {pedido.folio} espera a las demás guías")
                return False
    if not fid:
        _log_push(tienda, False, f"evento {status} de {pedido.folio}: sin fulfillment id en Torre (corre shopify_eventos_backfill)")
        return False
    try:
        api = ShopifyClient(tienda)
        api.crear_evento_fulfillment(fid, status, happened_at=ts, message=descripcion)
    except Exception as exc:  # noqa: BLE001 — best-effort: Shopify caído no detiene el rastreo
        _log_push(tienda, False, f"evento {status} de {pedido.folio} ({donde}): {exc}")
        return False
    _log_push(tienda, True, f"evento {status} de {pedido.folio} ({donde})")
    registrar_evento(
        "guia" if guia is not None else "pedido", guia.pk if guia is not None else pedido.pk,
        "evento_fulfillment_shopify", cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "status": status, "estado_guia": estado_guia, "fulfillment": fid},
        motivo=(descripcion or f"{status} en Shopify")[:300],
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
