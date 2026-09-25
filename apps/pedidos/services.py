"""Servicios de dominio de pedidos (contrato CONVENTIONS.md §pedidos).

Reglas duras que este módulo garantiza:
- Todo cambio de estado pasa por Pedido.transicionar() (valida + evento).
- Todo efecto sobre stock pasa por apps.inventario.services (import lazy).
- La plantilla B ("en camino") se dispara SOLO en marcar_recolectado —
  jamás se marca enviado un paquete que sigue en la bodega.
- Errores de validación → ValueError con mensaje claro para el operador:
  qué pasó y qué hacer.
"""
from datetime import time, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento

from .models import LineaPedido, Pedido

# Evidencia de empaque (C1 — fin de la paradoja de la evidencia): al empacar
# la guía AÚN NO existe, así que aquí solo se exige la foto del CONTENIDO.
# La foto de la caja cerrada (con la etiqueta ya pegada) se toma después y se
# valida en cerrar_caja().
FOTOS_CONTENIDO_MINIMAS = 1
TIPO_FOTO_CONTENIDO = "contenido"
TIPO_FOTO_CIERRE = "caja_cerrada"


# ── Helpers de parámetros canónicos ──

def _corte_contractual():
    """Corte vigente desde settings.TORRE — un solo número por promesa."""
    crudo = str(settings.TORRE["CORTE_CONTRACTUAL"])
    hora, minuto = crudo.split(":")
    return time(int(hora), int(minuto))


def _es_local(cp):
    """Zona de entrega local propia: CDMX (bodega en Olivar de los Padres, 01780)."""
    if not cp:
        return False
    cp = str(cp).strip().zfill(5)
    prefijos = settings.TORRE.get("CP_LOCAL_PREFIJOS") or [settings.TORRE.get("CP_LOCAL_PREFIJO", "01")]
    return cp[:2] in {str(p).zfill(2) for p in prefijos}


def _actor_nombre(actor):
    if actor is None:
        return ""
    return getattr(actor, "username", None) or str(actor)


def _datos_comprador(payload):
    cust = payload.get("customer") or {}
    direccion = payload.get("shipping_address") or {}
    nombre = (direccion.get("name")
              or " ".join(p for p in [cust.get("first_name"), cust.get("last_name")] if p)).strip()
    tel = str(direccion.get("phone") or cust.get("phone") or payload.get("phone") or "").strip()
    email = str(payload.get("email") or cust.get("email") or "").strip()
    return nombre[:120], tel[:20], email


# ── Helpers compartidos del alta de pedido (ingesta Shopify y alta manual) ──

def _reservar_linea(linea):
    """Aparta el stock de una línea vía inventario.reservar; marca linea.reservada.

    Regresa False si no alcanzó el stock (la línea queda sin reservar).
    """
    from apps.inventario.services import reservar  # lazy: servicio de otra app
    if reservar(linea.sku, linea.cantidad, linea.pedido.folio):
        linea.reservada = True
        linea.save(update_fields=["reservada"])
        return True
    return False


# Estados en los que una línea sin reserva vuelve a intentar: el pedido aún no
# empieza (PENDIENTE) o ya salió una parte y espera inventario del resto
# (fulfillment parcial). Nunca a media ola: lo que llegue durante el picking
# espera a que salga esa caja y va en la siguiente.
_ESTADOS_REINTENTO = (Pedido.PENDIENTE, Pedido.PARCIALMENTE_DESPACHADO)


def reintentar_reservas_sku(sku):
    """Reintenta reservar líneas sin apartar de este SKU en pedidos PENDIENTES
    o que esperan inventario tras una salida parcial.

    FIFO por antigüedad del pedido; una línea reserva completa o nada (misma
    semántica que la ingesta). Lo dispara inventario cuando ENTRA stock
    (putaway, ajuste, liberación por cancelación), en on_commit. La incidencia
    FAL no se resuelve sola: el evento avisa y un humano la cierra. Tras cada
    reserva, _tras_reserva replanea (PENDIENTE) o reabre la segunda ola.
    """
    lineas = (
        LineaPedido.objects.select_related("pedido", "pedido__cliente", "sku")
        .filter(sku=sku, reservada=False, pedido__estado__in=_ESTADOS_REINTENTO)
        .order_by("pedido__creado", "pk")
    )
    logradas = []
    for linea in lineas:
        if not _reservar_linea(linea):
            continue  # sin stock para esta; una posterior podría pedir menos piezas
        pedido = linea.pedido
        completo = not pedido.lineas.filter(reservada=False).exists()
        registrar_evento(
            "pedido", pedido.pk, "reserva_reintentada", actor="sistema",
            cliente=pedido.cliente,
            delta={"sku": sku.codigo, "cantidad": linea.cantidad, "pedido_completo": completo},
            motivo="Entró stock y la reserva pendiente se completó sola.",
        )
        _tras_reserva(pedido, "sistema")
        logradas.append(pedido.folio)
    return logradas


def reintentar_reservas_pedido(pedido, actor):
    """Botón de Mesa: reintenta las líneas sin reservar de ESTE pedido
    (PENDIENTE, o esperando inventario tras una salida parcial).

    Salta la fila FIFO a propósito: es la palanca humana para priorizar.
    """
    if pedido.estado not in _ESTADOS_REINTENTO:
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display()}: solo reservan stock los "
            "pedidos PENDIENTES o los que esperan inventario tras una salida parcial."
        )
    pendientes = list(pedido.lineas.select_related("sku").filter(reservada=False))
    if not pendientes:
        return f"{pedido.folio} ya tiene todas sus líneas reservadas."
    con_stock = [linea for linea in pendientes if _reservar_linea(linea)]
    registrar_evento(
        "pedido", pedido.pk, "reserva_reintentada", actor=actor, cliente=pedido.cliente,
        delta={
            "logradas": [l.sku.codigo for l in con_stock],
            "sin_stock": [l.sku.codigo for l in pendientes if not l.reservada],
        },
        motivo="Reintento manual de reservas desde Mesa de Control.",
    )
    if con_stock:
        _tras_reserva(pedido, actor)
    if len(con_stock) == len(pendientes):
        if pedido.estado == Pedido.PENDIENTE and pedido.tiene_despachadas:
            return f"{pedido.folio}: ya tiene su inventario; vuelve a picking para completarse."
        return (
            f"{pedido.folio}: todas sus líneas quedaron reservadas. "
            "Si su incidencia FAL sigue abierta, resuélvela."
        )
    return (
        f"{pedido.folio}: {len(con_stock)} de {len(pendientes)} líneas reservadas; "
        "al resto le sigue faltando stock."
    )


def _tras_reserva(pedido, actor):
    """Qué sigue cuando una línea faltante consigue reserva. PENDIENTE (la ola
    no empezó): se replanean las cajas con la línea nueva (force: aún no hay
    guías). Esperando inventario tras una salida parcial: cuando ya no queda
    ninguna faltante, el pedido vuelve a PENDIENTE (segunda ola, mismo folio),
    sin dueño, y se planean cajas nuevas solo con lo pendiente. Con más de una
    faltante se espera a tener todas: una sola segunda salida, no varias."""
    if pedido.estado == Pedido.PENDIENTE:
        _planificar_best_effort(pedido, force=True)
        return
    if pedido.estado != Pedido.PARCIALMENTE_DESPACHADO or pedido.tiene_faltantes:
        return
    pedido.asignado_a = None
    pedido.transferencia_a = None
    pedido.save(update_fields=["asignado_a", "transferencia_a", "actualizado"])
    pedido.transicionar(
        Pedido.PENDIENTE, actor=actor,
        motivo="Llegó el inventario que faltaba: segunda ola con el mismo folio.",
    )
    registrar_evento(
        "pedido", pedido.pk, "pedido_reabierto", actor=actor, cliente=pedido.cliente,
        delta={"pendientes": [
            {"sku": l.sku.codigo, "cantidad": l.pendiente} for l in pedido.lineas_por_surtir
        ]},
        motivo="Fulfillment parcial: lo que faltaba ya tiene reserva y entra a picking.",
    )
    _planificar_best_effort(pedido)


def _incidencias_auto_pausadas(cliente):
    """True si el cliente pausó las incidencias automáticas (incidencias.auto_pausadas);
    False si el módulo no existe. Con pausa, el pedido tampoco se marca incidencia_activa
    (no habría incidencia que cerrar para desmarcarlo)."""
    try:
        from apps.incidencias.services import auto_pausadas  # lazy
    except ImportError:
        return False
    return auto_pausadas(cliente)


def _abrir_incidencia_faltante(pedido, faltantes):
    """Sin stock suficiente al dar de alta el pedido → incidencia FAL automática."""
    texto = "Faltante al ingerir la orden: " + "; ".join(faltantes)
    try:
        from apps.incidencias.services import abrir_incidencia  # lazy
    except ImportError:
        pass
    else:
        abrir_incidencia(pedido.cliente, "FAL", "auto", pedido=pedido, texto=texto)


def _planificar_best_effort(pedido, force=False):
    """División de envío (≤20 kg, optimizada por costo) planificada desde el alta.

    Best-effort: sin plan no se detiene el alta; generar_guias replanifica.
    `force` tira el plan vivo (sin guías) y replanea: una línea faltante que
    consiguió reserva con el pedido aún PENDIENTE.

    Sale en transaction.on_commit: cotizar un lane frío pega a la API real de
    Envia (varios segundos) y el alta corre dentro de un atomic con locks de
    Saldo tomados — otro pedido del mismo SKU (u operador pickeando) quedaría
    esperando detrás de un HTTP externo. Primero el commit, luego el plan.
    """
    def _planificar():
        try:
            from apps.envios.cotizador import planificar_envio  # lazy
            planificar_envio(pedido, force=force)
        except ImportError:
            pass
        except ValueError as exc:
            # Ningún carrier cotiza (Chema 2026-09-24): incidencia interna
            # "Sin paquetería que cotice" en vez de silencio; Mesa elige.
            # Best-effort: el aviso jamás tumba el alta ya committeada.
            try:
                from apps.incidencias.services import abrir_sin_paqueteria  # lazy
                abrir_sin_paqueteria(pedido, str(exc))
            except Exception:  # noqa: BLE001 — sin incidencia queda el evento y el wizard avisa al reintentar
                registrar_evento("pedido", pedido.pk, "sin_paqueteria", cliente=pedido.cliente, motivo=str(exc)[:300])
    transaction.on_commit(_planificar)


def replanear_con_carrier(pedido, carrier, actor, incidencia=None):
    """Mesa fuerza una paquetería para el pedido (Chema 2026-09-24, desde la
    incidencia "Sin paquetería que cotice"): `carrier` = un carrier o "envia"
    (la lista de envia.com por precio). Queda en Pedido.carrier_forzado y
    manda sobre reglas, reparto e integración del cliente. Según el pedido:
    - cajas solo planeadas (o ninguna): se replanea desde cero con ella;
    - cajas ya empacadas (físicas): se recotiza cada caja, sin repartirlas;
    - con guía comprada y nada en la calle: se cancelan las guías y el pedido
      regresa a empaque (regresar_a_empaque) recotizando con ella.
    Un pedido empacado entero (sin cajas) que queda con varias cajas vuelve a
    la mesa sin dueño: se reempaca por caja. Si la paquetería tampoco cotiza,
    no cambia nada (ValueError) y queda la nota en la incidencia; si cotiza,
    la incidencia se resuelve y cierra sola. Regresa {"modo", "cajas"}."""
    from apps.envios.adapters import ErrorCarrier  # lazy por contrato
    from apps.envios.cotizador import planificar_envio  # lazy por contrato
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.envios.services import (  # lazy por contrato
        carriers_del_pedido, etiqueta_paqueteria, opciones_paqueteria, recotizar_paquete,
    )

    carrier = (carrier or "").strip()
    if carrier not in dict(opciones_paqueteria()):
        raise ValueError("Elige una paquetería de la lista.")
    etiqueta = etiqueta_paqueteria(carrier)
    if pedido.estado not in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display().lower()}: la paquetería solo se cambia antes de salir."
        )
    fuera = [c.numero for c in pedido.paquetes.all() if c.estado == Paquete.DESPACHADO]
    if fuera or pedido.tiene_despachadas:
        raise ValueError(f"{pedido.folio} ya salió (caja {', '.join(str(n) for n in fuera) or 'entera'}): no se replanea.")
    fallo = None
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        anterior = fresco.carrier_forzado
        fresco.carrier_forzado = carrier
        fresco.save(update_fields=["carrier_forzado", "actualizado"])
        try:
            # Savepoint: si la paquetería tampoco cotiza, el plan anterior
            # (que el replaneo forzado tira antes de cotizar) se queda intacto.
            with transaction.atomic():
                cajas_previas = list(fresco.paquetes.all())
                if fresco.estado == Pedido.GUIA_GENERADA:
                    regresar_a_empaque(fresco, actor, motivo=f"Cambio de paquetería a {etiqueta}")
                    modo = "recotizadas"
                elif any(c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO) for c in cajas_previas):
                    for caja in cajas_previas:
                        recotizar_paquete(fresco, caja)  # la caja física se queda; solo cambia carrier y precio
                    modo = "recotizadas"
                else:
                    planificar_envio(fresco, force=True)
                    modo = "replaneadas"
                cajas = list(fresco.paquetes.all().order_by("numero"))
                permitidos = set(carriers_del_pedido(fresco))
                fallidas = [c.numero for c in cajas if c.carrier not in permitidos]
                if fallidas:
                    raise ValueError(f"la caja {', '.join(str(n) for n in fallidas)} se quedó sin tarifa de {etiqueta}")
        except (ValueError, ErrorCarrier) as exc:
            fallo = exc
            fresco.carrier_forzado = anterior
            fresco.save(update_fields=["carrier_forzado", "actualizado"])
    if fallo is not None:
        # Fuera del atomic: la nota y el evento sí se guardan aunque se levante el error.
        registrar_evento(
            "pedido", fresco.pk, "replaneo_carrier_fallido", actor=actor, cliente=fresco.cliente,
            delta={"carrier": carrier}, motivo=str(fallo)[:300],
        )
        if incidencia is not None:
            from apps.incidencias.models import MensajeIncidencia  # lazy: modelo de otra app
            from apps.incidencias.services import responder  # lazy por contrato
            responder(incidencia, "Torre", MensajeIncidencia.ROL_SISTEMA,
                      f"{etiqueta} tampoco cotiza {fresco.folio} (CP {fresco.cp}): {str(fallo)[:300]}")
        raise ValueError(f"{etiqueta} tampoco cotiza {fresco.folio} (CP {fresco.cp}): {fallo}") from fallo
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=fresco.pk)
        if modo == "replaneadas" and fresco.estado == Pedido.EMPACADO and fresco.asignado_a_id:
            # Se empacó entero sin plan y ahora hay cajas: lo reempaca quien esté en la mesa.
            fresco.asignado_a = None
            fresco.save(update_fields=["asignado_a", "actualizado"])
        resumen = [(c.numero, c.carrier, float(c.precio_cotizado or 0)) for c in cajas]
        registrar_evento(
            "pedido", fresco.pk, "replaneo_carrier", actor=actor, cliente=fresco.cliente,
            delta={"carrier": carrier, "modo": modo, "cajas": resumen},
            motivo=f"Mesa forzó {etiqueta}: {len(cajas)} caja(s) {modo}.",
        )
        if incidencia is not None and incidencia.abierta:
            from apps.incidencias.services import cerrar, resolver  # lazy por contrato
            detalle = ", ".join(f"caja {n} {c} ${p:.2f}" for n, c, p in resumen)
            resolver(incidencia, f"Replaneado con {etiqueta}: {detalle}.", actor)
            cerrar(incidencia, actor)
    pedido.carrier_forzado = fresco.carrier_forzado
    pedido.estado = fresco.estado
    pedido.asignado_a = fresco.asignado_a
    return {"modo": modo, "cajas": cajas}


def _enviar_confirmacion_best_effort(pedido):
    """Plantilla A (confirmación). mensajeria omite el envío si no hay teléfono.

    Sale en transaction.on_commit: la ingesta/alta corre dentro de un atomic
    con locks tomados y un adapter externo colgado NO debe detener el commit
    (misma regla que el push al piso).
    """
    def _enviar():
        try:
            from apps.mensajeria.services import enviar_confirmacion  # lazy — plantilla A
        except ImportError:
            pass
        else:
            try:
                enviar_confirmacion(pedido)
            except Exception:
                pass  # best-effort: la confirmación jamás tira el alta
    transaction.on_commit(_enviar)


def _avisar_piso_pedido_nuevo(pedido):
    """Web Push al piso: cayó pedido nuevo. Best-effort TOTAL: sin VAPID es
    no-op y un push caído JAMÁS tira la ingesta ni el alta manual.

    Sale en transaction.on_commit: el POST del webhook corre dentro de un
    atomic con select_for_update — un push service colgado con el lock tomado
    apilaría workers y (en SQLite) bloquearía TODA escritura de Torre.
    """
    def _enviar():
        try:
            from apps.mensajeria import push  # lazy por contrato

            lineas = list(pedido.lineas.all())
            piezas = sum(linea.cantidad for linea in lineas)
            sin_inventario = sum(linea.cantidad for linea in lineas if linea.faltante)
            destino = (
                str((pedido.direccion or {}).get("city") or "").strip()
                or pedido.cp or "sin destino"
            )
            if pedido.corte_vigente_al_ingreso:
                corte = pedido.corte_vigente_al_ingreso.strftime("%H:%M")
            else:
                corte = str(settings.TORRE["CORTE_CONTRACTUAL"])
            cuerpo = f"{piezas} pzas · {destino} · corte {corte}"
            if sin_inventario:
                cuerpo += f" · {sin_inventario} sin inventario"
            push.enviar_push_a_rol(
                "piso", f"📦 Nuevo pedido {pedido.folio}", cuerpo, url="/piso/picking/",
            )
        except Exception:
            pass
    transaction.on_commit(_enviar)


def _escribir_link_shopify_best_effort(pedido):
    """Metafield `torre.pedido_url` en la orden de Shopify: el link al pedido en
    el portal, para que servicio al cliente llegue desde el admin de Shopify
    (Chema 2026-09-23). Best-effort TOTAL en on_commit: un Shopify caído
    jamás tira la ingesta; el resultado queda en SyncLog.
    """
    def _escribir():
        try:
            from apps.integraciones.services import escribir_link_pedido  # lazy por contrato

            escribir_link_pedido(pedido)
        except Exception:
            pass
    transaction.on_commit(_escribir)


# ── Ingesta desde Shopify ──

@transaction.atomic
def ingerir_pedido_shopify(tienda, payload, origen="webhook"):
    """Upsert idempotente por (tienda, shopify_order_id).

    Orden nueva: crea Pedido + líneas, estampa corte vigente, calcula es_local
    por CP contra la zona local CDMX (bodega 01780) y reserva stock por línea vía
    inventario.reservar. Sin stock suficiente → el pedido queda PENDIENTE con
    incidencia_activa y se abre incidencia FAL (lazy).
    Orden repetida: refresca contacto; la dirección solo mientras no esté
    congelada (con guía comprada queda en direccion_pendiente); NO duplica ni
    re-reserva. Orden con cancelled_at → pasa por la matriz de cancelación.
    Filtros de entrada (solo órdenes NUEVAS; decisión 2026-08-19): se ingiere
    únicamente lo pagado (paid/partially_refunded) y sin fulfillment previo.
    Lo demás deja evento y NO se crea: una orden pendiente entra sola cuando
    se paga (el pago bumpea updated_at y regresa por sync/webhook).
    """
    shopify_order_id = str(payload.get("id") or "").strip()
    if not shopify_order_id:
        raise ValueError("El payload de Shopify no trae id de orden; no se puede ingerir.")
    cancelada = bool(payload.get("cancelled_at"))
    existente = (
        Pedido.objects.select_for_update()
        .filter(tienda=tienda, shopify_order_id=shopify_order_id)
        .first()
    )
    if existente is not None:
        return _actualizar_pedido_existente(existente, payload, origen, cancelada)
    estado_fulfillment = payload.get("fulfillment_status") or ""
    if estado_fulfillment:  # fulfilled/partial/restocked: atendida fuera de Torre
        registrar_evento(
            "pedido", shopify_order_id, "ingesta_omitida_fulfilled", actor=origen,
            cliente=tienda.cliente,
            delta={"origen": origen, "fulfillment_status": estado_fulfillment},
            motivo="Orden nueva pero ya atendida (total o parcialmente) fuera de Torre.",
        )
        return None
    financiero = payload.get("financial_status") or ""
    if financiero not in ("paid", "partially_refunded"):
        registrar_evento(
            "pedido", shopify_order_id, "ingesta_omitida_pago", actor=origen,
            cliente=tienda.cliente,
            delta={"origen": origen, "financial_status": financiero},
            motivo="Solo se ingiere lo pagado; al pagarse la orden entra sola.",
        )
        return None
    return _crear_pedido_nuevo(tienda, payload, origen, shopify_order_id, cancelada)


# Estados donde una edición ya no ajusta nada (refund sobre entregado = puro
# tema financiero; cancelados los gobierna su propia matriz).
_ESTADOS_EDICION_TERMINAL = (
    "ENTREGADO", "RETORNADO", "CANCELADO", "CANCELACION_PENDIENTE",
)


def _cantidades_objetivo(payload):
    """{codigo_sku: piezas} sumando current_quantity; None si ningún item trae la llave."""
    objetivo = {}
    con_llave = False
    for item in payload.get("line_items") or []:
        codigo = str(item.get("sku") or "").strip()
        if not codigo or "current_quantity" not in item:
            continue
        try:
            piezas = int(item.get("current_quantity") or 0)
        except (TypeError, ValueError):
            continue
        con_llave = True
        objetivo[codigo] = objetivo.get(codigo, 0) + max(piezas, 0)
    return objetivo if con_llave else None


def _reducir_lineas(pedido, filas, faltan, liberar_reserva):
    """Encoge/borra filas SIN avance físico hasta consumir `faltan`. Regresa lo no consumido."""
    quitadas_peso = 0
    for linea in filas:
        if faltan <= 0:
            break
        if linea.cantidad_pickeada:
            continue  # avance físico: intocable, va a conflicto
        quitar = min(linea.cantidad, faltan)
        if linea.reservada and not linea.sku.es_kit:
            liberar_reserva(linea.sku, quitar, pedido.folio)
        quitadas_peso += (linea.sku.peso_gr or 0) * quitar
        faltan -= quitar
        if quitar == linea.cantidad:
            linea.delete()
        else:
            linea.cantidad -= quitar
            linea.save(update_fields=["cantidad"])
    return faltan, quitadas_peso


def _aplicar_cambios_cantidades(pedido, payload, origen):
    """Refund parcial / edición de orden: ajusta las líneas al current_quantity.

    Reducción sin avance físico → encoge y libera reserva; con avance
    (pickeada, empacado o después) → incidencia CAN, cantidades intactas.
    Aumento o línea nueva en PENDIENTE → camino de ingesta (reserva o FAL);
    en estados posteriores → incidencia. Diff-based: repetir el mismo update
    es no-op.
    """
    if pedido.estado in _ESTADOS_EDICION_TERMINAL:
        return
    objetivo = _cantidades_objetivo(payload)
    if objetivo is None:
        return
    from apps.inventario.services import liberar_reserva  # lazy por contrato

    por_sku = {}
    # Las hijas de kit no entran al diff: las gobierna su kit, no la orden.
    for linea in pedido.lineas.select_related("sku").filter(parte_de_kit__isnull=True):
        por_sku.setdefault(linea.sku.codigo, []).append(linea)

    # Esperando inventario tras una salida parcial, la línea faltante (sin
    # avance físico) también se puede quitar: es la forma de cerrar el pedido
    # si el producto ya no va a llegar.
    editable = pedido.estado in (Pedido.PENDIENTE, Pedido.EN_PICKING) or pedido.esperando_inventario
    reducidas, aumentadas, conflictos, faltantes_stock = [], [], [], []
    delta_peso = 0
    for codigo, piezas_objetivo in objetivo.items():
        filas = por_sku.get(codigo, [])
        delta = piezas_objetivo - sum(l.cantidad for l in filas)
        if delta == 0:
            continue
        if delta < 0:
            faltan = -delta
            if editable:
                faltan, peso_quitado = _reducir_lineas(pedido, filas, faltan, liberar_reserva)
                delta_peso -= peso_quitado
                if faltan < -delta:
                    reducidas.append({"sku": codigo, "quitadas": (-delta) - faltan})
            if faltan > 0:
                conflictos.append(f"{codigo}: reducir {faltan} pieza(s) ya en proceso físico")
        elif pedido.estado == Pedido.PENDIENTE:
            delta_peso += _aumentar_linea(
                pedido, filas, codigo, delta, aumentadas, conflictos, faltantes_stock,
            )
        else:
            conflictos.append(f"{codigo}: la edición agrega {delta} pieza(s) con el pedido ya en proceso")

    if not (reducidas or aumentadas or conflictos):
        return
    if delta_peso:
        pedido.peso_esperado_gr = max((pedido.peso_esperado_gr or 0) + delta_peso, 0)
        pedido.save(update_fields=["peso_esperado_gr", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "edicion_orden", actor=origen, cliente=pedido.cliente,
        delta={"reducidas": reducidas, "aumentadas": aumentadas, "conflictos": conflictos},
        motivo="Refund parcial o edición de la orden en Shopify.",
    )
    _abrir_incidencia_edicion(pedido, faltantes_stock, conflictos)
    _cerrar_espera_si_nada_pendiente(pedido, origen)


def _cerrar_espera_si_nada_pendiente(pedido, actor):
    """Un pedido que esperaba inventario tras una salida parcial se cierra
    como RECOLECTADO cuando la edición de la orden quitó la línea faltante:
    ya no hay nada que surtir y el tracking de lo que salió toma el control."""
    if pedido.estado != Pedido.PARCIALMENTE_DESPACHADO or pedido.tiene_faltantes:
        return
    if pedido.paquetes.filter(estado="EMPACADO").exists() or pedido.lineas_por_surtir:
        return
    pedido.transicionar(
        Pedido.RECOLECTADO, actor=actor,
        motivo="La orden ya no pide lo que faltaba: todo lo pendiente salió.",
    )


def _aumentar_linea(pedido, filas, codigo, delta, aumentadas, conflictos, faltantes_stock):
    """Edición que agrega piezas en PENDIENTE: línea nueva por el delta (reserva o FAL)."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app
    sku = filas[0].sku if filas else SKU.objects.filter(cliente=pedido.cliente, codigo=codigo).first()
    if sku is None:
        conflictos.append(f"{codigo}: la edición agrega un SKU desconocido × {delta}")
        return 0
    if sku.es_kit:
        # Kit agregado por edición: misma semántica que en la ingesta.
        LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=delta, reservada=True)
        aumentadas.append({"sku": codigo, "agregadas": delta, "reservada": True})
        return (sku.peso_gr or 0) * delta
    linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=delta)
    reservada = _reservar_linea(linea)
    aumentadas.append({"sku": codigo, "agregadas": delta, "reservada": reservada})
    if not reservada:
        faltantes_stock.append(f"Sin stock suficiente: {codigo} × {delta}")
    return (sku.peso_gr or 0) * delta


def _abrir_incidencia_edicion(pedido, faltantes_stock, conflictos):
    """FAL si la edición agregó piezas sin stock; CAN si tocó piezas en proceso.
    Con una incidencia ya activa no se duplica (la pelota ya está en juego)."""
    if not (faltantes_stock or conflictos) or pedido.incidencia_activa or _incidencias_auto_pausadas(pedido.cliente):
        return
    pedido.incidencia_activa = True
    pedido.save(update_fields=["incidencia_activa", "actualizado"])
    try:
        from apps.incidencias.services import abrir_incidencia  # lazy
    except ImportError:
        return
    if faltantes_stock:
        abrir_incidencia(
            pedido.cliente, "FAL", "auto", pedido=pedido,
            texto="Faltante en edición de la orden: " + "; ".join(faltantes_stock),
        )
    else:
        abrir_incidencia(
            pedido.cliente, "CAN", "auto", pedido=pedido,
            texto="Edición de orden con el pedido en proceso: " + "; ".join(conflictos),
        )


_CLAVES_DIRECCION = ("address1", "address2", "city", "province", "province_code", "zip", "country_code")


def _misma_direccion(a, b):
    """Compara lo que decide a dónde viaja el paquete (calle, ciudad, estado,
    CP, país) sin espacios ni mayúsculas; nombre y teléfono van aparte."""
    def _forma(d):
        return tuple(str((d or {}).get(k) or "").strip().lower() for k in _CLAVES_DIRECCION)
    return _forma(a) == _forma(b)


def direccion_en_una_linea(direccion):
    """Dirección de Shopify (dict) en una línea, para Mesa y el portal."""
    d = direccion or {}
    partes = [d.get("address1"), d.get("address2"), d.get("city"), d.get("province") or d.get("province_code"), d.get("zip")]
    return ", ".join(str(p).strip() for p in partes if p and str(p).strip())


def _aplicar_direccion(pedido, direccion):
    """Pone la dirección en el pedido y recalcula CP y es_local; regresa los
    campos tocados (sin guardar)."""
    pedido.direccion = direccion
    campos = ["direccion"]
    cp = str(direccion.get("zip") or "").strip()
    if cp and cp != pedido.cp:
        pedido.cp = cp
        pedido.es_local = _es_local(cp)
        campos += ["cp", "es_local"]
    return campos


def _refrescar_direccion(pedido, direccion):
    """Dirección que llega en una ingesta repetida (Chema 2026-09-23). Sin
    guía se aplica; con la dirección congelada (guía comprada, algo en la
    calle, pedido cerrado) NO se pisa la dirección a la que viaja el paquete:
    la nueva queda en `direccion_pendiente` para que Mesa la vea y decida
    (regresar_a_empaque la aplica). Si Shopify vuelve a la dirección de la
    guía, la pendiente se limpia. Regresa los campos tocados (sin guardar)."""
    if _misma_direccion(pedido.direccion, direccion):
        if pedido.direccion_pendiente is not None:
            pedido.direccion_pendiente = None
            return ["direccion_pendiente"]
        return []
    if not pedido.direccion_congelada:
        campos = _aplicar_direccion(pedido, direccion)
        if pedido.direccion_pendiente is not None:
            pedido.direccion_pendiente = None
            campos.append("direccion_pendiente")
        return campos
    if _misma_direccion(pedido.direccion_pendiente, direccion):
        return []
    pedido.direccion_pendiente = direccion
    return ["direccion_pendiente"]


def _nombre_orden(payload):
    """El "name" de la orden de Shopify ("#4074"), recortado al campo."""
    return str(payload.get("name") or "").strip()[:40]


def _actualizar_pedido_existente(pedido, payload, origen, cancelada):
    """Rama idempotente del upsert: refresca datos blandos y aplica ediciones de cantidades."""
    campos = []
    direccion = payload.get("shipping_address")
    if direccion:
        campos += _refrescar_direccion(pedido, direccion)
    nombre, tel, email = _datos_comprador(payload)
    for campo, valor in [("comprador_nombre", nombre), ("comprador_tel", tel), ("comprador_email", email)]:
        if valor and getattr(pedido, campo) != valor:
            setattr(pedido, campo, valor)
            campos.append(campo)
    canal, canal_fuente = canal_desde_payload(payload)
    if (canal, canal_fuente) != (pedido.canal, pedido.canal_fuente) and (canal_fuente or canal != Pedido.CANAL_OTRO):
        pedido.canal, pedido.canal_fuente = canal, canal_fuente
        campos += ["canal", "canal_fuente"]
    nota = payload.get("note") or ""
    if nota and nota != pedido.nota_regalo:
        pedido.nota_regalo = nota
        campos.append("nota_regalo")
    nombre_orden = _nombre_orden(payload)
    if nombre_orden and nombre_orden != pedido.shopify_order_name:
        pedido.shopify_order_name = nombre_orden
        campos.append("shopify_order_name")
    if campos:
        pedido.save(update_fields=campos + ["actualizado"])
    if not cancelada:
        # Una orden cancelada la gobierna su matriz; lo demás ajusta cantidades.
        _aplicar_cambios_cantidades(pedido, payload, origen)
    registrar_evento(
        "pedido", pedido.pk, "ingesta_repetida", actor=origen, cliente=pedido.cliente,
        delta={"origen": origen, "campos": campos},
        motivo="Upsert idempotente: la orden ya existía, no se duplica ni se re-reserva.",
    )
    if cancelada and pedido.estado not in (
        Pedido.CANCELADO, Pedido.CANCELACION_PENDIENTE, Pedido.RETORNADO,
    ):
        try:
            cancelar(pedido, actor=origen, motivo="Orden cancelada en Shopify")
        except ValueError as exc:
            registrar_evento(
                "pedido", pedido.pk, "cancelacion_no_aplicable", actor=origen,
                cliente=pedido.cliente, motivo=str(exc),
            )
    return pedido


def _ticket_nuestro(tienda, shopify_order_id):
    """Cantidades por line_item de NUESTROS tickets (integraciones). None =
    sin datos para filtrar → se ingiere la orden completa (comportamiento de
    siempre). Un ShopifyError se propaga: el webhook queda para replay."""
    try:
        from apps.integraciones.services import lineas_fulfillment_nuestras  # lazy por contrato
    except ImportError:
        return None
    return lineas_fulfillment_nuestras(tienda, shopify_order_id)


def _cantidad_de_item(item, ticket):
    """Piezas a surtir de un item del payload.

    current_quantity = lo que QUEDA tras refunds/ediciones (quantity es lo
    original y nunca cambia; payloads sin la llave caen a quantity), acotado
    a lo que ampara NUESTRO ticket cuando la orden se dividió entre
    locations. None = la línea es de otro ticket.
    """
    try:
        cantidad = int(item.get("current_quantity", item.get("quantity")) or 0)
    except (TypeError, ValueError):
        cantidad = 0
    if ticket is not None:
        asignada = ticket["cantidades"].get(str(item.get("id") or ""))
        if asignada is None:
            return None
        cantidad = min(cantidad, asignada)  # split de línea: solo lo nuestro
    return cantidad


def _propiedades(item):
    """{name: value} de las properties del line item (Appstle escribe ahí)."""
    return {
        str(p.get("name") or ""): str(p.get("value") or "")
        for p in item.get("properties") or []
        if isinstance(p, dict)
    }


def _parsear_bb_variants(crudo):
    """'variantId:qty,variantId:qty' (__appstle-bb-variants) → [(id, piezas)]."""
    pares = []
    for parte in (crudo or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        variante, _, piezas = parte.partition(":")
        try:
            pares.append((variante.strip(), max(int(piezas), 1)))
        except ValueError:
            return []
    return pares


def _variantes_remotas(tienda, ids):
    """Resolución por API de integraciones; cualquier falla = degradar, jamás bloquear."""
    try:
        from apps.integraciones.services import resolver_variantes  # lazy por contrato
    except ImportError:
        return None
    try:
        return resolver_variantes(tienda, ids)
    except Exception:  # noqa: BLE001 — el kit cae al flujo de empaque, la orden entra igual
        return None


def _resolver_componentes_kit(pedido, props):
    """[(SKU, piezas)] desde las properties de Appstle, o None si algo no resuelve.

    Cadena por pieza: SKU posicional de _appstle-bb-product-sku → SKU de la
    variante por API → título del PRODUCTO contra descripcion (solo si es
    inequívoco). TODO-o-nada: una pieza sin resolver degrada el kit completo
    al flujo de empaque — jamás se adivina media caja.
    """
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    pares = _parsear_bb_variants(props.get("__appstle-bb-variants"))
    if not pares:
        return None
    posicionales = [s.strip() for s in (props.get("_appstle-bb-product-sku") or "").split(",")]
    remotas = None
    componentes = []
    for indice, (variant_id, piezas) in enumerate(pares):
        sku = None
        codigo = posicionales[indice] if indice < len(posicionales) else ""
        if codigo:
            sku = SKU.objects.filter(cliente=pedido.cliente, codigo=codigo, es_kit=False).first()
        if sku is None:
            if remotas is None:
                remotas = _variantes_remotas(pedido.tienda, [v for v, _ in pares]) or {}
            datos = remotas.get(variant_id) or {}
            codigo_remoto = (datos.get("sku") or "").strip()
            if codigo_remoto:
                sku = SKU.objects.filter(
                    cliente=pedido.cliente, codigo=codigo_remoto, es_kit=False,
                ).first()
            if sku is None:
                titulo = (datos.get("producto") or "").strip()
                if titulo:
                    candidatos = list(SKU.objects.filter(
                        cliente=pedido.cliente, descripcion__iexact=titulo, es_kit=False,
                    )[:2])
                    if len(candidatos) == 1:  # ambiguo (variantes) = no se adivina
                        sku = candidatos[0]
        if sku is None:
            return None
        componentes.append((sku, piezas))
    return componentes


def precio_de_item(item):
    """Precio unitario del line_item de Shopify como Decimal, o None si no viene
    o no se entiende."""
    crudo = item.get("price")
    if crudo in (None, ""):
        return None
    try:
        return Decimal(str(crudo)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _agregar_linea_kit(pedido, sku, cantidad, item, cancelada, faltantes):
    """Línea kit + (si Appstle trae la elección) sus hijas materializadas.

    Las hijas van por el riel NORMAL: reservan (FAL si falta stock, el
    reintento las recupera), el picker las escanea y el cotizador las pesa
    desde la ingesta. Sin datos resolubles → evento y el kit se declara en
    empaque leyendo nota_kit (la orden jamás se bloquea por el parser).
    """
    linea = LineaPedido.objects.create(
        pedido=pedido, sku=sku, cantidad=cantidad, reservada=True, precio_unitario=precio_de_item(item),
    )
    peso = (sku.peso_gr or 0) * cantidad
    props = _propiedades(item)
    if props.get("products"):
        linea.nota_kit = props["products"][:300]
        linea.save(update_fields=["nota_kit"])
    componentes = _resolver_componentes_kit(pedido, props) if props else None
    if componentes is None:
        if props.get("__appstle-bb-variants"):
            registrar_evento(
                "pedido", pedido.pk, "kit_sin_resolver", cliente=pedido.cliente,
                delta={"kit": sku.codigo, "eleccion": (props.get("products") or "")[:200]},
                motivo="Componentes del kit sin resolver: se declaran en empaque.",
            )
        return peso
    for comp_sku, piezas in componentes:
        hija = LineaPedido.objects.create(
            pedido=pedido, sku=comp_sku, cantidad=piezas, parte_de_kit=linea,
        )
        peso += (comp_sku.peso_gr or 0) * piezas
        if not cancelada and not _reservar_linea(hija):
            faltantes.append(f"Sin stock suficiente: {comp_sku.codigo} × {piezas}")
    registrar_evento(
        "pedido", pedido.pk, "kit_componentes_ingesta", cliente=pedido.cliente,
        delta={
            "kit": sku.codigo, "cantidad_kit": cantidad,
            "componentes": [{"sku": s.codigo, "cantidad": c} for s, c in componentes],
        },
        motivo="Componentes del arma-tu-teabox materializados desde la orden.",
    )
    return peso


def _agregar_linea_de_item(pedido, item, cantidad, cancelada, faltantes):
    """Crea la línea del item (si su SKU existe) y reserva. Regresa (peso_gr, valor)."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    codigo = str(item.get("sku") or "").strip()
    sku = SKU.objects.filter(cliente=pedido.cliente, codigo=codigo).first() if codigo else None
    if sku is None:
        faltantes.append(f"SKU desconocido: {codigo or item.get('title', '?')} × {cantidad}")
        return 0, Decimal("0")
    try:
        valor = Decimal(str(item.get("price") or "0")) * cantidad
    except InvalidOperation:
        valor = Decimal("0")
    if sku.es_kit:
        # Kit: se arma al empacar — nada que reservar ni FAL que abrir por SÍ
        # MISMO (sus componentes, si la orden los trae, sí reservan normal).
        peso = _agregar_linea_kit(pedido, sku, cantidad, item, cancelada, faltantes)
        return peso, valor
    linea = LineaPedido.objects.create(
        pedido=pedido, sku=sku, cantidad=cantidad, precio_unitario=precio_de_item(item),
    )
    # Orden que llega ya cancelada: no se aparta stock.
    if not cancelada and not _reservar_linea(linea):
        faltantes.append(f"Sin stock suficiente: {sku.codigo} × {cantidad}")
    return (sku.peso_gr or 0) * cantidad, valor


def canal_desde_payload(payload):
    """(canal, source_name crudo) de una orden de Shopify: una etiqueta de la orden
    en TORRE["CANAL_POR_TAG"] manda; si no, el source_name por prefijo en
    TORRE["CANAL_POR_SOURCE"]; sin coincidencia → "otro"."""
    torre = settings.TORRE
    fuente = str(payload.get("source_name") or "").strip()
    tags = payload.get("tags") or ""
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    for tag in tags:
        canal = torre["CANAL_POR_TAG"].get(str(tag).strip().lower())
        if canal:
            return canal, fuente
    clave = fuente.lower()
    if not clave:
        return Pedido.CANAL_WEB, fuente  # sin source_name (payloads viejos, tests): tienda en línea
    for prefijo, canal in torre["CANAL_POR_SOURCE"].items():
        if clave.startswith(prefijo):
            return canal, fuente
    return Pedido.CANAL_OTRO, fuente


def _crear_pedido_nuevo(tienda, payload, origen, shopify_order_id, cancelada):
    cliente = tienda.cliente

    # Multi-location (Stage 2): la orden puede dividirse entre bodegas — solo
    # se ingieren las líneas/cantidades de NUESTROS tickets de fulfillment.
    ticket = _ticket_nuestro(tienda, shopify_order_id)
    if ticket is not None and not ticket["cantidades"]:
        registrar_evento(
            "pedido", shopify_order_id, "ingesta_omitida_otra_location", actor=origen,
            cliente=cliente, delta={"origen": origen},
            motivo="Todos los tickets de fulfillment de la orden son de otra location.",
        )
        return None

    direccion = payload.get("shipping_address") or payload.get("billing_address") or {}
    cp = str(direccion.get("zip") or "").strip()
    nombre, tel, email = _datos_comprador(payload)
    try:
        valor_declarado = Decimal(str(payload.get("total_price") or "0"))
    except InvalidOperation:
        valor_declarado = Decimal("0")

    canal, canal_fuente = canal_desde_payload(payload)
    pedido = Pedido.objects.create(
        tienda=tienda,
        cliente=cliente,
        shopify_order_id=shopify_order_id,
        shopify_order_name=_nombre_orden(payload),
        origen=origen,
        canal=canal,
        canal_fuente=canal_fuente,
        comprador_nombre=nombre,
        comprador_tel=tel,
        comprador_email=email,
        direccion=direccion,
        cp=cp,
        es_local=_es_local(cp),
        parcial_de_orden=bool(ticket and ticket["parcial"]),
        valor_declarado=valor_declarado,
        nota_regalo=payload.get("note") or "",
        corte_vigente_al_ingreso=_corte_contractual(),
        estado=Pedido.PENDIENTE,
    )

    faltantes = []
    peso_esperado = 0
    valor_nuestro = Decimal("0")
    for item in payload.get("line_items") or []:
        cantidad = _cantidad_de_item(item, ticket)
        if not cantidad or cantidad <= 0:
            continue  # removida por refund/edición, o línea de otro ticket
        peso, valor = _agregar_linea_de_item(pedido, item, cantidad, cancelada, faltantes)
        peso_esperado += peso
        valor_nuestro += valor

    pedido.peso_esperado_gr = peso_esperado
    campos = ["peso_esperado_gr"]
    if pedido.parcial_de_orden:
        # El total_price ampara la orden completa; lo declarado es solo lo nuestro.
        pedido.valor_declarado = valor_nuestro
        campos.append("valor_declarado")
    if faltantes and not cancelada and not _incidencias_auto_pausadas(cliente):
        pedido.incidencia_activa = True
        campos.append("incidencia_activa")
    pedido.save(update_fields=campos + ["actualizado"])

    registrar_evento(
        "pedido", pedido.pk, "ingesta", actor=origen, cliente=cliente,
        delta={
            "shopify_order_id": shopify_order_id,
            "lineas": pedido.lineas.count(),
            "faltantes": faltantes,
            "es_local": pedido.es_local,
            "parcial": pedido.parcial_de_orden,
            "fos": ticket["fos"] if ticket else [],
        },
        motivo=f"Orden {payload.get('name') or shopify_order_id} ingerida vía {origen}.",
    )

    if faltantes and not cancelada:
        _abrir_incidencia_faltante(pedido, faltantes)

    if cancelada:
        cancelar(pedido, actor=origen, motivo="Orden cancelada en Shopify antes de operarse")
        return pedido

    # División de envío planificada desde la ingesta: pickers y packers ven
    # los paquetes desde el minuto uno.
    _planificar_best_effort(pedido)
    _enviar_confirmacion_best_effort(pedido)
    _avisar_piso_pedido_nuevo(pedido)
    _escribir_link_shopify_best_effort(pedido)
    return pedido


# ── Alta manual desde Mesa ──

@transaction.atomic
def crear_pedido_manual(cliente, *, comprador_nombre, comprador_tel="",
                        comprador_email="", direccion, cp, valor_declarado=None,
                        nota_regalo="", lineas, actor):
    """Alta manual de un pedido desde Mesa (cliente sin Shopify: mayoreo, B2B).

    `lineas` = lista de tuplas (sku, cantidad); `direccion` = dict con el mismo
    shape que el shipping_address de Shopify (zip, address1, city, ...).
    Mismo contrato operativo que la ingesta: reserva por línea (sin stock →
    línea sin reservar + incidencia FAL + incidencia_activa), es_local por CP,
    corte vigente estampado, plan de paquetes best-effort y plantilla A de
    confirmación (mensajeria la omite si no hay teléfono).
    valor_declarado vacío = Σ precio_declarado × cantidad del catálogo.
    """
    comprador_nombre = str(comprador_nombre or "").strip()
    if not comprador_nombre:
        raise ValueError("Captura el nombre del comprador para crear el pedido.")
    if not lineas:
        raise ValueError("El pedido necesita al menos un renglón de producto con sus piezas.")
    lineas_limpias = []
    for sku, cantidad in lineas:
        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            raise ValueError(
                f"La cantidad de {sku.codigo} debe ser un número entero mayor a cero."
            )
        if cantidad <= 0:
            raise ValueError(
                f"La cantidad de {sku.codigo} debe ser mayor a cero; revisa el renglón."
            )
        if sku.cliente_id != cliente.pk:
            raise ValueError(
                f"El SKU {sku.codigo} no es del catálogo de {cliente.nombre}; revisa el renglón."
            )
        lineas_limpias.append((sku, cantidad))

    if valor_declarado in (None, ""):
        valor_declarado = sum(
            ((sku.precio_declarado or Decimal("0")) * cantidad
             for sku, cantidad in lineas_limpias),
            Decimal("0"),
        )
    else:
        try:
            valor_declarado = Decimal(str(valor_declarado))
        except InvalidOperation:
            raise ValueError("El valor declarado debe ser un monto en MXN (ej. 1500.00).")
        if valor_declarado < 0:
            raise ValueError("El valor declarado no puede ser negativo.")

    cp = str(cp or "").strip()
    pedido = Pedido.objects.create(
        tienda=None,
        cliente=cliente,
        shopify_order_id="",
        origen="manual",
        canal=Pedido.CANAL_MANUAL,
        comprador_nombre=comprador_nombre[:120],
        comprador_tel=str(comprador_tel or "").strip()[:20],
        comprador_email=str(comprador_email or "").strip(),
        direccion=direccion or {},
        cp=cp,
        es_local=_es_local(cp),
        valor_declarado=valor_declarado,
        nota_regalo=nota_regalo or "",
        corte_vigente_al_ingreso=_corte_contractual(),
        estado=Pedido.PENDIENTE,
    )

    faltantes = []
    peso_esperado = 0
    for sku, cantidad in lineas_limpias:
        if sku.es_kit:
            # Kit: espejo de la ingesta — se arma al empacar. La línea nace
            # reservada SIN tocar inventario (SKU virtual, sin stock propio);
            # pedirle stock abría FALes falsas (PED-00002).
            LineaPedido.objects.create(
                pedido=pedido, sku=sku, cantidad=cantidad, reservada=True,
            )
            peso_esperado += (sku.peso_gr or 0) * cantidad
            continue
        linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=cantidad)
        peso_esperado += (sku.peso_gr or 0) * cantidad
        if not _reservar_linea(linea):
            faltantes.append(f"Sin stock suficiente: {sku.codigo} × {cantidad}")

    pedido.peso_esperado_gr = peso_esperado
    campos = ["peso_esperado_gr"]
    if faltantes and not _incidencias_auto_pausadas(cliente):
        pedido.incidencia_activa = True
        campos.append("incidencia_activa")
    pedido.save(update_fields=campos + ["actualizado"])

    registrar_evento(
        "pedido", pedido.folio, "alta_manual", actor=actor, cliente=cliente,
        delta={
            "lineas": [
                {"sku": sku.codigo, "cantidad": cantidad}
                for sku, cantidad in lineas_limpias
            ],
            "faltantes": faltantes,
            "es_local": pedido.es_local,
        },
        motivo="Pedido manual capturado desde Mesa de Control.",
    )

    if faltantes:
        _abrir_incidencia_faltante(pedido, faltantes)

    _planificar_best_effort(pedido)
    _enviar_confirmacion_best_effort(pedido)
    _avisar_piso_pedido_nuevo(pedido)
    return pedido


# ── Picking ──

def iniciar_picking(pedido, actor):
    """PENDIENTE → EN_PICKING. La ola del piso empieza aquí.

    GATE de reservas (sep-2026): la reserva ES la garantía de existencias, y
    pickear sin ella produce las inconsistencias bandera-vs-kardex que se
    arreglaban por shell (PED-00002, PED-00009). Fulfillment parcial (Chema
    2026-09-22): una línea sin reservar ya no detiene la ola — se surte lo
    que sí tiene reserva y la faltante espera con el tag "Sin inventario"
    (confirmar_linea_pick la rechaza). Sin NADA que surtir no hay ola que
    empezar. El reintento de reservas vive en Mesa y en la entrada de stock.
    """
    if not pedido.lineas_por_surtir:
        faltantes = sorted({l.sku.codigo for l in pedido.lineas_faltantes})
        detalle = f": todas sus líneas están sin inventario ({', '.join(faltantes)})" if faltantes else ""
        raise ValueError(
            f"{pedido.folio} no tiene nada que surtir{detalle}. "
            "Pide a Mesa reintentar las reservas."
        )
    if hasattr(actor, "pk"):
        # El que lo inicia se vuelve DUEÑO: el pedido desaparece para los
        # demás operadores hasta la última foto de cierre (o transferencia).
        pedido.asignado_a = actor
        pedido.save(update_fields=["asignado_a", "actualizado"])
    pedido.transicionar(Pedido.EN_PICKING, actor=actor, motivo="Inicio de picking")
    return pedido


def _nombre_usuario(u):
    return getattr(u, "username", str(u) or "?")


def transferir_pedido(pedido, de, a):
    """El DUEÑO envía su pedido a otro operador; queda pendiente de ACEPTAR."""
    if pedido.asignado_a_id != getattr(de, "pk", None):
        raise ValueError(f"{pedido.folio} no es tuyo: solo su dueño puede enviarlo.")
    if getattr(a, "pk", None) == de.pk:
        raise ValueError("Elegiste tu propio usuario: no hay nada que enviar.")
    if pedido.cajas_cerradas_completas or pedido.estado not in (
        Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA,
    ):
        raise ValueError(
            f"{pedido.folio} ya está liberado (cierre completo o fuera del carril): "
            "no hay dueño que transferir."
        )
    pedido.transferencia_a = a
    pedido.save(update_fields=["transferencia_a", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "transferencia_enviada", actor=de, cliente=pedido.cliente,
        delta={"de": _nombre_usuario(de), "a": _nombre_usuario(a)},
        motivo=f"{_nombre_usuario(de)} envió el pedido a {_nombre_usuario(a)}; falta que acepte.",
    )
    return pedido


def soltar_pedido(pedido, usuario, motivo=""):
    """El dueño suelta su pedido en picking (faltante, cambio de prioridad):
    se queda EN_PICKING con el avance intacto y sin dueño, así lo puede tomar
    cualquier operador desde la lista o seguirlo él mismo después (Chema
    2026-09-21: con un faltante el operador quedaba trabado). Mesa también
    puede soltarlo. Cancela una transferencia pendiente si la había. Si lo
    retoma OTRA persona, el avance se reinicia al reclamarlo
    (reiniciar_picking): nadie da fe del carrito de otro."""
    es_mesa = getattr(getattr(usuario, "perfil", None), "rol", "") == "mesa" or getattr(usuario, "is_superuser", False)
    if pedido.asignado_a_id != getattr(usuario, "pk", None) and not es_mesa:
        raise ValueError(f"{pedido.folio} no es tuyo: solo su dueño (o Mesa) puede soltarlo.")
    if pedido.estado != Pedido.EN_PICKING:
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display()}: solo se suelta un pedido en picking."
        )
    if pedido.asignado_a_id is None:
        return pedido
    anterior = pedido.asignado_a
    pedido.asignado_a = None
    pedido.transferencia_a = None
    pedido.save(update_fields=["asignado_a", "transferencia_a", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "pedido_soltado", actor=usuario, cliente=pedido.cliente,
        delta={"de": _nombre_usuario(anterior), "motivo": motivo},
        motivo=(motivo or f"{_nombre_usuario(usuario)} soltó el pedido; lo puede tomar cualquier operador.")[:300],
    )
    return pedido


def aceptar_transferencia(pedido, usuario):
    """El destinatario acepta: cambia de manos y, si sigue en picking, el
    avance se reinicia (Chema 2026-09-22: quien recibe re-escanea desde el
    carrito; nadie da fe de lo que otro metió)."""
    if pedido.transferencia_a_id != usuario.pk:
        raise ValueError(f"{pedido.folio} no tiene una transferencia para ti.")
    anterior = pedido.asignado_a
    pedido.asignado_a = usuario
    pedido.transferencia_a = None
    pedido.save(update_fields=["asignado_a", "transferencia_a", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "pedido_transferido", actor=usuario, cliente=pedido.cliente,
        delta={"de": _nombre_usuario(anterior), "a": usuario.username},
        motivo=f"Transferencia aceptada: ahora lo trabaja {usuario.username}.",
    )
    reiniciar_picking(
        pedido, usuario,
        motivo=f"Transferencia aceptada de {_nombre_usuario(anterior)}: re-escanea desde el carrito.",
    )
    return pedido


def reiniciar_picking(pedido, actor, motivo=""):
    """El picking vuelve a cero cuando el pedido cambia de manos (transferencia
    aceptada o un pedido libre que toma OTRA persona): quien recibe el carrito
    no puede dar fe de lo que otro metió y lo re-escanea todo. Solo aplica en
    picking y antes de que alguna caja esté empacada (una caja ya pesada y
    con foto no se deshace); las hijas de kit declaradas en empaque no se
    tocan (reservan stock al declararse). No mueve inventario: el pick se
    confirma hasta empacar. Regresa True si reinició algo; queda el evento
    picking_reiniciado con el avance que había."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    if pedido.estado != Pedido.EN_PICKING:
        return False
    if pedido.paquetes.filter(estado__in=[Paquete.EMPACADO, Paquete.DESPACHADO]).exists():
        return False
    with transaction.atomic():
        lineas = list(
            pedido.lineas.select_for_update().select_related("sku")
            .filter(parte_de_kit__isnull=True, cantidad_pickeada__gt=0)
        )
        if not lineas:
            return False
        avance = [{"sku": l.sku.codigo, "pickeada": l.cantidad_pickeada} for l in lineas]
        for linea in lineas:
            linea.cantidad_pickeada = 0
            linea.save(update_fields=["cantidad_pickeada"])
        registrar_evento(
            "pedido", pedido.pk, "picking_reiniciado", actor=actor, cliente=pedido.cliente,
            delta={"avance_anterior": avance},
            motivo=(motivo or "El pedido cambió de manos: se re-escanea desde el carrito.")[:300],
        )
    return True


def rechazar_transferencia(pedido, usuario):
    """El destinatario la rechaza — o el dueño cancela su propio envío."""
    if usuario.pk not in (pedido.transferencia_a_id, pedido.asignado_a_id):
        raise ValueError(f"{pedido.folio}: esa transferencia no es tuya.")
    destinatario = pedido.transferencia_a
    pedido.transferencia_a = None
    pedido.save(update_fields=["transferencia_a", "actualizado"])
    accion = "cancelada por el dueño" if usuario.pk == pedido.asignado_a_id else "rechazada"
    registrar_evento(
        "pedido", pedido.pk, "transferencia_rechazada", actor=usuario, cliente=pedido.cliente,
        delta={"destinatario": _nombre_usuario(destinatario)},
        motivo=f"Transferencia {accion}.",
    )
    return pedido


def confirmar_linea_pick(linea, cantidad, actor, codigo_escaneado=None):
    """Confirma unidades pickeadas de una línea, por escaneo.

    Si viene codigo_escaneado, valida contra el código de barras (o código)
    del SKU de la línea: escaneo equivocado → ValueError.
    Atómico con select_for_update de la línea: dos escaneos concurrentes se
    serializan y el segundo valida sobre el avance FRESCO (nada se pierde ni
    se pasa del pedido).
    """
    with transaction.atomic():
        fresca = (
            LineaPedido.objects.select_for_update()
            .select_related("sku", "pedido", "pedido__cliente")
            .get(pk=linea.pk)
        )
        pedido = fresca.pedido
        if pedido.estado != Pedido.EN_PICKING:
            raise ValueError(
                f"El pedido {pedido.folio} no está en picking (está {pedido.get_estado_display()}). "
                "Pídele a Mesa que lo inicie antes de escanear."
            )
        if fresca.faltante:
            raise ValueError(
                f"{fresca.sku.codigo} está SIN INVENTARIO en este pedido: no se surte en esta "
                "ola. Regresa la pieza al anaquel y avisa a Mesa si sí había existencia."
            )
        if codigo_escaneado is not None:
            codigo = str(codigo_escaneado).strip()
            validos = {c for c in (fresca.sku.codigo_barras, fresca.sku.codigo) if c}
            if codigo not in validos:
                raise ValueError(
                    f"Código equivocado: escaneaste {codigo} y esta línea pide "
                    f"{fresca.sku.codigo} ({fresca.sku.descripcion}). Escanea el producto correcto."
                )
        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            raise ValueError("La cantidad debe ser un número entero mayor a cero.")
        if cantidad <= 0:
            raise ValueError("La cantidad debe ser mayor a cero.")
        if fresca.cantidad_pickeada + cantidad > fresca.cantidad:
            raise ValueError(
                f"Te pasas: la línea pide {fresca.cantidad} y ya llevas {fresca.cantidad_pickeada}. "
                "Revisa la cantidad antes de confirmar."
            )
        fresca.cantidad_pickeada += cantidad
        fresca.save(update_fields=["cantidad_pickeada"])
        registrar_evento(
            "linea_pedido", fresca.pk, "pick", actor=actor, cliente=pedido.cliente,
            delta={"sku": fresca.sku.codigo, "cantidad": cantidad, "pickeada": fresca.cantidad_pickeada},
            motivo=f"Pick de {pedido.folio}",
        )
    # La instancia del caller refleja el avance real (las vistas la re-pintan).
    linea.cantidad_pickeada = fresca.cantidad_pickeada
    return fresca


# ── Kits (contenido declarado en empaque — mystery box) ──

def declarar_contenido_kit(linea_kit, items, actor, caja=None):
    """El packer declara los tés del kit en empaque (registro por pedido).

    items = [(SKU, piezas)]. Atómico y completo-o-nada: cada componente
    reserva de vendible; sin stock de alguno → ValueError y nada se toca.
    Las hijas nacen pickeadas (el packer las tiene en la mano) y viajan por
    los rieles normales: empacar las confirma, salida las despacha,
    cancelación/restock las trata como líneas comunes.
    """
    if not linea_kit.sku.es_kit:
        raise ValueError(f"{linea_kit.sku.codigo} no es un kit.")
    pedido = linea_kit.pedido
    if pedido.estado != Pedido.EN_PICKING:
        raise ValueError(
            f"El contenido del kit se declara durante el empaque; {pedido.folio} "
            f"está {pedido.get_estado_display()}."
        )
    if caja is not None:
        if not (1 <= int(caja) <= linea_kit.cantidad):
            raise ValueError(
                f"Caja {caja} fuera de rango: el pedido lleva {linea_kit.cantidad} "
                "caja(s) de este kit."
            )
        if linea_kit.componentes.filter(kit_caja=caja).exists():
            raise ValueError(
                f"La caja {caja} del kit ya tiene contenido; quítalo antes de re-declararla."
            )
    elif linea_kit.componentes.exists():
        raise ValueError(
            f"El kit de {pedido.folio} ya tiene contenido; quítalo antes de re-declararlo."
        )
    consolidadas = {}
    for sku, piezas in items:
        if sku.es_kit:
            raise ValueError("Un kit no puede llevar otro kit dentro.")
        consolidadas[sku] = consolidadas.get(sku, 0) + int(piezas)
    if not consolidadas:
        raise ValueError("Declara al menos un producto dentro del kit.")
    # Cupo exacto (productos_por_kit > 0): la caja lleva N productos, ni más
    # ni menos. Declaración por caja valida N; declaración completa (sin
    # caja) valida N × cantidad de cajas. Cupo 0 = libre (compat).
    cupo = linea_kit.sku.productos_por_kit or 0
    if cupo:
        total = sum(consolidadas.values())
        objetivo = cupo if caja is not None else cupo * linea_kit.cantidad
        donde = f"la caja {caja}" if caja is not None else "el kit completo"
        if total != objetivo:
            raise ValueError(
                f"{linea_kit.sku.codigo} lleva {objetivo} producto(s) en {donde} "
                f"y declaraste {total}. Ajusta los renglones al cupo exacto."
            )

    from apps.inventario.services import reservar  # lazy por contrato
    with transaction.atomic():
        for sku, piezas in consolidadas.items():
            if not reservar(sku, piezas, pedido.folio):
                raise ValueError(
                    f"Sin stock de {sku.codigo} para el kit; no se apartó nada — "
                    "elige otro producto o avisa a Mesa."
                )
            LineaPedido.objects.create(
                pedido=pedido, sku=sku, cantidad=piezas,
                cantidad_pickeada=piezas, reservada=True, parte_de_kit=linea_kit,
                kit_caja=caja,
            )
        pedido.peso_esperado_gr = (pedido.peso_esperado_gr or 0) + sum(
            (s.peso_gr or 0) * c for s, c in consolidadas.items()
        )
        pedido.save(update_fields=["peso_esperado_gr", "actualizado"])
        registrar_evento(
            "pedido", pedido.pk, "kit_contenido", actor=actor, cliente=pedido.cliente,
            delta={
                "kit": linea_kit.sku.codigo,
                "caja": caja,
                "componentes": [
                    {"sku": s.codigo, "cantidad": c} for s, c in consolidadas.items()
                ],
            },
            motivo="Contenido del kit declarado en empaque (registro por pedido).",
        )
    return consolidadas


def quitar_contenido_kit(linea_kit, actor):
    """Deshace la declaración (antes de empacar): libera reservas y borra hijas.

    Solo estaban reservadas — la liberación además dispara el reintento de
    reservas para quien esperara ese té.
    """
    pedido = linea_kit.pedido
    if pedido.estado != Pedido.EN_PICKING:
        raise ValueError(
            f"El contenido solo se cambia durante el empaque; {pedido.folio} "
            f"está {pedido.get_estado_display()}."
        )
    hijas = list(linea_kit.componentes.select_related("sku"))
    if not hijas:
        return
    from apps.inventario.services import liberar_reserva  # lazy por contrato
    with transaction.atomic():
        peso = 0
        for hija in hijas:
            liberar_reserva(hija.sku, hija.cantidad, pedido.folio)
            peso += (hija.sku.peso_gr or 0) * hija.cantidad
            hija.delete()
        pedido.peso_esperado_gr = max((pedido.peso_esperado_gr or 0) - peso, 0)
        pedido.save(update_fields=["peso_esperado_gr", "actualizado"])
        registrar_evento(
            "pedido", pedido.pk, "kit_contenido_quitado", actor=actor, cliente=pedido.cliente,
            delta={"kit": linea_kit.sku.codigo, "componentes": len(hijas)},
        )


# ── Empaque ──

def _verificar_peso(pedido, peso_real, esperado, caja=None):
    """Báscula vs esperado según settings.TORRE_PESO_MODO: bloquear|avisar|off.

    off (default): se omite — el esperado suma solo productos, sin la tara de
    la caja, y bloquear reventaría empaques legítimos. avisar: registra el
    evento peso_discrepante (visible a Mesa) y el empaque continúa. bloquear:
    ValueError con tolerancia ±TORRE["TOLERANCIA_PESO_PCT"] — la conducta
    contractual, que regresa por .env cuando el catálogo de cajas aporte tara
    y el esperado sea confiable.
    """
    modo = getattr(settings, "TORRE_PESO_MODO", "off")
    if modo == "off" or esperado <= 0:
        return
    tolerancia_pct = float(settings.TORRE["TOLERANCIA_PESO_PCT"])
    diferencia_pct = abs(peso_real - esperado) * 100.0 / esperado
    if diferencia_pct <= tolerancia_pct:
        return
    if modo == "avisar":
        donde = f"la caja {caja}" if caja is not None else "el pedido"
        registrar_evento(
            "pedido", pedido.pk, "peso_discrepante", cliente=pedido.cliente,
            delta={"caja": caja, "esperado_gr": esperado, "bascula_gr": peso_real,
                   "diferencia_pct": round(diferencia_pct, 1)},
            motivo=(f"Peso discrepante en {donde}: esperado {esperado} g, báscula "
                    f"{peso_real} g ({diferencia_pct:.1f}%; tolerancia "
                    f"±{tolerancia_pct:g}%). Modo avisar: el empaque continúa."),
        )
        return
    if caja is not None:
        raise ValueError(
            f"El peso de la caja {caja} no cuadra: el plan marca {esperado} g "
            f"y la báscula {peso_real} g ({diferencia_pct:.1f}% de diferencia; "
            f"la tolerancia es ±{tolerancia_pct:g}%). "
            "Revisa el contenido de ESA caja antes de cerrarla."
        )
    raise ValueError(
        f"El peso no cuadra: se esperaban {esperado} g y la báscula marca {peso_real} g "
        f"({diferencia_pct:.1f}% de diferencia; la tolerancia es ±{tolerancia_pct:g}%). "
        "Revisa el contenido antes de cerrar la caja."
    )


def empacar(pedido, actor, peso_real_gr, fotos, peso_ya_verificado=False):
    """EN_PICKING → EMPACADO con verificación por báscula y evidencia.

    Valida ANTES de transicionar: líneas completas, tolerancia de peso
    ±settings.TORRE["TOLERANCIA_PESO_PCT"] y ≥1 foto tipo "contenido" ligada
    al pedido. La foto de la caja cerrada YA NO se pide aquí: la guía todavía
    no existe — se exige después, en cerrar_caja(), con la etiqueta pegada.
    Luego confirma los picks en inventario y transiciona.
    `fotos`: lista de archivos subidos o instancias de EvidenciaFoto.

    `peso_ya_verificado=True` (encadenado de empacar_caja): cada caja ya pasó
    la báscula contra SU plan — y el plan (paquete.peso_kg) trae el margen de
    empaque del cotizador, mientras peso_esperado_gr es NETO. Revalidar la
    suma contra el neto atoraría pedidos fieles al plan, así que aquí se omite.

    Atómico con select_for_update del pedido: dos POSTs concurrentes se
    serializan y el segundo valida sobre el estado FRESCO (jamás se confirma
    el pick dos veces).
    """
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        if fresco.estado != Pedido.EN_PICKING:
            raise ValueError(
                f"El pedido {fresco.folio} no se puede empacar: está {fresco.get_estado_display()} "
                "y el empaque solo aplica a pedidos en picking."
            )
        # Solo cuenta lo que ESTA ola surte: las faltantes (sin inventario)
        # y lo que ya salió con una ola anterior no detienen el empaque.
        incompletas = [l for l in fresco.lineas_por_surtir if l.cantidad_pickeada < l.cantidad]
        if incompletas:
            detalle = ", ".join(
                f"{l.sku.codigo} ({l.cantidad_pickeada}/{l.cantidad})" for l in incompletas
            )
            raise ValueError(
                f"Faltan unidades por pickear: {detalle}. Escanea todo antes de empacar."
            )
        # GATE de kits: la caja no se cierra sin su contenido declarado (7B lo
        # pre-declara desde la orden; el mystery se declara aquí en empaque).
        # GATE de kits con cupo: cada kit debe tener su contenido COMPLETO —
        # cupo × cajas piezas exactas si productos_por_kit > 0; al menos una
        # pieza si el cupo es libre (0).
        kits_incompletos = []
        for l in fresco.lineas.select_related("sku").filter(
            sku__es_kit=True, parte_de_kit__isnull=True,
        ):
            if l.pendiente <= 0:
                continue  # kit faltante (espera inventario) o ya despachado en la ola anterior
            piezas = sum(h.cantidad for h in l.componentes.all())
            cupo = l.sku.productos_por_kit or 0
            objetivo = cupo * l.cantidad
            if cupo and piezas != objetivo:
                kits_incompletos.append(f"{l.sku.codigo} ({piezas}/{objetivo})")
            elif not cupo and piezas == 0:
                kits_incompletos.append(l.sku.codigo)
        if kits_incompletos:
            raise ValueError(
                "Declara el contenido del kit antes de empacar: "
                + ", ".join(kits_incompletos)
            )

        try:
            peso_real = int(peso_real_gr)
        except (TypeError, ValueError):
            raise ValueError("Captura el peso de la báscula en gramos antes de empacar.")
        if peso_real <= 0:
            raise ValueError("Captura el peso de la báscula en gramos antes de empacar.")
        # Verificación de báscula según TORRE_PESO_MODO (ver _verificar_peso).
        if not peso_ya_verificado:
            _verificar_peso(fresco, peso_real, fresco.peso_esperado_gr or 0)

        # Persistir las fotos que llegan (default: contenido) y validar la
        # evidencia de contenido ligada al pedido.
        entidad_id = str(fresco.pk)
        for foto in fotos or []:
            if isinstance(foto, EvidenciaFoto):
                foto.entidad = "pedido"
                foto.entidad_id = entidad_id
                if not foto.tipo:
                    foto.tipo = TIPO_FOTO_CONTENIDO
                if not foto.tomada_por:
                    foto.tomada_por = _actor_nombre(actor)
                foto.save()
            else:
                EvidenciaFoto.objects.create(
                    entidad="pedido", entidad_id=entidad_id, tipo=TIPO_FOTO_CONTENIDO,
                    archivo=foto, tomada_por=_actor_nombre(actor),
                )
        fotos_contenido = EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=entidad_id, tipo=TIPO_FOTO_CONTENIDO,
        ).count()
        if fotos_contenido < FOTOS_CONTENIDO_MINIMAS:
            raise ValueError(
                "Falta la foto del contenido: toma al menos una foto del contenido "
                "de la caja antes de empacar. La foto de la caja cerrada se toma "
                "al final, con la etiqueta ya pegada."
            )

        from apps.inventario.services import confirmar_pick  # lazy
        for linea in fresco.lineas.select_related("sku"):
            # La línea kit se escanea (la caja física) pero no tiene stock propio.
            # Segunda ola: lo que salió con la primera (cantidad_despachada) se
            # confirmó entonces; solo se confirma lo nuevo.
            por_confirmar = linea.cantidad_pickeada - linea.cantidad_despachada
            if por_confirmar > 0 and not linea.sku.es_kit:
                confirmar_pick(linea.sku, por_confirmar, fresco.folio)

        fresco.peso_real_gr = peso_real
        fresco.save(update_fields=["peso_real_gr", "actualizado"])
        fresco.transicionar(
            Pedido.EMPACADO, actor=actor,
            motivo=f"Empaque verificado: báscula {peso_real} g y {fotos_contenido} foto(s) de contenido.",
        )
    # La instancia del caller refleja el estado real (las vistas deciden con ella).
    if fresco is not pedido:
        pedido.estado = fresco.estado
        pedido.peso_real_gr = fresco.peso_real_gr
        pedido.ts_empacado = fresco.ts_empacado
    return fresco


def _peso_esperado_caja(paquete, tara_gr=None):
    """Gramos esperados de UNA caja para la báscula.

    Esperado HONESTO cuando hay desglose: productos de ESTA caja + tara. La
    tara manual (bulto especial / caja propia) manda; si no, la de la caja
    elegida del catálogo; sin ninguna, 0 y el esperado es solo neto. Sin
    desglose o con fracciones (medias cajas) cae al plan (peso_kg, que trae
    el margen de empaque como proxy).
    """
    lineas_caja = list(paquete.lineas.select_related("linea_pedido__sku"))
    neto = 0
    if lineas_caja and not any(pl.fraccion_de > 1 for pl in lineas_caja):
        neto = sum(
            (pl.linea_pedido.sku.peso_gr or 0) * pl.cantidad for pl in lineas_caja
        )
    if tara_gr is not None:
        tara = tara_gr
    else:
        tara = paquete.caja.peso_gr if paquete.caja_id else 0
    if neto:
        return neto + tara
    return int(paquete.peso_kg * 1000) if paquete.peso_kg else 0


@transaction.atomic
def empacar_caja(paquete, actor, peso_real_gr, foto_contenido, caja=None, dims=None, tara_gr=None):
    """Empaque POR CAJA (wizard del carril único): peso contra SU plan + foto contenido.

    Valida el peso de la báscula contra el plan de la caja
    (paquete.peso_kg ± settings.TORRE["TOLERANCIA_PESO_PCT"], mismo criterio
    que empacar), guarda Paquete.peso_real_gr, adjunta la foto de contenido
    al pedido y la estampa en Paquete.foto_contenido (el evento lleva el
    número de caja) y transiciona el paquete
    PLANEADO/EN_EMPAQUE → EMPACADO. Cuando TODAS las cajas del pedido quedan
    EMPACADO, encadena empacar() con peso = Σ pesos reales por caja (las
    fotos de contenido ya están ligadas al pedido). Cualquier ValueError
    revienta la transacción completa: caja fuera de peso → no se toca nada.
    El pedido legacy sin paquetes sigue usando empacar() directo (1 caja
    implícita).

    Candado de carrera: paquete y pedido se re-leen con select_for_update
    dentro del atomic — dos POSTs concurrentes de la MISMA caja se serializan
    y el segundo valida sobre el estado FRESCO (ya EMPACADO → ValueError, sin
    doble confirmar_pick ni kardex doblado).
    """
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    fresco = Paquete.objects.select_for_update().get(pk=paquete.pk)
    pedido = Pedido.objects.select_for_update().get(pk=fresco.pedido_id)
    if fresco.estado not in (Paquete.PLANEADO, Paquete.EN_EMPAQUE):
        raise ValueError(
            f"La caja {fresco.numero} de {pedido.folio} ya está "
            f"{fresco.get_estado_display().lower()}; no se empaca dos veces."
        )
    if pedido.estado not in (Pedido.EN_PICKING, Pedido.EMPACADO):
        # EMPACADO entra solo para el REEMPAQUE por caja (Chema 2026-09-24):
        # el pedido se empacó entero sin plan y Mesa replaneó las cajas.
        raise ValueError(
            f"El pedido {pedido.folio} no se puede empacar: está {pedido.get_estado_display()} "
            "y el empaque solo aplica a pedidos en picking."
        )
    incompletas = [l for l in pedido.lineas_por_surtir if l.cantidad_pickeada < l.cantidad]
    if incompletas:
        detalle = ", ".join(
            f"{l.sku.codigo} ({l.cantidad_pickeada}/{l.cantidad})" for l in incompletas
        )
        raise ValueError(
            f"Faltan unidades por pickear: {detalle}. Escanea todo antes de empacar."
        )

    try:
        peso_real = int(peso_real_gr)
    except (TypeError, ValueError):
        peso_real = 0
    if peso_real <= 0:
        raise ValueError(
            f"Captura el peso de la báscula de la caja {fresco.numero}, en gramos."
        )
    if caja is not None:
        fresco.caja = caja
    if dims:
        fresco.largo_cm, fresco.ancho_cm, fresco.alto_cm = dims
    esperado = _peso_esperado_caja(fresco, tara_gr)
    _verificar_peso(pedido, peso_real, esperado, caja=fresco.numero)
    if foto_contenido is None:
        raise ValueError(
            f"Toma la foto del contenido de la caja {fresco.numero} antes de confirmarla."
        )

    evidencia = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo=TIPO_FOTO_CONTENIDO,
        archivo=foto_contenido, tomada_por=_actor_nombre(actor),
    )
    fresco.peso_real_gr = peso_real
    fresco.foto_contenido = evidencia
    fresco.save(update_fields=[
        "peso_real_gr", "foto_contenido", "caja", "largo_cm", "ancho_cm", "alto_cm",
    ])
    fresco.transicionar(
        Paquete.EMPACADO, actor=actor,
        motivo=f"Caja {fresco.numero}: báscula {peso_real} g contra plan de {esperado} g.",
    )
    registrar_evento(
        "pedido", pedido.pk, "caja_empacada", actor=actor, cliente=pedido.cliente,
        delta={"caja": fresco.numero, "peso_real_gr": peso_real,
               "peso_plan_gr": esperado, "evidencia_id": evidencia.pk},
        motivo=f"Empaque por caja de {pedido.folio} (caja {fresco.numero}).",
    )

    cajas = list(pedido.paquetes.all())
    if all(c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO) for c in cajas):
        if pedido.estado == Pedido.EMPACADO:
            # Reempaque por caja: el pedido ya confirmó su pick y ya es
            # EMPACADO; solo se actualiza el peso real total (sin segundo
            # confirmar_pick ni transición).
            pedido.peso_real_gr = sum(c.peso_real_gr or 0 for c in cajas)
            pedido.save(update_fields=["peso_real_gr", "actualizado"])
            registrar_evento(
                "pedido", pedido.pk, "reempacado_por_caja", actor=actor, cliente=pedido.cliente,
                delta={"cajas": [(c.numero, c.peso_real_gr) for c in cajas]},
                motivo="Pedido empacado entero que Mesa replaneó en cajas: reempacado caja por caja.",
            )
        else:
            # Última caja: el pedido completo queda EMPACADO con el peso real
            # total. peso_ya_verificado: cada caja ya pasó báscula contra SU plan
            # (que incluye el margen de empaque) — la suma NO se revalida contra
            # el peso_esperado_gr NETO del pedido.
            empacar(
                pedido, actor, sum(c.peso_real_gr or 0 for c in cajas), fotos=[],
                peso_ya_verificado=True,
            )
    # La instancia del caller refleja el estado real (las vistas la usan).
    if fresco is not paquete:
        paquete.estado = fresco.estado
        paquete.peso_real_gr = fresco.peso_real_gr
        paquete.foto_contenido = evidencia
    return fresco


@transaction.atomic
def cerrar_caja(paquete, actor, foto_caja_cerrada):
    """Cierre de caja con evidencia REAL: la caja cerrada CON su etiqueta pegada.

    Solo aplica a cajas EMPACADO/DESPACHADO y con guía (antes de la guía la
    etiqueta no existe — por eso la foto ya no se pide en empacar). Adjunta
    EvidenciaFoto tipo "caja_cerrada" ligada al pedido y la estampa en el
    paquete (ts_cierre + foto_cierre: el estado vive en columnas, el evento
    es bitácora). Una caja no se cierra dos veces: el candado (ts_cierre) se
    revisa con el paquete bajo select_for_update — dos POSTs concurrentes se
    serializan.
    """
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    fresco = Paquete.objects.select_for_update().select_related("pedido").get(pk=paquete.pk)
    pedido = fresco.pedido
    if fresco.estado not in (Paquete.EMPACADO, Paquete.DESPACHADO):
        raise ValueError(
            f"La caja {fresco.numero} de {pedido.folio} aún no está empacada; "
            "empácala antes de tomar la foto de cierre."
        )
    tiene_guia = fresco.guia_activa is not None or any(
        g.es_activa for g in pedido.guias.filter(paquete__isnull=True)
    )
    if not tiene_guia:
        raise ValueError(
            f"La caja {fresco.numero} de {pedido.folio} todavía no tiene guía; "
            "genera la guía, pega la etiqueta y entonces toma la foto de cierre."
        )
    if foto_caja_cerrada is None:
        raise ValueError(
            f"Toma la foto de la caja {fresco.numero} cerrada con la etiqueta pegada."
        )
    if fresco.ts_cierre is not None:
        raise ValueError(
            f"La caja {fresco.numero} de {pedido.folio} ya tiene su foto de cierre."
        )

    evidencia = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo=TIPO_FOTO_CIERRE,
        archivo=foto_caja_cerrada, tomada_por=_actor_nombre(actor),
    )
    fresco.ts_cierre = timezone.now()
    fresco.foto_cierre = evidencia
    fresco.save(update_fields=["ts_cierre", "foto_cierre"])
    if fresco is not paquete:
        paquete.ts_cierre = fresco.ts_cierre
        paquete.foto_cierre = evidencia
    registrar_evento(
        "paquete", fresco.pk, "caja_cerrada_con_evidencia", actor=actor,
        cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "caja": fresco.numero, "evidencia_id": evidencia.pk},
        motivo=f"Caja {fresco.numero} de {pedido.folio} cerrada con la etiqueta pegada.",
    )
    return fresco


@transaction.atomic
def corregir_peso_caja(paquete, actor, peso_real_gr):
    """Corrige la báscula de una caja ya empacada (se capturó el peso de otra caja o pedido).

    Misma verificación que al empacar (_verificar_peso contra el esperado de
    ESA caja, según TORRE_PESO_MODO); guarda Paquete.peso_real_gr y, si el
    pedido ya quedó empacado, recalcula Pedido.peso_real_gr = Σ cajas. No
    reabre nada: estado, fotos y cierre de la caja siguen igual. Si la guía
    ya se compró, viaja con el peso anterior (el carrier re-pesa de todos
    modos): la corrección sirve a costos y reportes, y a la guía solo si aún
    no existe. Evento peso_corregido con el de/a. Caja sin pesar
    (PLANEADO/EN_EMPAQUE) o mismo peso: ValueError.
    """
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    fresco = Paquete.objects.select_for_update().get(pk=paquete.pk)
    pedido = Pedido.objects.select_for_update().get(pk=fresco.pedido_id)
    if fresco.estado not in (Paquete.EMPACADO, Paquete.DESPACHADO):
        raise ValueError(
            f"La caja {fresco.numero} de {pedido.folio} todavía no se ha pesado; "
            "pásala por su paso de empaque."
        )
    try:
        peso_real = int(peso_real_gr)
    except (TypeError, ValueError):
        peso_real = 0
    if peso_real <= 0:
        raise ValueError(
            f"Captura el peso de la báscula de la caja {fresco.numero}, en gramos."
        )
    anterior = fresco.peso_real_gr
    if peso_real == anterior:
        raise ValueError(
            f"La caja {fresco.numero} ya tiene {anterior} g; no hay nada que corregir."
        )
    esperado = _peso_esperado_caja(fresco)
    _verificar_peso(pedido, peso_real, esperado, caja=fresco.numero)
    fresco.peso_real_gr = peso_real
    fresco.save(update_fields=["peso_real_gr"])
    if pedido.peso_real_gr is not None:
        pedido.peso_real_gr = sum(c.peso_real_gr or 0 for c in pedido.paquetes.all())
        pedido.save(update_fields=["peso_real_gr", "actualizado"])
    registrar_evento(
        "paquete", fresco.pk, "peso_corregido", actor=actor, cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "caja": fresco.numero, "de_gr": anterior,
               "a_gr": peso_real, "plan_gr": esperado},
        motivo=(f"Báscula de la caja {fresco.numero} de {pedido.folio} corregida: "
                f"{anterior} g → {peso_real} g."),
    )
    paquete.peso_real_gr = peso_real
    return fresco


@transaction.atomic
def reemplazar_foto_pedido(pedido, actor, evidencia_id, foto):
    """Cambia una foto de empaque (contenido o caja cerrada) que salió mal.

    La foto nueva entra como EvidenciaFoto del mismo tipo y las cajas que
    apuntaban a la vieja (Paquete.foto_contenido / foto_cierre) quedan
    apuntando a la nueva, con ts_cierre y peso intactos: cambiar la foto no
    reabre ni re-pesa nada. La vieja se borra, registro y archivo — no es
    evidencia de nada (era la caja equivocada, salió movida) y seguiría
    saliendo en el portal del cliente y en el reporte del día. El evento
    foto_reemplazada (entidad "paquete" si la foto era de UNA caja, "pedido"
    en el cierre único legacy) guarda id, SHA-256 y hora de la que se fue y
    el id de la nueva: la bitácora no pierde el rastro. Una foto congelada
    (expediente de incidencia) no se toca: ValueError. Ligada a otro pedido
    o de otro tipo: ValueError, sin filtrar existencia. El archivo viejo se
    borra al confirmar la transacción (on_commit), nunca antes.
    """
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    if foto is None:
        raise ValueError("Toma la foto nueva antes de confirmar el cambio.")
    try:
        evidencia_id = int(evidencia_id)
    except (TypeError, ValueError):
        evidencia_id = 0
    vieja = EvidenciaFoto.objects.filter(
        pk=evidencia_id, entidad="pedido", entidad_id=str(pedido.pk),
        tipo__in=(TIPO_FOTO_CONTENIDO, TIPO_FOTO_CIERRE),
    ).first()
    if vieja is None:
        raise ValueError(
            f"Esa foto no es de empaque de {pedido.folio}; no hay nada que cambiar."
        )
    if vieja.congelada:
        raise ValueError(
            f"La foto forma parte del expediente de una incidencia de {pedido.folio}; "
            "no se puede reemplazar."
        )
    cajas = list(
        Paquete.objects.select_for_update()
        .filter(Q(foto_contenido=vieja) | Q(foto_cierre=vieja), pedido=pedido)
        .order_by("numero")
    )
    nueva = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo=vieja.tipo,
        archivo=foto, tomada_por=_actor_nombre(actor),
    )
    for caja in cajas:
        campos = []
        if caja.foto_contenido_id == vieja.pk:
            caja.foto_contenido = nueva
            campos.append("foto_contenido")
        if caja.foto_cierre_id == vieja.pk:
            caja.foto_cierre = nueva
            campos.append("foto_cierre")
        caja.save(update_fields=campos)
    anterior = {"id": vieja.pk, "sha256": vieja.hash_sha256, "ts": vieja.ts.isoformat()}
    archivo_viejo = vieja.archivo
    vieja.delete()
    que = "del contenido" if nueva.tipo == TIPO_FOTO_CONTENIDO else "de la caja cerrada"
    numeros = [c.numero for c in cajas]
    if len(cajas) == 1:
        entidad, entidad_id, donde = "paquete", cajas[0].pk, f"caja {numeros[0]}"
    else:
        entidad, entidad_id = "pedido", pedido.pk
        donde = "cajas " + ", ".join(str(n) for n in numeros) if numeros else "cierre único"
    registrar_evento(
        entidad, entidad_id, "foto_reemplazada", actor=actor, cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "cajas": numeros, "tipo": nueva.tipo,
               "evidencia_anterior": anterior, "evidencia_id": nueva.pk},
        motivo=f"Foto {que} de {pedido.folio} ({donde}) reemplazada: la anterior salió mal.",
    )

    def borrar_archivo_viejo():
        try:
            archivo_viejo.delete(save=False)
        except OSError:
            pass

    transaction.on_commit(borrar_archivo_viejo)
    return nueva


# ── Guía y salida ──

def generar_guia(pedido):
    """EMPACADO → GUIA_GENERADA vía envios.services.generar_guia (lazy, idempotente)."""
    if pedido.estado not in (Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        raise ValueError(
            f"El pedido {pedido.folio} debe estar empacado antes de generar guía "
            f"(está {pedido.get_estado_display()})."
        )
    from apps.envios.services import generar_guia as generar_guia_envio  # lazy
    guia = generar_guia_envio(pedido)
    if pedido.estado == Pedido.EMPACADO:
        numero = getattr(guia, "numero", "")
        pedido.transicionar(Pedido.GUIA_GENERADA, motivo=f"Guía {numero} generada".strip())
    return guia


def imprimir_guias_activas(pedido):
    """Manda a imprimir, BEST-EFFORT, la etiqueta del carrier y la interna
    (folio + QR) de cada guía activa del pedido: una falla de impresora se
    acumula en mensajes y jamás toca la guía (la reimpresión vive en Salida).
    Regresa (guias, mensajes)."""
    from apps.envios.models import Guia  # lazy: modelo de otra app
    from apps.piso.etiquetas import imprimir_etiqueta  # lazy por contrato

    guias = list(
        pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).order_by("id")
    )
    mensajes = []
    for guia in guias:
        try:
            mensajes.append(imprimir_etiqueta(guia))
        except Exception as exc:  # noqa: BLE001 — best-effort: la guía ya existe y NUNCA se revierte
            mensajes.append(f"No se imprimió la etiqueta de la guía {guia.numero}: {exc}")
        # La etiqueta INTERNA sale JUNTO con la del carrier: identidad de la
        # caja en una cara, ruteo en la otra. Mismo best-effort.
        try:
            mensajes.append(imprimir_etiqueta(guia, interna=True))
        except Exception as exc:  # noqa: BLE001
            mensajes.append(f"No se imprimió la etiqueta interna de {guia.numero}: {exc}")
    return guias, mensajes


def despachar_a_corral(pedido, actor):
    """Guía + impresión de etiquetas en el MISMO POST del empaque (carril único).

    Encadena generar_guia (idempotente; transiciona a GUIA_GENERADA) y manda
    a imprimir las guías activas (imprimir_guias_activas, best-effort). Si una
    caja falla con el carrier, las guías que SÍ salieron se imprimen igual
    antes de propagar el error (Chema 2026-09-21: antes la excepción cortaba
    antes de imprimir y ninguna etiqueta salía); la caja caída se genera desde
    Salida. Regresa {"guias": [...], "mensajes": [...]}.
    """
    error = None
    try:
        generar_guia(pedido)
    except Exception as exc:  # noqa: BLE001 — ErrorCarrier/ValueError: se imprime lo que sí salió y luego se propaga
        error = exc
    guias, mensajes = imprimir_guias_activas(pedido)
    registrar_evento(
        "pedido", pedido.pk, "despachado_a_corral", actor=actor, cliente=pedido.cliente,
        delta={"guias": [g.numero for g in guias], "mensajes_impresion": mensajes,
               "error": str(error)[:200] if error else None},
        motivo="Guía(s) e impresión encadenadas al empaque: el pedido va directo a su corral.",
    )
    if error is not None:
        raise error
    return {"guias": guias, "mensajes": mensajes}


def regresar_a_empaque(pedido, actor, motivo=""):
    """Cambio de dirección con guía comprada y NADA en la calle (Chema
    2026-09-23, incidencia "Cambio de dirección"): aplica la dirección
    pendiente (la que Shopify mandó con la guía ya comprada), cancela con el
    carrier las guías activas (envios.cancelar_guia), borra el cierre de las
    cajas (la etiqueta cambia: foto de cierre nueva; la foto vieja queda como
    historia, re-tipada), re-cotiza cada caja con la dirección nueva (puede
    cambiar carrier y precio), deja el pedido SIN dueño (lo toma quien esté en
    la mesa) y lo regresa GUIA_GENERADA → EMPACADO. Cae solo a "Completar
    empaquetado" ("sin guía") y "Reintentar guía" compra la nueva. Con algo ya
    despachado → ValueError: eso se resuelve con el carrier o el comprador."""
    from apps.envios.adapters import ErrorCarrier  # lazy por contrato
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.envios.services import cancelar_guia, recotizar_paquete  # lazy por contrato

    if pedido.estado != Pedido.GUIA_GENERADA:
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display().lower()}: solo se regresa a "
            "empaque un pedido con guía que aún no sale."
        )
    fuera = [c.numero for c in pedido.paquetes.all() if c.estado == Paquete.DESPACHADO]
    if fuera or pedido.tiene_despachadas:
        raise ValueError(
            f"{pedido.folio} ya salió (caja {', '.join(str(n) for n in fuera) or 'entera'}): "
            "no se regresa a empaque; se resuelve con el carrier o el comprador."
        )
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        campos = ["asignado_a"]
        fresco.asignado_a = None
        aplicada = fresco.direccion_pendiente
        if aplicada:
            campos += _aplicar_direccion(fresco, aplicada)
            fresco.direccion_pendiente = None
            campos.append("direccion_pendiente")
        fresco.save(update_fields=campos + ["actualizado"])
        canceladas = []
        for guia in [g for g in fresco.guias.all() if g.es_activa]:
            cancelar_guia(guia, actor, motivo=motivo or "Cambio de dirección: guía cancelada antes de salir")
            canceladas.append(guia.numero)
        recotizadas = []
        for caja in fresco.paquetes.all():
            campos = []
            if caja.ts_cierre is not None or caja.foto_cierre_id:
                caja.ts_cierre, caja.foto_cierre = None, None
                campos += ["ts_cierre", "foto_cierre"]
            if campos:
                caja.save(update_fields=campos)
            try:
                recotizar_paquete(fresco, caja)
                recotizadas.append(caja.numero)
            except (ErrorCarrier, ValueError):
                pass  # se queda con su carrier; la compra decide
        # Cierre legacy (sin cajas): la foto vieja ya no cuenta como cierre.
        EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(fresco.pk), tipo=TIPO_FOTO_CIERRE,
        ).update(tipo="cierre_anulado")
        fresco.transicionar(
            Pedido.EMPACADO, actor=actor,
            motivo=(motivo or "Cambio de dirección: guías canceladas, se compra guía nueva")[:300],
        )
        registrar_evento(
            "pedido", fresco.pk, "regresado_a_empaque", actor=actor, cliente=fresco.cliente,
            delta={
                "guias_canceladas": canceladas, "cajas_recotizadas": recotizadas,
                "direccion_aplicada": bool(aplicada),
            },
            motivo=(motivo or "Cambio de dirección con guía comprada: etiqueta y foto de cierre nuevas.")[:300],
        )
    pedido.estado = fresco.estado
    return fresco


def _lineas_de_cajas(cajas):
    """{pk: LineaPedido} de lo que viaja en esas cajas; un kit arrastra a sus
    hijas (el kardex despacha las hijas, nunca el kit virtual)."""
    lineas = {}
    for caja in cajas:
        for pl in caja.lineas.select_related("linea_pedido__sku"):
            linea = pl.linea_pedido
            lineas[linea.pk] = linea
            if linea.sku.es_kit:
                for hija in linea.componentes.select_related("sku"):
                    lineas[hija.pk] = hija
    return lineas


def cajas_por_salir(pedido):
    """Cajas EMPACADO del pedido (aún en bodega) cuando se empacó POR CAJA;
    [] si el pedido se empacó entero (legacy: sin plan o plan sin empacar por
    caja), que sale completo en un solo manifiesto."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    cajas = sorted(pedido.paquetes.all(), key=lambda c: c.numero)
    if not any(c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO) for c in cajas):
        return []
    return [c for c in cajas if c.estado == Paquete.EMPACADO]


def marcar_recolectado(pedido, actor, paquetes=None):
    """GUIA_GENERADA / PARCIALMENTE_DESPACHADO → RECOLECTADO (o
    PARCIALMENTE_DESPACHADO si solo salieron algunas cajas): escaneo de
    salida + manifiesto.

    `paquetes` = cajas que suben a ESTE manifiesto (None = todas las que
    faltan). Cada caja pasa a DESPACHADO; el kardex despacha (en_empaque →
    salida) las líneas de esas cajas la primera vez que alguna de sus
    unidades sale — una caja 24 reempacada en dos medias sale del kardex con
    la primera media: la unidad de venta ya se abrió. Lo despachado queda en
    `LineaPedido.cantidad_despachada`: una segunda ola (fulfillment parcial)
    despacha solo lo nuevo, nunca dos veces. Pedido empacado entero
    (sin cajas EMPACADO): sale completo, como siempre. Con cajas pendientes
    el pedido queda PARCIALMENTE_DESPACHADO; con todas fuera, RECOLECTADO.
    Con líneas sin inventario (fulfillment parcial) también queda
    PARCIALMENTE_DESPACHADO: espera stock y vuelve a PENDIENTE para su
    segunda ola con el mismo folio.

    Todo el efecto de dominio (kardex + transiciones) va en UNA transacción:
    una línea que falle revierte completo. La plantilla B ("va en camino")
    sale SOLO aquí y una vez por OLA: en el primer manifiesto de cada una (el
    rastreo público muestra cada caja). El fulfillment en Shopify sale POR
    CAJA en cada manifiesto (Partially fulfilled hasta la última), o entero
    cuando no hay cajas; el comprador recibe el correo de envío de Shopify
    una vez por ola (`notificar`).
    """
    if pedido.estado not in (Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
        raise ValueError(
            f"El pedido {pedido.folio} no tiene guía lista (está {pedido.get_estado_display()}); "
            "no se puede marcar recolectado."
        )
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.inventario.services import despachar  # lazy

    primer_manifiesto = pedido.estado == Pedido.GUIA_GENERADA
    with transaction.atomic():
        Pedido.objects.select_for_update().get(pk=pedido.pk)
        pendientes = cajas_por_salir(pedido)
        if pendientes and paquetes is not None:
            ids = {c.pk for c in paquetes}
            salen = [c for c in pendientes if c.pk in ids]
        else:
            salen = pendientes
        if pendientes and not salen:
            raise ValueError(
                f"{pedido.folio}: ninguna de las cajas palomeadas sigue pendiente de salir."
            )
        if salen:
            lineas = list(_lineas_de_cajas(salen).values())
        else:
            lineas = list(pedido.lineas.select_related("sku"))
        for linea in lineas:
            # Sale del kardex lo pickeado que aún no ha salido: una línea
            # repartida en dos cajas sale entera con la primera (la unidad de
            # venta ya se abrió) y en una segunda ola lo de la primera ya se
            # fue (cantidad_despachada): nada se despacha dos veces.
            por_despachar = linea.cantidad_pickeada - linea.cantidad_despachada
            if por_despachar <= 0:
                continue
            if not linea.sku.es_kit:
                despachar(linea.sku, por_despachar, pedido.folio)
            linea.cantidad_despachada = linea.cantidad_pickeada
            linea.save(update_fields=["cantidad_despachada"])
        for caja in salen:
            caja.transicionar(
                Paquete.DESPACHADO, actor=actor,
                motivo=f"Caja {caja.numero} de {pedido.folio} subió al camión (manifiesto firmado).",
            )
        quedan = [c for c in pendientes if c not in salen]
        sin_inventario = sorted({l.sku.codigo for l in pedido.lineas_faltantes})
        if quedan:
            numeros = ", ".join(str(c.numero) for c in salen)
            faltan = ", ".join(str(c.numero) for c in quedan)
            motivo = f"Manifiesto por caja: salió la caja {numeros}; la caja {faltan} sigue en bodega."
            if pedido.estado == Pedido.GUIA_GENERADA:
                pedido.transicionar(Pedido.PARCIALMENTE_DESPACHADO, actor=actor, motivo=motivo)
            else:
                registrar_evento(
                    "pedido", pedido.pk, "salida_parcial", actor=actor, cliente=pedido.cliente,
                    delta={"salen": [c.numero for c in salen], "quedan": [c.numero for c in quedan]},
                    motivo=motivo,
                )
        elif sin_inventario:
            # Fulfillment parcial: salió lo que había; lo sin inventario espera
            # stock con el mismo folio (reintentar_reservas_sku lo reabre).
            motivo = (
                f"Salió lo que había; sin inventario: {', '.join(sin_inventario)}. "
                "Se completa con el mismo folio cuando entre stock."
            )
            if pedido.estado == Pedido.GUIA_GENERADA:
                pedido.transicionar(Pedido.PARCIALMENTE_DESPACHADO, actor=actor, motivo=motivo)
            else:
                registrar_evento(
                    "pedido", pedido.pk, "salida_parcial", actor=actor, cliente=pedido.cliente,
                    delta={"salen": [c.numero for c in salen], "sin_inventario": sin_inventario},
                    motivo=motivo,
                )
        else:
            pedido.transicionar(
                Pedido.RECOLECTADO, actor=actor,
                motivo="Escaneo de salida + manifiesto firmado (RECOLECTADO autoritativo).",
            )
    if primer_manifiesto:
        try:
            from apps.mensajeria.services import enviar_en_camino  # lazy — plantilla B: SOLO aquí
        except ImportError:
            pass
        else:
            enviar_en_camino(pedido)
    # Hermano del "va en camino": el fulfillment en Shopify sale del MISMO
    # momento canónico (correo nativo de envío + Fulfilled en el admin del
    # cliente). Con cajas, UN fulfillment por caja que sube a ESTE camión
    # (Shopify muestra Partially fulfilled hasta la última); sin cajas, el
    # pedido entero. Best-effort total en on_commit: Shopify jamás bloquea
    # un manifiesto.
    cajas_fuera = list(salen)

    def _fulfillment():
        try:
            from apps.integraciones.services import marcar_fulfillment  # lazy
            if cajas_fuera:
                marcar_fulfillment(pedido, cajas=cajas_fuera, notificar=primer_manifiesto)
            else:
                marcar_fulfillment(pedido, notificar=primer_manifiesto)
        except Exception:
            pass
    transaction.on_commit(_fulfillment)
    return pedido


def entregar_sin_guia(pedido, actor, recibio="", motivo=""):
    """Entrega en mano / recolección en bodega (Chema 2026-09-21): el pedido
    sale sin carrier ni guía y queda ENTREGADO. Vale desde PENDIENTE,
    EN_PICKING o EMPACADO; con guía generada primero se cancela la guía por
    el flujo normal, y de la calle en adelante ya no aplica. Todo en una
    transacción: 1) PENDIENTE pasa por iniciar_picking (exige reservas) y en
    picking se confirma lo que falte de cada línea (confirmar_linea_pick sin
    escaneo); 2) el físico sale del kardex: en picking confirmar_pick +
    despachar por línea (reservado → en_empaque → despachado); ya EMPACADO
    solo despachar, porque empacar ya confirmó el pick; los kits no tienen
    stock propio; 3) las cajas del plan quedan DESPACHADO; 4) el pedido pasa
    a ENTREGADO estampando empacado, recolectado y entregado, y se libera del
    operador de piso (asignado_a). No hay transición directa en la máquina de
    estados: es un atajo explícito, auditado como entregado_sin_guia con
    quién recibió. 5) Shopify: fulfillment sin rastreo con evento DELIVERED,
    best-effort en on_commit, para que el admin lo vea Fulfilled y entregado
    y el cliente reciba su aviso. Sin "va en camino" de WhatsApp: nunca
    viajó. Regresa el pedido."""
    if pedido.estado == Pedido.GUIA_GENERADA:
        raise ValueError(
            f"{pedido.folio} ya tiene guía generada: cancela la guía primero y vuelve a intentar."
        )
    if pedido.estado not in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO):
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display()}: la entrega sin guía "
            "solo aplica antes de generar la guía."
        )
    faltantes = sorted({l.sku.codigo for l in pedido.lineas_faltantes})
    if faltantes:
        # ENTREGADO es terminal: no hay segunda ola después. Lo sin inventario
        # se resuelve (o se quita del pedido) antes de entregar en bodega.
        raise ValueError(
            f"{pedido.folio} tiene líneas sin inventario ({', '.join(faltantes)}): "
            "consigue el stock o quítalas del pedido antes de entregarlo en bodega."
        )
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.inventario.services import confirmar_pick, despachar  # lazy

    with transaction.atomic():
        # Sin select_related: tienda es nullable y Postgres no admite FOR UPDATE
        # sobre el lado nulo de un outer join (SQLite lo ignora y no lo delata).
        pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
        estado_inicial = pedido.estado
        if pedido.estado == Pedido.PENDIENTE:
            iniciar_picking(pedido, actor)
        lineas = list(pedido.lineas.select_related("sku"))
        if pedido.estado == Pedido.EN_PICKING:
            for linea in lineas:
                faltan = linea.cantidad - linea.cantidad_pickeada
                if faltan > 0:
                    confirmar_linea_pick(linea, faltan, actor)
        for linea in lineas:
            por_despachar = linea.cantidad_pickeada - linea.cantidad_despachada
            if por_despachar <= 0:
                continue  # sin pick, o ya salió con la primera ola
            if not linea.sku.es_kit:
                if estado_inicial != Pedido.EMPACADO:
                    confirmar_pick(linea.sku, por_despachar, pedido.folio)
                despachar(linea.sku, por_despachar, pedido.folio)
            linea.cantidad_despachada = linea.cantidad_pickeada
            linea.save(update_fields=["cantidad_despachada"])
        for caja in pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero"):
            if caja.estado != Paquete.EMPACADO:
                caja.transicionar(Paquete.EMPACADO, actor=actor, motivo=f"Entrega sin guía de {pedido.folio}.")
            caja.transicionar(
                Paquete.DESPACHADO, actor=actor,
                motivo=f"Entrega sin guía de {pedido.folio}: la caja {caja.numero} se entregó en bodega.",
            )
        ahora = timezone.now()
        anterior = pedido.estado
        pedido.estado = Pedido.ENTREGADO
        pedido.asignado_a = None
        campos = ["estado", "asignado_a", "actualizado"]
        for estado in (Pedido.EMPACADO, Pedido.RECOLECTADO, Pedido.ENTREGADO):
            campo = Pedido.TIMESTAMPS_TRANSICION[estado]
            if getattr(pedido, campo) is None:
                setattr(pedido, campo, ahora)
                campos.append(campo)
        pedido.save(update_fields=campos)
        registrar_evento(
            "pedido", pedido.pk, "entregado_sin_guia", actor=actor, cliente=pedido.cliente,
            delta={
                "de": anterior, "estado_inicial": estado_inicial, "recibio": recibio,
                "lineas": [[l.sku.codigo, l.cantidad_pickeada] for l in lineas],
            },
            motivo=(motivo or "Entrega en bodega sin guía.")[:300],
        )

        def _fulfillment():
            try:
                from apps.integraciones.services import marcar_fulfillment  # lazy
                marcar_fulfillment(pedido, evento_inicial="DELIVERED")
            except Exception:  # noqa: BLE001, S110 — best-effort: Shopify caído no deshace la entrega
                pass
        transaction.on_commit(_fulfillment)
    return pedido


# ── Cancelación ──

def cancelar(pedido, actor, motivo=""):
    """Matriz de cancelación por estado (BLUEPRINT §2.3).

    - PENDIENTE: libera reservas y cancela directo.
    - EN_PICKING / EMPACADO / GUIA_GENERADA: cancela directo; lo pickeado entra
      como OrdenEntrada tipo reingreso (RECIBIDA, stock en put-away) para que
      el piso lo ubique por recepción; el resto libera su reserva; las guías
      activas se cancelan con el carrier best-effort.
    - Ya despachado (RECOLECTADO / EN_TRANSITO / ...): el paquete ya salió →
      se marca cancelacion_tardia y se abre incidencia CAN; el pedido pasa a
      CANCELADO cuando Mesa decide el reingreso (registrar_reingreso /
      marcar_no_recuperado) o resuelve la CAN, lo que ocurra primero.
    - Segunda ola en bodega con la primera ya en la calle (fulfillment
      parcial): lo de bodega se libera / reingresa como siempre y lo que
      salió (cantidad_despachada) sigue el camino de la cancelación tardía;
      el pedido queda CANCELADO con la decisión de reingreso abierta en Mesa.
    - Terminales: ValueError.
    """
    estado = pedido.estado
    en_bodega = (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA)
    if estado in en_bodega and pedido.tiene_despachadas:
        _devolver_stock_y_cancelar(
            pedido, actor, motivo or "Cancelación con una parte del pedido ya en la calle",
            tardia=True,
        )
    elif estado == Pedido.PENDIENTE:
        _liberar_reservas(pedido)
        pedido.transicionar(Pedido.CANCELADO, actor=actor, motivo=motivo or "Cancelación directa")
    elif estado in (Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        _devolver_stock_y_cancelar(pedido, actor, motivo or "Cancelación con mercancía en proceso")
    elif estado in (
        Pedido.RECOLECTADO, Pedido.EN_TRANSITO,
        Pedido.PARCIALMENTE_DESPACHADO, Pedido.ENTREGA_PRESUNTA,
    ):
        pedido.incidencia_activa = True
        pedido.cancelacion_tardia = True
        pedido.save(update_fields=["incidencia_activa", "cancelacion_tardia", "actualizado"])
        registrar_evento(
            "pedido", pedido.pk, "cancelacion_tardia", actor=actor, cliente=pedido.cliente,
            motivo=motivo or "Cancelación solicitada con el paquete ya despachado.",
        )
        _abrir_incidencia_can(
            pedido,
            f"Cancelación tardía: el pedido {pedido.folio} ya salió de bodega "
            f"({pedido.get_estado_display()}). {motivo}".strip(),
        )
    else:
        raise ValueError(
            f"No se puede cancelar el pedido {pedido.folio}: está {pedido.get_estado_display()}."
        )
    return pedido


def _abrir_incidencia_can(pedido, texto):
    """Incidencia CAN de una cancelación con mercancía en la calle (lazy por
    contrato; sin módulo de incidencias no pasa nada)."""
    try:
        from apps.incidencias.services import abrir_incidencia  # lazy
    except ImportError:
        return None
    return abrir_incidencia(pedido.cliente, "CAN", "auto", pedido=pedido, texto=texto)


def confirmar_restock(pedido, actor, motivo=""):
    """Cierra un pedido que quedó en CANCELACION_PENDIENTE (flujo anterior al
    reingreso): misma devolución de stock que la cancelación en bodega."""
    if pedido.estado != Pedido.CANCELACION_PENDIENTE:
        raise ValueError(
            f"El pedido {pedido.folio} no tiene cancelación pendiente "
            f"(está {pedido.get_estado_display()})."
        )
    return _devolver_stock_y_cancelar(
        pedido, actor, motivo or "Restock confirmado en piso; cancelación cerrada.",
    )


def _devolver_stock_y_cancelar(pedido, actor, motivo, tardia=False):
    """Núcleo de la cancelación con mercancía en proceso, en UNA transacción:
    por línea (kits fuera), lo pickeado que sigue en bodega → reingreso
    (put-away en recepción, desde empaque si ESTA ola ya se empacó, desde
    vendible si sigue en carrito) y el resto libera su reserva; si hubo
    reingreso nace una OrdenEntrada tipo reingreso ya RECIBIDA; el pedido pasa
    a CANCELADO. Las guías activas se cancelan con el carrier fuera de la
    transacción, best-effort.

    `tardia=True` (segunda ola cancelada con la primera en la calle): lo que
    ya salió (cantidad_despachada) no se toca — se marca cancelacion_tardia,
    se abre la CAN y la decisión de reingreso de ESA parte queda abierta en
    Mesa aunque lo de bodega ya haya vuelto; vale también desde PENDIENTE.
    """
    from apps.inventario.models import LineaASN, OrdenEntrada  # lazy: modelo de otra app
    from apps.inventario.services import liberar_reserva, reingresar_desde_pedido  # lazy

    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        pedido.estado = fresco.estado
        permitidos = (
            Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.CANCELACION_PENDIENTE,
        )
        if tardia:
            permitidos += (Pedido.PENDIENTE,)  # segunda ola aún sin empezar
        if pedido.estado not in permitidos:
            raise ValueError(
                f"No se puede cancelar el pedido {pedido.folio}: está {pedido.get_estado_display()}."
            )
        # De dónde vuelve lo pickeado: de empaque si ESTA ola ya se empacó.
        # ts_empacado no sirve en una segunda ola: lo estampó la primera.
        empacado = pedido.estado in (Pedido.EMPACADO, Pedido.GUIA_GENERADA) or (
            pedido.estado == Pedido.CANCELACION_PENDIENTE and pedido.ts_empacado is not None
        )
        reingreso, en_calle = [], []
        for linea in pedido.lineas.select_related("sku"):
            if linea.cantidad_despachada:
                en_calle.append(f"{linea.sku.codigo} × {linea.cantidad_despachada}")
            if linea.sku.es_kit:
                continue
            # Lo que ya salió en un manifiesto (cantidad_despachada) no está
            # en bodega: sigue el camino de la cancelación tardía.
            pickeada = linea.cantidad_pickeada - linea.cantidad_despachada
            if pickeada > 0:
                reingresar_desde_pedido(linea.sku, pickeada, pedido.folio, actor, desde_empaque=empacado)
                reingreso.append((linea.sku, pickeada))
            resto = linea.cantidad - linea.cantidad_pickeada
            if resto > 0 and linea.reservada:
                liberar_reserva(linea.sku, resto, pedido.folio)
            if linea.reservada and not linea.cantidad_despachada:
                linea.reservada = False
                linea.save(update_fields=["reservada"])
        orden = None
        if reingreso:
            ahora = timezone.now()
            orden = OrdenEntrada.objects.create(
                cliente=pedido.cliente, tipo=OrdenEntrada.TIPO_REINGRESO, pedido=pedido,
                estado=OrdenEntrada.RECIBIDA, fecha_compromiso=timezone.localdate(),
                ts_descarga_fin=ahora,
            )
            for sku, piezas in reingreso:
                LineaASN.objects.create(
                    orden=orden, sku=sku, cantidad_anunciada=piezas, cantidad_recibida=piezas,
                )
            registrar_evento(
                "asn", orden.folio, "reingreso_creado", actor=actor, cliente=pedido.cliente,
                delta={"pedido": pedido.folio, "desde": "empaque" if empacado else "carrito",
                       "lineas": [{"sku": s.codigo, "cantidad": n} for s, n in reingreso]},
                motivo=f"Mercancía de {pedido.folio} de vuelta a recepción; el piso la ubica.",
            )
            if not tardia:
                pedido.reingreso_estado = Pedido.REINGRESADO
                pedido.save(update_fields=["reingreso_estado", "actualizado"])
        if tardia:
            # La parte en la calle: misma señal que la cancelación tardía
            # normal; Mesa decide su reingreso (o lo da por no recuperado).
            pedido.incidencia_activa = True
            pedido.cancelacion_tardia = True
            pedido.save(update_fields=["incidencia_activa", "cancelacion_tardia", "actualizado"])
            registrar_evento(
                "pedido", pedido.pk, "cancelacion_tardia", actor=actor, cliente=pedido.cliente,
                delta={"en_calle": en_calle,
                       "reingreso_bodega": [{"sku": s.codigo, "cantidad": n} for s, n in reingreso]},
                motivo=f"{motivo} Ya en la calle: {', '.join(en_calle)}."[:300],
            )
        pedido.transicionar(Pedido.CANCELADO, actor=actor, motivo=motivo)
    if tardia:
        _abrir_incidencia_can(
            pedido,
            f"Cancelación con una parte de {pedido.folio} ya en la calle "
            f"({', '.join(en_calle)}); lo de bodega ya se liberó o reingresó. {motivo}".strip(),
        )
    _cancelar_guias_best_effort(pedido, actor)
    return pedido


def _cancelar_guias_best_effort(pedido, actor):
    """Avisa al carrier de cada guía activa (adapter.cancelar); un fallo del
    carrier no revierte la cancelación: queda auditado para que Mesa lo persiga."""
    try:
        from apps.envios.models import Guia, Paquete
        from apps.envios.services import get_adapter  # lazy por contrato
    except ImportError:
        return
    guias = pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).select_related("paquete")
    for guia in guias:
        if guia.paquete_id and guia.paquete.estado == Paquete.DESPACHADO:
            continue  # ya en la calle (primera ola): la CAN decide, no se cancela con el carrier
        try:
            adapter = get_adapter(guia.carrier, proveedor=guia.proveedor, cliente=pedido.cliente)
            ok = bool(adapter.cancelar(guia))
            detalle = ""
        except Exception as exc:  # noqa: BLE001 — best-effort: el carrier no bloquea la cancelación
            ok, detalle = False, str(exc)[:200]
        registrar_evento(
            "guia", guia.pk, "cancelada_carrier" if ok else "cancelacion_carrier_fallida",
            actor=actor, cliente=pedido.cliente,
            delta={"numero": guia.numero, "carrier": guia.carrier, "proveedor": guia.proveedor, "detalle": detalle},
            motivo=f"Pedido {pedido.folio} cancelado con guía activa.",
        )


ESTADOS_CON_MERCANCIA_FUERA = (
    Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.PARCIALMENTE_DESPACHADO,
    Pedido.ENTREGA_PRESUNTA, Pedido.ENTREGADO, Pedido.RETORNADO,
)


def reingresos_por_decidir():
    """Pedidos que ya salieron y cuya mercancía puede volver, sin decisión de Mesa:
    RETORNADO por el carrier, o cancelación tardía (aunque la CAN ya se haya
    resuelto y el pedido esté CANCELADO: la mercancía sigue por decidir)."""
    return (
        Pedido.objects.filter(reingreso_estado=Pedido.REINGRESO_PENDIENTE)
        .filter(Q(estado=Pedido.RETORNADO) | Q(cancelacion_tardia=True))
        .filter(estado__in=ESTADOS_CON_MERCANCIA_FUERA + (Pedido.CANCELADO,))
        .select_related("cliente").order_by("actualizado")
    )


def cerrar_cancelacion_tardia(pedido, actor, motivo=""):
    """Pasa a CANCELADO un pedido cancelado con el paquete en la calle. Lo
    dispara lo que ocurra primero: la decisión de Mesa sobre el reingreso o la
    resolución de la incidencia CAN. Sin cancelación tardía, o ya terminal
    (CANCELADO / RETORNADO / ENTREGADO), no hace nada."""
    if not pedido.cancelacion_tardia or Pedido.CANCELADO not in Pedido.TRANSICIONES.get(pedido.estado, ()):
        return pedido
    return pedido.transicionar(
        Pedido.CANCELADO, actor=actor,
        motivo=motivo or "Cancelación tardía cerrada: Mesa decidió el reingreso o resolvió la incidencia.",
    )


def _validar_decision_reingreso(pedido):
    fuera = pedido.estado in ESTADOS_CON_MERCANCIA_FUERA or (
        pedido.estado == Pedido.CANCELADO and pedido.cancelacion_tardia
    )
    if not fuera:
        raise ValueError(
            f"El pedido {pedido.folio} no ha salido de bodega ({pedido.get_estado_display()}): "
            "no hay reingreso que decidir."
        )
    if pedido.reingreso_estado != Pedido.REINGRESO_PENDIENTE:
        raise ValueError(
            f"El pedido {pedido.folio} ya tiene decisión: {pedido.get_reingreso_estado_display()}."
        )


def registrar_reingreso(pedido, actor, motivo=""):
    """Mesa: la mercancía de un pedido que salió está de vuelta físicamente.
    Crea la OrdenEntrada tipo reingreso ANUNCIADA con lo despachado por línea;
    el piso la recibe (ok → put-away, dañado → cuarentena) y la ubica. Una
    cancelación tardía pasa a CANCELADO; un RETORNADO se queda así."""
    from apps.inventario.models import LineaASN, OrdenEntrada  # lazy: modelo de otra app

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
        _validar_decision_reingreso(pedido)
        # Vuelve lo que salió: cantidad_despachada cuando el manifiesto la
        # estampó (desde 2026-09-22; en una cancelación mixta lo de bodega ya
        # regresó por su cuenta); en pedidos anteriores, lo pickeado o, sin
        # pick registrado, lo pedido.
        con_despacho = pedido.tiene_despachadas
        lineas = [
            (l.sku, l.cantidad_despachada if con_despacho else (l.cantidad_pickeada or l.cantidad))
            for l in pedido.lineas.select_related("sku") if not l.sku.es_kit
        ]
        lineas = [(sku, n) for sku, n in lineas if n > 0]
        if not lineas:
            raise ValueError(f"El pedido {pedido.folio} no tiene mercancía que reingresar.")
        orden = OrdenEntrada.objects.create(
            cliente=pedido.cliente, tipo=OrdenEntrada.TIPO_REINGRESO, pedido=pedido,
            fecha_compromiso=timezone.localdate(),
        )
        for sku, piezas in lineas:
            LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=piezas)
        pedido.reingreso_estado = Pedido.REINGRESADO
        pedido.save(update_fields=["reingreso_estado", "actualizado"])
        cerrar_cancelacion_tardia(pedido, actor, "Cancelación tardía: la mercancía regresa como reingreso.")
        registrar_evento(
            "asn", orden.folio, "reingreso_creado", actor=actor, cliente=pedido.cliente,
            delta={"pedido": pedido.folio, "desde": "retorno",
                   "lineas": [{"sku": s.codigo, "cantidad": n} for s, n in lineas]},
            motivo=motivo or f"Mercancía de {pedido.folio} de vuelta; el piso la recibe y ubica.",
        )
    return orden


def marcar_no_recuperado(pedido, actor, motivo=""):
    """Mesa: la mercancía de un pedido que salió no volverá. Sin movimiento de
    stock (ya salió del kardex en el manifiesto); queda el evento, se
    resuelven las incidencias CAN/RF abiertas y una cancelación tardía pasa
    a CANCELADO."""
    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
        _validar_decision_reingreso(pedido)
        pedido.reingreso_estado = Pedido.NO_RECUPERADO
        pedido.save(update_fields=["reingreso_estado", "actualizado"])
        registrar_evento(
            "pedido", pedido.pk, "inventario_no_recuperado", actor=actor, cliente=pedido.cliente,
            motivo=motivo or "Mesa dio por perdida la mercancía del pedido.",
        )
        cerrar_cancelacion_tardia(pedido, actor, "Cancelación tardía: inventario no recuperado.")
        try:
            from apps.incidencias.models import Incidencia
            from apps.incidencias.services import resolver  # lazy por contrato
        except ImportError:
            return pedido
        for incidencia in Incidencia.objects.filter(
            pedido=pedido, tipo__in=["CAN", "RF"], estado__in=Incidencia.ESTADOS_ABIERTOS,
        ):
            resolver(
                incidencia, f"Inventario no recuperado: {motivo or 'la mercancía no volvió a bodega'}.", actor,
            )
    return pedido


def _liberar_reservas(pedido):
    """Libera en inventario las líneas que sí alcanzaron reserva."""
    from apps.inventario.services import liberar_reserva  # lazy
    for linea in pedido.lineas.filter(reservada=True).select_related("sku"):
        if linea.sku.es_kit:
            continue  # reservada=True del kit es bookkeeping: no hay RESERVADO real
        liberar_reserva(linea.sku, linea.cantidad, pedido.folio)
        linea.reservada = False
        linea.save(update_fields=["reservada"])


# ── Job: entregas presuntas ──

def cerrar_entregas_presuntas(dias=None):
    """Pedidos EN_TRANSITO sin evento en N días → ENTREGA_PRESUNTA.

    Ningún pedido queda "EN TRÁNSITO para siempre" (BLUEPRINT §1.4): el
    cierre queda documentado para confirmar con el comprador. Idempotente:
    correrlo dos veces no re-cierra nada. N sale de
    settings.TORRE["ENTREGA_PRESUNTA_DIAS"] (default 7). "Evento" = lo más
    reciente entre la última actualización del pedido (Pedido.actualizado),
    su entrada a tránsito y el último movimiento de sus guías — columnas, no
    la auditoría.
    """
    if dias is None:
        dias = int(settings.TORRE.get("ENTREGA_PRESUNTA_DIAS", 7))
    limite = timezone.now() - timedelta(days=dias)
    cerrados = []
    for pedido in Pedido.objects.filter(estado=Pedido.EN_TRANSITO).prefetch_related("guias"):
        marcas = [pedido.ts_en_transito or pedido.creado, pedido.actualizado]
        marcas += [g.ts_ultimo_movimiento for g in pedido.guias.all()]
        referencia = max(m for m in marcas if m is not None)
        if referencia <= limite:
            pedido.transicionar(
                Pedido.ENTREGA_PRESUNTA,
                motivo=(
                    f"Sin evento en {dias} días: cierre presunto documentado, "
                    "confirmar entrega con el comprador."
                ),
            )
            cerrados.append(pedido)
    return cerrados
