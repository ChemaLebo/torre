"""Servicios de integración (contrato CONVENTIONS.md § integraciones).

- procesar_webhook(evento)      → ruteo de orders/* a pedidos (lazy).
- encolar_push_inventario(sku)  → cola en BD, idempotente por SKU.
- push_inventario()             → drena la cola: on_hand a TODAS las tiendas del cliente.
- reconciliar_pedidos(tienda)   → polling de respaldo con checkpoint.
- escribir_link_pedido(pedido)  → metafield torre.pedido_url en la orden: link al pedido en el portal.
- marcar_fulfillment / registrar_evento_fulfillment → write-back de fulfillment y avance;
  lo que Shopify rechaza se encola (EscrituraShopifyPendiente) y
  reintentar_escrituras_shopify() lo reintenta desde el cron sync_shopify.

Todo job puede correr dos veces sin duplicar efecto (BLUEPRINT §2.2.7).
"""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.services import registrar_evento

from .models import EscrituraShopifyPendiente, PushInventarioPendiente, SyncLog, Tienda, WebhookEvento
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


# ── Reembolsos y reposiciones (Chema 2026-09-28) ─────────────────────────────

def reembolsar_en_shopify(pedido, lineas=(), reembolsar_envio=False, monto=None, nota="", avisar=True):
    """Refund en Shopify por line items (`lineas` = [(sku, cantidad)]) y/o el
    envío, o por un `monto` libre sin líneas. El monto y las transacciones
    (contra el medio de pago original) los calcula Shopify; sin restock: el
    inventario lo manda Torre y un producto que vuelve entra por reingreso.
    Regresa (gid del refund, monto). ShopifyError si no hay tienda con token,
    si la orden no tiene esas piezas reembolsables o si Shopify rechaza."""
    from decimal import Decimal  # lazy: solo aquí

    tienda = pedido.tienda
    if tienda is None or not pedido.shopify_order_id:
        raise ShopifyError(f"{pedido.folio} no viene de una tienda de Shopify.")
    if not tienda.token:
        raise ShopifyError(f"La tienda {tienda.dominio} no tiene token: configúralo en Mesa → Clientes → tiendas.")
    api = ShopifyClient(tienda)
    try:
        items, transacciones = api.lineas_orden(pedido.shopify_order_id)
        refund_lines = []
        for sku, cantidad in lineas:
            restante = int(cantidad)
            for item in items:
                if item["sku"] != sku or restante <= 0:
                    continue
                toma = min(restante, item["reembolsable"])
                if toma > 0:
                    refund_lines.append({"lineItemId": item["id"], "quantity": toma})
                    restante -= toma
            if restante > 0:
                raise ShopifyError(f"La orden no tiene {cantidad} pieza(s) reembolsable(s) de {sku}.")
        if refund_lines or reembolsar_envio:
            sugerido = api.reembolso_sugerido(pedido.shopify_order_id, refund_lines, reembolsar_envio)
            transacciones_refund = [t for t in sugerido["transacciones"] if t.get("parent_id") and t.get("amount")]
            total = Decimal(str(sugerido["monto"] or 0))
        elif monto and Decimal(str(monto)) > 0:
            padre = next(
                (t for t in transacciones if t["kind"] in ("SALE", "CAPTURE") and t["status"] == "SUCCESS"), None,
            )
            if padre is None:
                raise ShopifyError("La orden no tiene un pago exitoso al que devolverle dinero.")
            total = Decimal(str(monto))
            transacciones_refund = [{"gateway": padre["gateway"], "parent_id": padre["id"], "amount": f"{total:.2f}"}]
        else:
            raise ShopifyError("Nada que reembolsar: elige líneas, el envío o un monto.")
        referencia, monto_final = api.crear_reembolso(
            pedido.shopify_order_id, refund_lines, reembolsar_envio, transacciones_refund, note=nota, notify=avisar,
        )
    except ShopifyError as exc:
        _log_push(tienda, False, f"refund {pedido.folio}: {exc}")
        raise
    monto_final = Decimal(str(monto_final)) if monto_final not in (None, "") else total
    _log_push(tienda, True, f"refund {pedido.folio}: ${monto_final} ({referencia})")
    registrar_evento(
        "pedido", pedido.pk, "reembolso_shopify", cliente=pedido.cliente,
        delta={"refund": referencia, "monto": str(monto_final), "lineas": [list(x) for x in lineas], "envio": bool(reembolsar_envio)},
        motivo=(nota or "Reembolso desde Torre")[:300],
    )
    return referencia, monto_final


def _caja_es_reposicion(caja):
    """True si TODO el contenido de la caja son líneas de reposición: la
    orden ya está fulfilled en Shopify y no hay line items que fulfillear;
    en su lugar, el fulfillment sustituido cambia de guía (_tracking_reposicion)."""
    lineas = list(caja.lineas.select_related("linea_pedido"))
    return bool(lineas) and all(pl.linea_pedido.reposicion_de_id for pl in lineas)


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


def _encolar_escritura(tienda, pedido, accion, clave, datos, error):
    """Una escritura de fulfillment que Shopify rechazó se encola para que el
    cron la reintente (reintentar_escrituras_shopify). Idempotente por
    `clave`: la misma escritura fallando otra vez suma un intento y refresca
    el error, sin renglón nuevo. Best-effort: si la cola misma falla, queda
    solo el SyncLog."""
    ahora = timezone.now()
    horas = settings.TORRE.get("SHOPIFY_REINTENTOS_HORAS", 24)
    try:
        escritura, creada = EscrituraShopifyPendiente.objects.get_or_create(
            clave=clave[:160],
            defaults={
                "tienda": tienda, "pedido": pedido, "accion": accion, "datos": datos,
                "ultimo_error": str(error)[:1000], "vence": ahora + timedelta(hours=horas),
            },
        )
        if not creada:
            escritura.intentos += 1
            escritura.ultimo_error = str(error)[:1000]
            escritura.datos = datos
            escritura.save(update_fields=["intentos", "ultimo_error", "datos", "ultimo_intento"])
        return escritura
    except Exception:  # noqa: BLE001, S110 — la cola jamás rompe al caller
        return None


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


def _evento_inicial(api, tienda, pedido, fid, status, donde, ts=None):
    """Primer evento de avance tras crear el fulfillment (best-effort, con
    SyncLog; si Shopify falla, a la cola de reintentos con el id ya conocido)."""
    if not fid or not status:
        return True
    ts = ts or timezone.now()
    try:
        api.crear_evento_fulfillment(fid, status, happened_at=ts)
    except Exception as exc:  # noqa: BLE001 — best-effort: el fulfillment ya quedó
        _log_push(tienda, False, f"evento {status} de {pedido.folio} ({donde}): {exc}")
        _encolar_escritura(
            tienda, pedido, EscrituraShopifyPendiente.ACCION_EVENTO, f"evento:fid:{fid}:{status}",
            {"fid": fid, "status": status, "ts": ts.isoformat(), "donde": donde}, exc,
        )
        return False
    _log_push(tienda, True, f"evento {status} de {pedido.folio} ({donde})")
    return True


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

    con_cajas = cajas is not None
    cajas = [c for c in (cajas or []) if not c.shopify_fulfillment_id]
    de_reposicion = [c for c in cajas if _caja_es_reposicion(c)]
    cajas = [c for c in cajas if c not in de_reposicion]
    if con_cajas and not cajas and not de_reposicion:
        # Todas las cajas de este manifiesto ya tienen fulfillment (el carrier
        # las recogió antes, o es un reintento): nada que escribir, y JAMÁS
        # caer al fulfillment del pedido entero con cajas aún en bodega.
        _log_push(tienda, True, f"fulfillment: {pedido.folio} caja(s) ya con fulfillment; nada que escribir")
        return True
    numeros, carrier = [], ""
    for guia in pedido.guias.all().order_by("pk"):  # orden estable: caja 1 primero
        if guia.carrier == "local":
            continue  # guía interna (entrega propia, sin carrier): no es un tracking para Shopify
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

    if de_reposicion:
        ok_reposicion = _tracking_reposicion(pedido, tienda, de_reposicion, notificar, evento_inicial)
        if not cajas:
            return ok_reposicion
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
        _encolar_escritura(
            tienda, pedido, EscrituraShopifyPendiente.ACCION_FULFILLMENT, f"fulfillment:{pedido.pk}:pedido",
            {"cajas": [], "evento_inicial": evento_inicial, "notificar": notificar}, exc,
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

    notificar_pendiente = notificar  # lo que hereda el reintento si Shopify falla a medias
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
            notificar_pendiente = False
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
        faltan = sorted(c.pk for c in cajas if not c.shopify_fulfillment_id)
        _encolar_escritura(
            tienda, pedido, EscrituraShopifyPendiente.ACCION_FULFILLMENT,
            f"fulfillment:{pedido.pk}:{','.join(str(pk) for pk in faltan)}",
            {"cajas": faltan, "evento_inicial": evento_inicial, "notificar": notificar_pendiente}, exc,
        )
        return False
    return True


def _fulfillments_sustituidos(pedido, caja):
    """gids de los fulfillments de las cajas cuyo contenido repone `caja`
    (PaqueteLinea → LineaPedido.reposicion_de → caja original →
    Paquete.shopify_fulfillment_id), en orden de caja; sin caja original con
    id, el fulfillment del pedido entero; [] si Torre no guardó ninguno."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    originales = [
        pl.linea_pedido.reposicion_de_id
        for pl in caja.lineas.select_related("linea_pedido") if pl.linea_pedido.reposicion_de_id
    ]
    fids = []
    for fid in (
        Paquete.objects.filter(pedido=pedido, lineas__linea_pedido_id__in=originales)
        .exclude(shopify_fulfillment_id="").order_by("numero")
        .values_list("shopify_fulfillment_id", flat=True)
    ):
        if fid not in fids:
            fids.append(fid)
    if not fids and pedido.shopify_fulfillment_id:
        fids.append(pedido.shopify_fulfillment_id)
    return fids


def _tracking_reposicion(pedido, tienda, cajas, notificar, evento_inicial):
    """Reposición (Chema 2026-09-30): la orden ya está fulfilled, así que la
    caja de reposición no crea fulfillment; el fulfillment de la caja
    sustituida cambia de guía a la nueva (fulfillmentTrackingInfoUpdate) y el
    comprador recibe el correo de envío actualizado de Shopify (`notificar`;
    None = sí). La caja de reposición guarda ese id para que sus eventos de
    avance cuelguen de ahí; con varias cajas originales se actualizan todas y
    los eventos van a la primera. Entrega propia: paquetería "WOP" y la página
    pública de rastreo. Sin fulfillment original en Torre queda en SyncLog
    (corre shopify_eventos_backfill). Shopify caído → cola de reintentos."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.rastreo.services import url_publica  # lazy por contrato

    avisar = True if notificar is None else bool(notificar)
    todo_ok = True
    for caja in cajas:
        guia = caja.guia_activa
        if guia is None:
            _log_push(tienda, False, f"reposición: caja {caja.numero} de {pedido.folio} sin guía activa; sin rastreo que actualizar")
            todo_ok = False
            continue
        fids = _fulfillments_sustituidos(pedido, caja)
        if not fids:
            _log_push(tienda, False, f"reposición: caja {caja.numero} de {pedido.folio} sin fulfillment original en Torre (corre shopify_eventos_backfill)")
            todo_ok = False
            continue
        carrier, numero = ("WOP", "") if guia.carrier == "local" else (guia.carrier, guia.numero)
        try:
            api = ShopifyClient(tienda)
            url = url_publica(pedido)
            for fid in fids:
                api.actualizar_tracking_fulfillment(fid, carrier, numero, url, notificar=avisar)
                avisar = False  # un solo correo por manifiesto
        except Exception as exc:  # noqa: BLE001 — best-effort: Shopify caído no bloquea el manifiesto
            _log_push(tienda, False, f"reposición: caja {caja.numero} de {pedido.folio}: {exc}")
            _encolar_escritura(
                tienda, pedido, EscrituraShopifyPendiente.ACCION_TRACKING, f"tracking:{pedido.pk}:{caja.pk}",
                {"caja": caja.pk, "notificar": avisar, "evento_inicial": evento_inicial}, exc,
            )
            todo_ok = False
            continue
        Paquete.objects.filter(pk=caja.pk).update(shopify_fulfillment_id=fids[0])
        caja.shopify_fulfillment_id = fids[0]
        _log_push(tienda, True, f"reposición: caja {caja.numero} de {pedido.folio} viaja en el fulfillment sustituido ({', '.join(fids)}) con la guía {numero or 'de entrega propia'}")
        registrar_evento(
            "paquete", caja.pk, "tracking_reposicion_shopify", cliente=pedido.cliente,
            delta={"tienda": tienda.dominio, "pedido": pedido.folio, "caja": caja.numero,
                   "guia": numero, "carrier": carrier, "fulfillments": fids},
            motivo=f"Rastreo del fulfillment sustituido actualizado a la guía de la caja {caja.numero} (reposición).",
        )
        _evento_inicial(api, tienda, pedido, fids[0], evento_inicial, f"caja {caja.numero}, reposición")
    return todo_ok


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
    clave = f"evento:guia:{guia.pk}:{status}" if guia is not None else f"evento:pedido:{pedido.pk}:{status}"
    datos = {"guia": guia.pk if guia is not None else None, "estado_guia": estado_guia,
             "descripcion": descripcion, "ts": ts.isoformat() if ts else None}
    if not fid:
        if EscrituraShopifyPendiente.objects.filter(
            pedido=pedido, accion__in=(EscrituraShopifyPendiente.ACCION_FULFILLMENT, EscrituraShopifyPendiente.ACCION_TRACKING),
        ).exists():
            # El fulfillment mismo está en la cola: el evento espera detrás de él.
            _log_push(tienda, True, f"evento {status} de {pedido.folio} espera al fulfillment pendiente (en cola)")
            _encolar_escritura(tienda, pedido, EscrituraShopifyPendiente.ACCION_EVENTO, clave, datos, "espera al fulfillment pendiente")
            return False
        _log_push(tienda, False, f"evento {status} de {pedido.folio}: sin fulfillment id en Torre (corre shopify_eventos_backfill)")
        return False
    try:
        api = ShopifyClient(tienda)
        api.crear_evento_fulfillment(fid, status, happened_at=ts, message=descripcion)
    except Exception as exc:  # noqa: BLE001 — best-effort: Shopify caído no detiene el rastreo
        _log_push(tienda, False, f"evento {status} de {pedido.folio} ({donde}): {exc}")
        _encolar_escritura(tienda, pedido, EscrituraShopifyPendiente.ACCION_EVENTO, clave, datos, exc)
        return False
    _log_push(tienda, True, f"evento {status} de {pedido.folio} ({donde})")
    registrar_evento(
        "guia" if guia is not None else "pedido", guia.pk if guia is not None else pedido.pk,
        "evento_fulfillment_shopify", cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "status": status, "estado_guia": estado_guia, "fulfillment": fid},
        motivo=(descripcion or f"{status} en Shopify")[:300],
    )
    return True


def reintentar_escrituras_shopify():
    """Drena la cola de escrituras de fulfillment que Shopify rechazó
    (EscrituraShopifyPendiente), en orden de llegada: cada una se reproduce
    con la MISMA función que la intentó en línea (idempotentes: caja con id
    guardado no se vuelve a fulfillear, el rastreo se puede volver a fijar, un
    evento repetido es inofensivo) y se borra cuando entra. Las vencidas no se
    tocan: Mesa → Sync las reactiva o descarta. Lo llama sync_shopify (cron
    cada 15 min). Regresa el resumen."""
    resumen = {"pendientes": 0, "ok": 0, "error": 0, "vencidas": 0}
    ahora = timezone.now()
    for escritura in list(EscrituraShopifyPendiente.objects.select_related("tienda", "pedido").order_by("creado")):
        if escritura.vence <= ahora:
            resumen["vencidas"] += 1
            continue
        resumen["pendientes"] += 1
        intentos = escritura.intentos
        try:
            ok = _reintentar_escritura(escritura)
        except Exception as exc:  # noqa: BLE001 — un renglón roto no detiene la cola
            _log_push(escritura.tienda, False, f"reintento {escritura.accion} de {escritura.pedido.folio}: {exc}")
            ok = False
        if ok:
            escritura.delete()
            resumen["ok"] += 1
            continue
        resumen["error"] += 1
        escritura.refresh_from_db()
        if escritura.intentos == intentos:  # la función no volvió a encolar: contar el intento aquí
            escritura.intentos += 1
            escritura.ultimo_error = escritura.ultimo_error or "sin efecto (ver el log de sync)"
            escritura.save(update_fields=["intentos", "ultimo_error", "ultimo_intento"])
    return resumen


def _reintentar_escritura(escritura):
    """Reproduce UNA escritura pendiente; True si entró (o ya no aplica)."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app

    datos = escritura.datos or {}
    pedido = escritura.pedido
    if escritura.accion == EscrituraShopifyPendiente.ACCION_FULFILLMENT:
        cajas = list(Paquete.objects.filter(pedido=pedido, pk__in=datos.get("cajas") or []).order_by("numero"))
        return marcar_fulfillment(
            pedido, cajas=cajas or None, evento_inicial=datos.get("evento_inicial"), notificar=datos.get("notificar"),
        )
    if escritura.accion == EscrituraShopifyPendiente.ACCION_TRACKING:
        caja = Paquete.objects.filter(pedido=pedido, pk=datos.get("caja")).first()
        if caja is None or caja.shopify_fulfillment_id:
            return True  # ya no existe o ya quedó
        return _tracking_reposicion(pedido, escritura.tienda, [caja], datos.get("notificar"), datos.get("evento_inicial"))
    ts = parse_datetime(datos["ts"]) if datos.get("ts") else None
    if datos.get("fid"):
        return _evento_inicial(
            ShopifyClient(escritura.tienda), escritura.tienda, pedido, datos["fid"], datos["status"],
            datos.get("donde", ""), ts=ts,
        )
    guia = Guia.objects.filter(pedido=pedido, pk=datos.get("guia")).first() if datos.get("guia") else None
    if datos.get("guia") and guia is None:
        return True  # la guía ya no existe: nada que reportar
    return registrar_evento_fulfillment(pedido, guia, datos.get("estado_guia"), descripcion=datos.get("descripcion", ""), ts=ts)


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


# ── Link al pedido en el portal (metafield de la orden) ─────────────────────

# Servicio al cliente trabaja con el "nombre" de la orden en el admin de
# Shopify; en vez de guardarlo en Torre, la orden lleva un metafield con el
# link al pedido en el portal (Chema 2026-09-23). Con la definición creada y
# fijada (`crear_definicion_link`), Shopify lo muestra en la página de la orden.
METAFIELD_LINK = {
    "namespace": "torre",
    "key": "pedido_url",
    "type": "url",
    "name": "Pedido en Torre",
    "description": "Abre el pedido en el portal de Torre (requiere usuario del portal).",
}


def url_pedido_portal(pedido):
    """URL absoluta del pedido en el portal del cliente, con la misma base
    pública que los links de rastreo (BASE_URL_PUBLICA)."""
    from django.urls import reverse

    from apps.rastreo.services import _base_publica  # lazy por contrato: misma base que /r/

    return f"{_base_publica()}{reverse('portal:pedido_detalle', args=[pedido.pk])}"


def escribir_link_pedido(pedido):
    """Escribe en la orden de Shopify el metafield `torre.pedido_url` con el
    link al pedido en el portal. Lo dispara la ingesta de una orden nueva (en
    on_commit) y el backfill del command `shopify_metafield_torre`.

    Best-effort: el resultado queda en SyncLog (push); jamás levanta hacia el
    caller. Idempotente: `metafieldsSet` pisa el valor con el mismo link.
    Pedido manual (sin tienda u orden) → False sin log.
    """
    tienda = pedido.tienda
    if tienda is None or not pedido.shopify_order_id:
        return False
    url = url_pedido_portal(pedido)
    if not tienda.token:
        if not settings.DEBUG:
            _log_push(tienda, False, f"link al portal: {pedido.folio} NO se escribió (tienda sin token)")
            return False
        _log_push(tienda, True, f"ok (mock): link al portal de {pedido.folio} → {url}")
        return True
    try:
        ShopifyClient(tienda).set_metafield_orden(
            pedido.shopify_order_id, METAFIELD_LINK["namespace"], METAFIELD_LINK["key"],
            METAFIELD_LINK["type"], url,
        )
    except ShopifyError as exc:
        _log_push(tienda, False, f"link al portal: {pedido.folio}: {exc}")
        return False
    _log_push(tienda, True, f"link al portal: {pedido.folio} → {url}")
    return True


def crear_definicion_link(tienda):
    """Define en la tienda el metafield del link (una vez por tienda): con
    definición y `pin`, el admin de Shopify lo muestra fijo en cada orden.
    Regresa "creada", "existia" o "error"; el detalle queda en SyncLog."""
    if not tienda.token:
        _log_push(tienda, False, "definición del link al portal: tienda sin token")
        return "error"
    definicion = {
        "name": METAFIELD_LINK["name"],
        "namespace": METAFIELD_LINK["namespace"],
        "key": METAFIELD_LINK["key"],
        "type": METAFIELD_LINK["type"],
        "description": METAFIELD_LINK["description"],
        "ownerType": "ORDER",
        "pin": True,
    }
    try:
        creada = ShopifyClient(tienda).crear_definicion_metafield(definicion)
    except ShopifyError as exc:
        _log_push(tienda, False, f"definición del link al portal: {exc}")
        return "error"
    _log_push(tienda, True, "definición del link al portal " + ("creada" if creada else "ya existía"))
    return "creada" if creada else "existia"
