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
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento

from .models import LineaPedido, Pedido, PickLinea

# Evidencia de empaque (C1 — fin de la paradoja de la evidencia): al empacar
# la guía AÚN NO existe, así que aquí solo se exige la foto del CONTENIDO.
# La foto de la caja cerrada (con la etiqueta ya pegada) se toma después:
# por caja en cerrar_caja() (primitiva) o, desde 2026-10-20, UNA por tanda en
# cerrar_tanda() (el wizard: etiquetas pegadas → evidencia → lista en salida).
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

# Líneas a las que les falta stock: sin reserva de lo pedido, o con piezas
# repuestas aún sin apartar (reposición esperando inventario).
SIN_STOCK = Q(reservada=False) | Q(cantidad_repuesta_reservada__lt=F("cantidad_repuesta"))


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
        .filter(SIN_STOCK, sku=sku, pedido__estado__in=_ESTADOS_REINTENTO)
        .order_by("pedido__creado", "pk")
    )
    logradas = []
    for linea in lineas:
        if not _reservar_lo_que_falte(linea):
            continue  # sin stock para esta; una posterior podría pedir menos piezas
        pedido = linea.pedido
        completo = not pedido.lineas.filter(SIN_STOCK).exists()
        registrar_evento(
            "pedido", pedido.pk, "reserva_reintentada", actor="sistema",
            cliente=pedido.cliente,
            delta={"sku": sku.codigo, "cantidad": linea.cantidad, "pedido_completo": completo},
            motivo="Entró stock y la reserva pendiente se completó sola.",
        )
        _tras_reserva(pedido, "sistema")
        if completo:
            _resolver_sku_si_completo(pedido, "sistema")
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
    pendientes = list(pedido.lineas.select_related("sku").filter(SIN_STOCK))
    if not pendientes:
        return f"{pedido.folio} ya tiene todas sus líneas reservadas."
    con_stock = [linea for linea in pendientes if _reservar_lo_que_falte(linea)]
    registrar_evento(
        "pedido", pedido.pk, "reserva_reintentada", actor=actor, cliente=pedido.cliente,
        delta={
            "logradas": [l.sku.codigo for l in con_stock],
            "sin_stock": [l.sku.codigo for l in pendientes if l not in con_stock],
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


# Desde dónde se repone: producto en la calle o ya entregado. Antes de salir
# el pedido simplemente se corrige. El candado real es por CAJA
# (piezas_reponibles): con estos estados y sin cantidad_despachada (pedidos
# anteriores al fulfillment parcial) todo el pedido cuenta como salido.
ESTADOS_REPOSICION = (
    Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGA_PRESUNTA, Pedido.ENTREGADO,
    Pedido.RETORNADO, Pedido.PARCIALMENTE_DESPACHADO,
)
# Otra ola en manos del piso: no se le meten líneas a medio picking/empaque.
ESTADOS_OLA_OCUPADA = (
    Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.CANCELACION_PENDIENTE,
)


def piezas_reponibles(pedido):
    """Qué se puede reponer del pedido, por caja que YA SALIÓ (Chema
    2026-09-30: el candado es por paquete, no por pedido). {linea_id:
    {"linea", "piezas", "cajas", "por_caja"}}: `piezas` = todo lo despachado
    de ese line item (originales y reposiciones anteriores: una caja de
    reposición extraviada también se repone), `cajas` = números de las cajas
    despachadas que lo llevaron y `por_caja` = {caja_pk: {"numero", "piezas"}}
    con las unidades de venta de ese producto en cada una (una media caja
    reempacada cuenta como la unidad entera). Lo que sigue en bodega (una
    segunda ola esperando stock) no aparece: eso se corrige, no se repone.
    Pedidos anteriores al fulfillment parcial (en la calle o entregados, sin
    cantidad_despachada): todas sus líneas cuentan como salidas."""
    import math
    from fractions import Fraction

    from apps.envios.models import Paquete, PaqueteLinea  # lazy: modelos de otra app

    legado = pedido.estado in ESTADOS_REPOSICION and not pedido.tiene_despachadas
    por_caja = {}
    for pl in (
        PaqueteLinea.objects.filter(paquete__pedido=pedido, paquete__estado=Paquete.DESPACHADO)
        .select_related("paquete")
    ):
        cajas = por_caja.setdefault(pl.linea_pedido_id, {})
        acumulado = cajas.setdefault(pl.paquete_id, {"numero": pl.paquete.numero, "unidades": Fraction(0)})
        acumulado["unidades"] += Fraction(pl.cantidad, max(pl.fraccion_de, 1))
    resultado = {}
    for linea in (
        pedido.lineas.filter(parte_de_kit__isnull=True).select_related("sku").order_by("pk")
    ):
        if getattr(linea.sku, "es_kit", False):
            continue
        piezas = linea.cantidad if legado else linea.cantidad_despachada
        if piezas > 0:
            cajas = {
                pk: {"numero": c["numero"], "piezas": math.ceil(c["unidades"])}
                for pk, c in sorted(por_caja.get(linea.pk, {}).items(), key=lambda kv: kv[1]["numero"])
            }
            resultado[linea.pk] = {
                "linea": linea, "piezas": piezas, "cajas": [c["numero"] for c in cajas.values()], "por_caja": cajas,
            }
    return resultado


def _reservar_reposicion(linea):
    """Aparta el stock de las piezas repuestas que aún no lo tienen (todas o
    nada, como la línea). Regresa False si no alcanzó."""
    from apps.inventario.services import reservar  # lazy: servicio de otra app

    faltan = linea.repuestas_sin_stock
    if faltan <= 0:
        return True
    if not reservar(linea.sku, faltan, linea.pedido.folio):
        return False
    linea.cantidad_repuesta_reservada = linea.cantidad_repuesta
    linea.save(update_fields=["cantidad_repuesta_reservada"])
    return True


def _reservar_lo_que_falte(linea):
    """La línea sin reserva reserva lo pedido; la reposición sin stock, lo
    repuesto. Regresa True si al final nada le falta."""
    if not linea.reservada and not _reservar_linea(linea):
        return False
    return _reservar_reposicion(linea)


class _Repuesta:
    """Renglón del resultado de reponer_lineas: línea, cantidad y caja origen."""

    def __init__(self, linea, cantidad, caja):
        self.linea, self.cantidad, self.caja = linea, cantidad, caja

    @property
    def sku(self):
        return self.linea.sku


def reponer_lineas(pedido, seleccion, actor, incidencia=None):
    """Reposición de producto (Chema 2026-10-05: "los line items son de la
    orden y nunca se agregan") sobre un pedido con producto en la calle o
    entregado. `seleccion` = [(línea, cantidad[, caja_origen])] de sus line
    items: cuántas piezas de ese producto llegaron mal y de qué caja salieron
    (sin caja: la única que lo llevó, o la primera). No se crea ninguna
    línea: la original suma `cantidad_repuesta` (y guarda el origen en
    `origen_reposicion`), se aparta stock para lo repuesto (sin stock queda
    esperando, como cualquier faltante) y el pedido regresa a PENDIENTE sin
    dueño con un plan de cajas solo de lo pendiente; el planeador estampa
    en cada renglón nuevo la caja original que repone (PaqueteLinea.repone_a).
    El piso lo toma con EMPEZAR y sigue picking → empaque → guía → salida; al
    entregarse la reposición el pedido vuelve a ENTREGADO. Lo que ya salió
    se estampa como despachado (pedidos anteriores al fulfillment parcial no
    lo tenían) para no planearlo dos veces. Shopify no se toca: la orden ya
    está fulfilled. Regresa [_Repuesta(linea, cantidad, caja)].

    Candado por CAJA (Chema 2026-09-30): solo se reponen piezas que ya
    salieron en una caja despachada (piezas_reponibles), aunque el pedido
    esté PENDIENTE por una segunda ola que espera stock; en ese caso lo
    repuesto se suma a esa ola (misma reserva y replaneo, sin transición ni
    estampado). Con la ola en manos del piso (picking, empaque, guía) se
    espera a que salga."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    # Una caja que ya se entregó pero cuyo registro quedó planeado/empacado (fantasma) se
    # cierra ANTES del candado: así cuenta como la caja de origen y el piso no la vuelve a pedir.
    cerrar_cajas_fantasma(pedido, actor)
    reponibles = piezas_reponibles(pedido)
    if not reponibles:
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display()} y ninguna caja ha salido: solo se repone "
            "lo que ya salió o se entregó; antes de eso se corrige el pedido."
        )
    if pedido.estado in ESTADOS_OLA_OCUPADA:
        raise ValueError(
            f"{pedido.folio} tiene otra ola en manos del piso ({pedido.get_estado_display()}): "
            "espera a que salga y luego repón."
        )
    if pedido.estado not in ESTADOS_REPOSICION + (Pedido.PENDIENTE,):
        raise ValueError(f"{pedido.folio} está {pedido.get_estado_display()}: no se puede reponer.")
    limpias = []
    for item in seleccion:
        linea, cantidad = item[0], int(item[1] or 0)
        caja = item[2] if len(item) > 2 else None
        if cantidad <= 0:
            continue
        if linea.pedido_id != pedido.pk:
            raise ValueError(f"La línea {linea.pk} no es de {pedido.folio}.")
        if linea.parte_de_kit_id or getattr(linea.sku, "es_kit", False):
            raise ValueError(f"{linea.sku.codigo}: los kits se reponen por componente; elige el producto, no el kit.")
        info = reponibles.get(linea.pk)
        if info is None:
            raise ValueError(f"{linea.sku.codigo}: no ha salido de bodega; eso se corrige en el pedido, no se repone.")
        if cantidad > info["piezas"]:
            raise ValueError(f"{linea.sku.codigo}: salieron {info['piezas']} pieza(s); no se reponen {cantidad}.")
        if caja is not None and not isinstance(caja, Paquete):
            caja = Paquete.objects.filter(pk=caja, pedido=pedido).first()
        if caja is None and info["por_caja"]:
            caja = Paquete.objects.get(pk=next(iter(info["por_caja"])))  # la única, o la primera que lo llevó
        if caja is not None:
            en_caja = info["por_caja"].get(caja.pk)
            if en_caja is None:
                raise ValueError(f"{linea.sku.codigo}: la caja {caja.numero} no llevaba ese producto.")
            if cantidad > en_caja["piezas"]:
                raise ValueError(
                    f"{linea.sku.codigo}: en la caja {caja.numero} salieron {en_caja['piezas']} pieza(s); "
                    f"no se reponen {cantidad} de esa caja (registra otra reposición para la otra caja)."
                )
        limpias.append(_Repuesta(linea, cantidad, caja))
    if not limpias:
        raise ValueError("Elige al menos un producto con piezas a reponer.")
    folio_inc = getattr(incidencia, "folio", "") or ""
    with transaction.atomic():
        if pedido.estado not in (Pedido.PARCIALMENTE_DESPACHADO, Pedido.PENDIENTE):
            # Ya salió todo lo reservado: los pedidos anteriores al fulfillment
            # parcial no traen cantidad_despachada y el plan los repetiría.
            for linea in pedido.lineas.all():
                if linea.reservada and linea.cantidad_despachada < linea.cantidad:
                    linea.cantidad_despachada = linea.cantidad
                    linea.save(update_fields=["cantidad_despachada"])
        sin_stock = []
        for r in limpias:
            linea = r.linea
            linea.cantidad_repuesta += r.cantidad
            linea.origen_reposicion = list(linea.origen_reposicion or []) + [{
                "caja": r.caja.pk if r.caja else None, "numero": r.caja.numero if r.caja else None,
                "cantidad": r.cantidad, "incidencia": folio_inc,
            }]
            linea.save(update_fields=["cantidad_repuesta", "origen_reposicion"])
            if not _reservar_reposicion(linea):
                sin_stock.append(linea)
        pedido.asignado_a = None
        pedido.transferencia_a = None
        pedido.save(update_fields=["asignado_a", "transferencia_a", "actualizado"])
        if pedido.estado != Pedido.PENDIENTE:
            pedido.transicionar(
                Pedido.PENDIENTE, actor=actor,
                motivo=f"Reposición de producto{f' ({folio_inc})' if folio_inc else ''}: regresa a picking con piezas repuestas.",
            )
        registrar_evento(
            "pedido", pedido.pk, "reposicion", actor=actor, cliente=pedido.cliente,
            delta={
                "incidencia": folio_inc,
                "lineas": [[r.sku.codigo, r.cantidad, r.caja.numero if r.caja else None] for r in limpias],
                "sin_stock": [l.sku.codigo for l in sin_stock],
            },
            motivo=f"Se reponen {sum(r.cantidad for r in limpias)} pieza(s); "
                   + ("todas con stock." if not sin_stock else f"sin stock: {', '.join(l.sku.codigo for l in sin_stock)}."),
        )
        _planificar_best_effort(pedido, force=True)
    return limpias


def deshacer_reposicion(pedido, seleccion, actor, incidencia=None):
    """Cancela una reposición aprobada (Chema 2026-10-14; PED-00261: el
    carrier entrega el paquete original con una guía nueva, así que la
    reposición sobra) mientras el piso no la haya tomado: el pedido debe
    seguir PENDIENTE y sin dueño (EMPEZAR lo pasa a EN_PICKING). `seleccion`
    = [(línea, cantidad, caja_origen pk|None)] tal como la aprobó la
    compensación. Por línea se restan las piezas repuestas, se libera la
    reserva que les tocaba (liberar_reserva) y se quita su entrada de
    `origen_reposicion`. Si ya no queda nada por surtir, las cajas planeadas
    sin guía (el plan de la reposición) se tiran y el pedido regresa al
    estado en que estaba (_estado_antes_de_reponer); con otra ola pendiente
    (otra reposición, inventario que faltaba) se queda PENDIENTE y se
    replanea. Shopify no se toca: la reposición nunca lo tocó. Las guías
    marcadas como sustituidas las revierte incidencias.cancelar_reposicion.
    Regresa el estado final del pedido."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.inventario.services import liberar_reserva  # lazy: servicio de otra app

    if pedido.estado != Pedido.PENDIENTE or pedido.asignado_a_id:
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display()}"
            + (" y en manos del piso" if pedido.asignado_a_id else "")
            + ": la reposición ya entró a picking y no se puede cancelar."
        )
    folio_inc = getattr(incidencia, "folio", "") or ""
    with transaction.atomic():
        revertidas = []
        for item in seleccion:
            linea, cantidad = item[0], int(item[1] or 0)
            caja = item[2] if len(item) > 2 else None
            caja_pk = caja.pk if isinstance(caja, Paquete) else caja
            if cantidad <= 0:
                continue
            if linea.pedido_id != pedido.pk:
                raise ValueError(f"La línea {linea.pk} no es de {pedido.folio}.")
            linea.refresh_from_db()
            if cantidad > linea.cantidad_repuesta:
                raise ValueError(
                    f"{linea.sku.codigo}: hay {linea.cantidad_repuesta} pieza(s) repuesta(s); no se cancelan {cantidad}."
                )
            linea.cantidad_repuesta -= cantidad
            sobra = linea.cantidad_repuesta_reservada - linea.cantidad_repuesta
            if sobra > 0:
                liberar_reserva(linea.sku, sobra, pedido.folio)
                linea.cantidad_repuesta_reservada = linea.cantidad_repuesta
            origen = list(linea.origen_reposicion or [])
            indice = _entrada_de_reposicion(origen, caja_pk, cantidad, folio_inc)
            if indice is not None:
                origen.pop(indice)
            linea.origen_reposicion = origen
            linea.save(update_fields=["cantidad_repuesta", "cantidad_repuesta_reservada", "origen_reposicion"])
            numero = Paquete.objects.filter(pk=caja_pk).values_list("numero", flat=True).first() if caja_pk else None
            revertidas.append((linea, cantidad, numero))
        if not revertidas:
            raise ValueError("La compensación no tiene piezas repuestas que cancelar.")
        queda = any(l.por_surtir - l.cantidad_despachada > 0 for l in pedido.lineas.all())
        destino = Pedido.PENDIENTE
        if not queda:
            destino = _estado_antes_de_reponer(pedido)
            for caja in pedido.paquetes.prefetch_related("guias"):
                if caja.estado != Paquete.DESPACHADO and not any(g.es_activa for g in caja.guias.all()):
                    caja.delete()
            if destino != Pedido.PENDIENTE:
                pedido.transicionar(
                    destino, actor=actor,
                    motivo=f"Reposición cancelada{f' ({folio_inc})' if folio_inc else ''}: el pedido sigue con lo que ya salió.",
                )
        piezas = sum(c for _, c, _ in revertidas)
        registrar_evento(
            "pedido", pedido.pk, "reposicion_cancelada", actor=actor, cliente=pedido.cliente,
            delta={
                "incidencia": folio_inc, "estado": destino,
                "lineas": [[l.sku.codigo, c, n] for l, c, n in revertidas],
            },
            motivo=f"Se cancela la reposición de {piezas} pieza(s)"
                   + ("; el pedido sigue pendiente por otra ola." if queda else f"; regresa a {dict(Pedido.ESTADOS)[destino]}."),
        )
        if queda:
            _planificar_best_effort(pedido, force=True)
    return destino


def _entrada_de_reposicion(origen, caja_pk, cantidad, folio_inc):
    """Índice de la entrada de `origen_reposicion` que corresponde a esa caja
    y cantidad: primero la de la misma incidencia; si no, cualquiera; None si
    no hay (reposición anterior al registro del origen)."""
    for exigir_incidencia in (True, False):
        for i, entrada in enumerate(origen):
            if entrada.get("caja") != caja_pk or int(entrada.get("cantidad") or 0) != cantidad:
                continue
            if exigir_incidencia and (entrada.get("incidencia") or "") != folio_inc:
                continue
            return i
    return None


def _estado_antes_de_reponer(pedido):
    """Estado al que regresa un pedido cuya reposición se cancela: el `de` del
    último cambio_estado que lo llevó a PENDIENTE (la reposición); si no está
    o no es un estado de la calle, lo que digan sus guías (entregado,
    retornado) o EN_TRANSITO."""
    from apps.core.models import EventoAuditoria  # lazy: modelo de otra app

    evento = (
        EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id__in=[str(pedido.pk), pedido.folio], accion="cambio_estado", delta__a=Pedido.PENDIENTE,
        ).order_by("-ts", "-pk").first()
    )
    anterior = (evento.delta or {}).get("de") if evento is not None else None
    if anterior in ESTADOS_REPOSICION:
        return anterior
    from apps.envios.services import _estado_por_guias  # lazy por contrato

    return _estado_por_guias(pedido) or Pedido.EN_TRANSITO


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
    cerrar_cajas_fantasma(pedido, actor)
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
    if carrier and carrier not in dict(opciones_paqueteria()):
        raise ValueError("Elige una paquetería de la lista.")
    # "" = sin forzar (Chema 2026-09-30): se quita el forzado y se replanea con
    # las reglas y preferencias normales del cliente (reparto, reglas de
    # envío, integración).
    etiqueta = etiqueta_paqueteria(carrier) if carrier else "las reglas normales del pedido"
    if pedido.estado not in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display().lower()}: la paquetería solo se cambia antes de salir."
        )
    # Segunda ola o reposición (2026-09-28): lo que ya salió (cajas
    # despachadas o con guía viva) es fijo; solo se replanea lo de ESTA ola.
    if not pedido.lineas_por_surtir and pedido.estado != Pedido.GUIA_GENERADA:
        raise ValueError(f"{pedido.folio} ya salió entero: no hay nada que replanear.")
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
                fijas = {
                    c.pk for c in fresco.paquetes.all()
                    if c.estado == Paquete.DESPACHADO or any(g.es_activa for g in c.guias.all())
                }
                vivas = [c for c in fresco.paquetes.all() if c.pk not in fijas]
                if fresco.estado == Pedido.GUIA_GENERADA:
                    regresar_a_empaque(fresco, actor, motivo=f"Cambio de paquetería a {etiqueta}")
                    modo = "recotizadas"
                elif any(c.estado == Paquete.EMPACADO for c in vivas):
                    for caja in vivas:
                        recotizar_paquete(fresco, caja)  # la caja física se queda; solo cambia carrier y precio
                    modo = "recotizadas"
                else:
                    planificar_envio(fresco, force=True)
                    modo = "replaneadas"
                cajas = [c for c in fresco.paquetes.all().order_by("numero") if c.pk not in fijas]
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
    guias = []
    if carrier == CARRIER_LOCAL and modo == "recotizadas" and cajas and all(c.estado == Paquete.EMPACADO for c in cajas):
        # Sin guía de carrier no hay nada que comprar (Chema 2026-10-07,
        # PED-00317/00319): la guía interna de cada caja empacada sale aquí
        # mismo y el pedido pasa a GUIA_GENERADA → Salida, en vez de quedarse
        # "sin guía" en la mesa hasta que alguien reintente.
        from apps.envios.services import generar_guias  # lazy por contrato

        guias = generar_guias(fresco)
        fresco.refresh_from_db(fields=["estado"])
    pedido.carrier_forzado = fresco.carrier_forzado
    pedido.estado = fresco.estado
    pedido.asignado_a = fresco.asignado_a
    return {"modo": modo, "cajas": cajas, "guias": guias}


def replanear_cajas(pedido, actor):
    """Mesa rehace el plan de cajas de la ola en bodega (Chema 2026-09-30,
    botón "Replanear cajas"; 2026-10-19, PED-00606): reparte de nuevo las
    cajas PLANEADAS sin guía y planea lo que NINGUNA caja cubre (líneas que
    entraron por edición después del plan). Las despachadas, las que tienen
    guía viva y las ya empacadas (físicas) son fijas y no se tocan. Si el
    pedido estaba en GUIA_GENERADA y nacieron cajas nuevas, regresa a EMPACADO
    sin dueño: cae a "Completar empaquetado" en Mi turno, el wizard pide
    pesar y fotografiar las nuevas y su guía se compra al terminar; las guías
    de las otras cajas no cambian. Usa las reglas vigentes (paquetería
    forzada incluida). ValueError sin nada que replanear. Regresa las cajas
    nuevas."""
    from apps.envios.cotizador import planificar_envio, unidades_sin_caja  # lazy por contrato
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    if pedido.estado not in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display().lower()}: el plan solo se rehace antes de salir."
        )
    cajas = list(pedido.paquetes.prefetch_related("guias"))
    vivas = [c for c in cajas if c.estado == Paquete.PLANEADO and not any(g.es_activa for g in c.guias.all())]
    sin_caja = unidades_sin_caja(pedido)
    if not vivas and not sin_caja:
        raise ValueError(
            f"{pedido.folio} no tiene nada que replanear: todo lo pendiente ya va en una caja empacada o con guía."
        )
    if not pedido.lineas_por_surtir:
        raise ValueError(f"{pedido.folio} no tiene nada en bodega que planear.")
    antes = [(c.numero, c.carrier, float(c.precio_cotizado or 0)) for c in vivas]
    with transaction.atomic():
        try:
            resultado = planificar_envio(pedido, force=True)
        except ValueError as exc:
            raise ValueError(f"Ningún carrier cotiza el plan de {pedido.folio} (CP {pedido.cp}): {exc}") from exc
        fijas = {c.pk for c in cajas if c not in vivas}
        nuevas = [c for c in resultado if c.pk not in fijas]
        if nuevas and pedido.estado == Pedido.GUIA_GENERADA:
            pedido.asignado_a = None
            pedido.transferencia_a = None
            pedido.save(update_fields=["asignado_a", "transferencia_a", "actualizado"])
            pedido.transicionar(
                Pedido.EMPACADO, actor=actor,
                motivo="Replaneo con cajas nuevas por empacar; las guías de las demás cajas no cambian.",
            )
        registrar_evento(
            "pedido", pedido.pk, "replaneo_cajas", actor=actor, cliente=pedido.cliente,
            delta={
                "antes": antes, "ahora": [(c.numero, c.carrier, float(c.precio_cotizado or 0)) for c in nuevas],
                "sin_caja": sorted({u[0].sku.codigo for u in sin_caja}),
            },
            motivo=f"Mesa rehizo el plan: {len(antes)} caja(s) → {len(nuevas)}"
                   + (f"; {len(sin_caja)} unidad(es) sin caja planeadas." if sin_caja else "."),
        )
    return nuevas


def cambiar_paqueteria_caja(pedido, caja, carrier, actor):
    """Mesa cambia la paquetería de UNA caja que aún no sale (Chema
    2026-09-30: "cambiar paquetería debería de ser por paquete"). `carrier`
    = una opción de opciones_paqueteria (incluida "local": sale sin guía de
    carrier), "" = quitar el forzado de la caja (vuelve a seguir al pedido) o
    None = conservar lo que tenga y solo cancelar su guía y recotizar.
    Orden: 1) se fija Paquete.carrier_forzado y se recotiza SOLO esa caja
    (envios._replan_paquete honra el forzado de la caja); si no cotiza, nada
    cambia (ValueError); 2) con guía comprada (GUIA_CREADA) se cancela con el
    carrier y se borra el cierre de la caja (etiqueta nueva); 3) si el pedido
    estaba GUIA_GENERADA regresa a EMPACADO sin dueño para que el piso
    recompre esa guía. Las demás cajas no se tocan. Regresa la caja."""
    from apps.envios.adapters import ErrorCarrier  # lazy por contrato
    from apps.envios.models import Guia, Paquete  # lazy: modelo de otra app
    from apps.envios.services import (  # lazy por contrato
        _replan_paquete, cancelar_guia, etiqueta_paqueteria, opciones_paqueteria,
    )

    carrier = carrier.strip() if isinstance(carrier, str) else None
    if carrier and carrier not in dict(opciones_paqueteria()):
        raise ValueError("Elige una paquetería de la lista.")
    if caja.pedido_id != pedido.pk:
        raise ValueError(f"La caja {caja.numero} no es de {pedido.folio}.")
    # PARCIALMENTE_DESPACHADO (2026-10-01): una caja salió y otra se quedó sin
    # paquetería; la que se quedó sí se puede cambiar (la despachada no).
    if pedido.estado not in (
        Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO,
    ):
        raise ValueError(
            f"{pedido.folio} está {pedido.get_estado_display().lower()}: la paquetería solo se cambia antes de salir."
        )
    if caja.estado == Paquete.DESPACHADO:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} ya salió: no se cambia su paquetería.")
    guia = caja.guia_activa
    if guia is not None and guia.estado != Guia.GUIA_CREADA:
        raise ValueError(f"La guía {guia.numero} de la caja {caja.numero} ya salió ({guia.get_estado_display().lower()}).")
    etiqueta = etiqueta_paqueteria(carrier) if carrier else "las reglas del pedido"
    quitar_forzado = carrier == ""
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        caja = Paquete.objects.select_for_update().get(pk=caja.pk)
        anterior_forzado, anterior_carrier = caja.carrier_forzado, caja.carrier
        if carrier or quitar_forzado:
            caja.carrier_forzado = carrier or ""
            caja.save(update_fields=["carrier_forzado"])
        try:
            with transaction.atomic():  # savepoint: si no cotiza, el plan de la caja queda intacto
                _replan_paquete(fresco, caja, anterior_carrier)
        except (ErrorCarrier, ValueError) as exc:
            caja.carrier_forzado = anterior_forzado
            caja.save(update_fields=["carrier_forzado"])
            registrar_evento(
                "paquete", caja.pk, "cambio_paqueteria_caja_fallido", actor=actor, cliente=fresco.cliente,
                delta={"pedido": fresco.folio, "caja": caja.numero, "carrier": carrier}, motivo=str(exc)[:300],
            )
            raise ValueError(f"{etiqueta} no cotiza la caja {caja.numero} de {fresco.folio} (CP {fresco.cp}): {exc}") from exc
        cancelada = ""
        if guia is not None:
            cancelar_guia(guia, actor, motivo=f"Cambio de paquetería de la caja {caja.numero} a {etiqueta}")
            cancelada = guia.numero
            _reabrir_cierre(caja)
        if fresco.estado == Pedido.GUIA_GENERADA:
            fresco.asignado_a = None
            fresco.save(update_fields=["asignado_a", "actualizado"])
            fresco.transicionar(
                Pedido.EMPACADO, actor=actor,
                motivo=f"Caja {caja.numero} cambia a {etiqueta}: guía cancelada, el piso la recompra."[:300],
            )
        caja.refresh_from_db()
        registrar_evento(
            "paquete", caja.pk, "cambio_paqueteria_caja", actor=actor, cliente=fresco.cliente,
            delta={"pedido": fresco.folio, "caja": caja.numero, "antes": anterior_carrier, "ahora": caja.carrier,
                   "forzado": caja.carrier_forzado, "precio": float(caja.precio_cotizado or 0), "guia_cancelada": cancelada},
            motivo=f"Mesa: caja {caja.numero} de {fresco.folio} con {etiqueta} (${caja.precio_cotizado or 0})."[:300],
        )
        if caja.carrier == CARRIER_LOCAL and caja.estado == Paquete.EMPACADO and caja.guia_activa is None:
            # Sin guía de carrier no hay nada que comprar (Chema 2026-10-07):
            # la guía interna de la caja sale aquí mismo; si con ella ya no
            # falta guía en ninguna caja en bodega, el pedido pasa a
            # GUIA_GENERADA (el cierre pendiente lo pide el wizard igual).
            from apps.envios.services import SERVICIO_LOCAL, _crear_guia  # lazy por contrato

            _crear_guia(fresco, CARRIER_LOCAL, SERVICIO_LOCAL, paquete=caja)
            en_bodega = [c for c in fresco.paquetes.all() if c.estado != Paquete.DESPACHADO]
            if fresco.estado == Pedido.EMPACADO and all(c.guia_activa is not None for c in en_bodega):
                fresco.transicionar(Pedido.GUIA_GENERADA, actor=actor, motivo=f"Guía interna de la caja {caja.numero}: sin guía de carrier.")
    pedido.estado = fresco.estado
    pedido.asignado_a = fresco.asignado_a
    return caja


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


def _reembolsos_por_sku(payload):
    """{sku: piezas} reembolsadas según `refunds[].refund_line_items[]` del
    payload de Shopify (el line item viene anidado con su sku)."""
    total = {}
    for refund in payload.get("refunds") or []:
        for item in refund.get("refund_line_items") or []:
            sku = str((item.get("line_item") or {}).get("sku") or "").strip()
            try:
                piezas = int(item.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if sku and piezas > 0:
                total[sku] = total.get(sku, 0) + piezas
    return total


def _aplicar_reembolsos(pedido, payload, origen):
    """Reembolsos de Shopify (Chema 2026-10-12, #33909): cada pieza reembolsada
    cancela una caja que salió y NO se entregó (regresada primero, luego en
    tránsito, intento fallido…) cuando el reembolso cubre todo su contenido;
    lo entregado con daño no se registra (se revisa caso por caso). La caja
    sigue DESPACHADO y gana `cancelada_por_reembolso`; evento
    `reembolso_aplicado`; una caja cancelada que sigue en tránsito abre la
    incidencia interna DEV "Pedir regreso al origen" (con su correo); y si las
    cajas no canceladas ya están entregadas, el pedido pasa a ENTREGADO.
    Idempotente: las cajas ya canceladas descuentan del reembolso. Regresa
    {sku: piezas} para que la edición de cantidades no lo tome por conflicto."""
    reembolsos = _reembolsos_por_sku(payload)
    if not reembolsos:
        return reembolsos
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app

    cajas = list(
        pedido.paquetes.filter(estado=Paquete.DESPACHADO)
        .prefetch_related("guias", "lineas__linea_pedido__sku").order_by("numero")
    )

    def unidades(caja):
        u = {}
        for pl in caja.lineas.all():
            codigo = pl.linea_pedido.sku.codigo
            u[codigo] = u.get(codigo, 0) + pl.cantidad
        return u

    restantes = dict(reembolsos)
    for caja in cajas:
        if caja.cancelada_por_reembolso:
            for sku, n in unidades(caja).items():
                restantes[sku] = restantes.get(sku, 0) - n
    candidatas = [
        c for c in cajas
        if not c.cancelada_por_reembolso and not any(g.estado == Guia.ENTREGADO for g in c.guias.all())
    ]
    candidatas.sort(key=lambda c: (0 if any(g.estado == Guia.RETORNO for g in c.guias.all()) else 1, c.numero))
    ahora = timezone.now()
    canceladas = []
    for caja in candidatas:
        u = unidades(caja)
        if u and all(restantes.get(sku, 0) >= n for sku, n in u.items()):
            for sku, n in u.items():
                restantes[sku] -= n
            caja.cancelada_por_reembolso = ahora
            caja.save(update_fields=["cancelada_por_reembolso"])
            canceladas.append(caja)
    if not canceladas:
        return reembolsos
    registrar_evento(
        "pedido", pedido.pk, "reembolso_aplicado", actor=origen, cliente=pedido.cliente,
        delta={"reembolsos": reembolsos, "cajas_canceladas": [c.numero for c in canceladas]},
        motivo="Reembolso en Shopify: las cajas que salieron y no se entregaron quedan canceladas; "
               "lo entregado no cambia.",
    )
    for caja in canceladas:
        guia = caja.guia_activa
        if guia is not None and guia.estado not in (Guia.RETORNO, Guia.CANCELADA, Guia.ENTREGADO):
            try:
                from apps.incidencias.models import Incidencia  # lazy: modelo de otra app
                from apps.incidencias.services import abrir_incidencia  # lazy por contrato
                abrir_incidencia(
                    pedido.cliente, Incidencia.TIPO_DEV, Incidencia.ORIGEN_AUTO, pedido=pedido, interna=True, pese_a_pausa=True,
                    texto=(f"La caja {caja.numero} de {pedido.folio} se reembolsó en Shopify y sigue en tránsito "
                           f"(guía {guia.numero}, {guia.carrier}): pídele a la paquetería que la regrese al origen "
                           "antes de que la entregue. Si la entrega, el comprador se la queda."),
                )
            except Exception:  # noqa: BLE001 — el aviso jamás detiene la ingesta
                pass
    try:
        from apps.envios.services import _estado_por_guias, _transicionar_pedido  # lazy por contrato
        pedido.refresh_from_db(fields=["estado"])
        if pedido.estado in (Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGA_PRESUNTA) and _estado_por_guias(pedido) == "ENTREGADO":
            _transicionar_pedido(pedido, "ENTREGADO", motivo="Reembolso: las cajas no entregadas quedaron canceladas; el resto ya se entregó.")
    except Exception:  # noqa: BLE001 — la re-evaluación jamás detiene la ingesta
        pass
    return reembolsos


def _aplicar_cambios_cantidades(pedido, payload, origen, reembolsos=None):
    """Refund parcial / edición de orden: ajusta las líneas al current_quantity.

    Reducción sin avance físico → encoge y libera reserva; con avance
    (pickeada, empacado o después) → incidencia CAN, cantidades intactas.
    Aumento o línea nueva en PENDIENTE → camino de ingesta (reserva o FAL);
    en estados posteriores → incidencia. Diff-based: repetir el mismo update
    es no-op. `reembolsos` ({sku: piezas}, 2026-10-12): la baja explicada por
    piezas reembolsadas que ya habían salido no es conflicto.
    """
    reembolsos = reembolsos or {}
    if pedido.estado in _ESTADOS_EDICION_TERMINAL:
        return
    desconocidos = items_no_registrados(pedido, payload)
    if desconocidos and pedido.estado in (Pedido.PENDIENTE, Pedido.EN_PICKING) and not pedido.detenido:
        detener_por_producto_no_registrado(pedido, desconocidos)
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
        explicado = min(reembolsos.get(codigo, 0), sum(l.cantidad_despachada for l in filas))
        delta = piezas_objetivo + explicado - sum(l.cantidad for l in filas)
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
    if pedido.estado == Pedido.PENDIENTE and (reducidas or aumentadas):
        # Chema 2026-10-19 (PED-00606): el plan de cajas se hizo con las
        # líneas de la ingesta; lo que entra o sale después, aún sin nada
        # físico, se replanea. De picking en adelante la edición no agrega.
        _planificar_best_effort(pedido, force=True)


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


def items_no_registrados(pedido, payload):
    """Line items vivos (current_quantity > 0, o quantity sin la llave) de la
    orden cuyo producto Torre no conoce para ese cliente: sin `sku` o con un
    código que no existe. Lo que la incidencia "Producto no registrado"
    reporta y lo que «Volver a leer la orden» vuelve a evaluar."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    codigos = set(SKU.objects.filter(cliente=pedido.cliente).values_list("codigo", flat=True))
    desconocidos = []
    for item in payload.get("line_items") or []:
        try:
            cantidad = int(item.get("current_quantity") if "current_quantity" in item else item.get("quantity") or 0)
        except (TypeError, ValueError):
            cantidad = 0
        if cantidad <= 0:
            continue
        codigo = str(item.get("sku") or "").strip()
        if not codigo or codigo not in codigos:
            desconocidos.append(_item_desconocido(item, cantidad))
    return desconocidos


def detener_por_producto_no_registrado(pedido, desconocidos, usuario=None):
    """Un producto de la orden no existe en Torre (Chema 2026-10-07): el pedido
    se detiene (fuera de la cola) con la incidencia interna SKU "Producto no
    registrado" con links a Shopify; correo solo a la lista fija. Nada de SKU
    provisional: se da de alta el producto, se recibe, y Mesa «vuelve a leer
    la orden». Regresa la incidencia (agrupada si ya había)."""
    from apps.incidencias.services import texto_productos_no_registrados  # lazy por contrato

    texto = texto_productos_no_registrados(pedido.tienda, desconocidos)
    motivo = "Producto no registrado en Torre: " + ", ".join(d["titulo"] for d in desconocidos)
    return detener_pedido(pedido, usuario, motivo[:300], origen="auto" if usuario is None else "manual", tipo="SKU", texto=texto)


def _resolver_sku_si_completo(pedido, actor):
    """Tras una reserva o una re-lectura: si el pedido estaba detenido por
    producto no registrado y ya todas sus líneas existen y reservaron, la
    incidencia SKU se resuelve y el pedido vuelve a la cola."""
    if not pedido.detenido or pedido.lineas.filter(SIN_STOCK).exists():
        return None
    from apps.incidencias.services import resolver_producto_no_registrado  # lazy por contrato

    return resolver_producto_no_registrado(pedido, actor)


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
    reembolsos = {} if cancelada else _aplicar_reembolsos(pedido, payload, origen)
    if not cancelada:
        # Una orden cancelada la gobierna su matriz; lo demás ajusta cantidades.
        _aplicar_cambios_cantidades(pedido, payload, origen, reembolsos)
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


def _item_desconocido(item, cantidad):
    """Datos del line item cuyo producto Torre no conoce (para la incidencia SKU)."""
    return {
        "titulo": str(item.get("title") or "?"), "cantidad": cantidad, "sku": str(item.get("sku") or "").strip(),
        "variant_id": str(item.get("variant_id") or ""), "product_id": str(item.get("product_id") or ""),
    }


def _agregar_linea_de_item(pedido, item, cantidad, cancelada, faltantes, desconocidos=None):
    """Crea la línea del item (si su SKU existe) y reserva. Regresa (peso_gr, valor).
    Un producto que Torre no conoce va a `desconocidos` (incidencia interna
    "Producto no registrado", el pedido nace detenido; Chema 2026-10-07), no
    a `faltantes` (FAL = sin stock de un producto conocido)."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    codigo = str(item.get("sku") or "").strip()
    sku = SKU.objects.filter(cliente=pedido.cliente, codigo=codigo).first() if codigo else None
    if sku is None:
        if desconocidos is not None:
            desconocidos.append(_item_desconocido(item, cantidad))
        else:
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

    faltantes, desconocidos = [], []
    peso_esperado = 0
    valor_nuestro = Decimal("0")
    for item in payload.get("line_items") or []:
        cantidad = _cantidad_de_item(item, ticket)
        if not cantidad or cantidad <= 0:
            continue  # removida por refund/edición, o línea de otro ticket
        peso, valor = _agregar_linea_de_item(pedido, item, cantidad, cancelada, faltantes, desconocidos)
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
            "no_registrados": [d["titulo"] for d in desconocidos],
            "es_local": pedido.es_local,
            "parcial": pedido.parcial_de_orden,
            "fos": ticket["fos"] if ticket else [],
        },
        motivo=f"Orden {payload.get('name') or shopify_order_id} ingerida vía {origen}.",
    )

    if faltantes and not cancelada:
        _abrir_incidencia_faltante(pedido, faltantes)
    if desconocidos and not cancelada:
        detener_por_producto_no_registrado(pedido, desconocidos)

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
    _completar_plan_si_falta(pedido, actor)
    return pedido


def _completar_plan_si_falta(pedido, actor):
    """Red al iniciar picking (Chema 2026-10-19, PED-00606): si alguna unidad
    de la ola no tiene caja (cotizador.unidades_sin_caja: líneas que entraron
    después del plan), se replanea el plan vivo antes de que el piso lo use;
    aún no hay nada físico, así que con force. Best-effort en on_commit como
    la ingesta; queda el evento `plan_incompleto`. True si faltaba algo."""
    try:
        from apps.envios.cotizador import unidades_sin_caja  # lazy por contrato
    except ImportError:
        return False
    sin_caja = unidades_sin_caja(pedido)
    if not sin_caja:
        return False
    registrar_evento(
        "pedido", pedido.pk, "plan_incompleto", actor=actor, cliente=pedido.cliente,
        delta={"sin_caja": sorted({u[0].sku.codigo for u in sin_caja}), "unidades": len(sin_caja)},
        motivo=f"{len(sin_caja)} unidad(es) sin caja al iniciar picking: se completa el plan.",
    )
    _planificar_best_effort(pedido, force=True)
    return True


def cerrar_cajas_fantasma(pedido, actor):
    """Cajas del pedido cuyo registro sigue en PLANEADO/EN_EMPAQUE/EMPACADO
    aunque su guía ya salió o se entregó (empaque entero anterior al
    2026-10-15; PED-00341): al reabrir el pedido (reposición, segunda ola) se
    cierran como DESPACHADO con evento `caja_fantasma_cerrada`, sin tocar
    kardex ni guías (eso ya pasó cuando salió el pedido). Regresa las cerradas."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    cerradas = []
    for caja in pedido.paquetes.exclude(estado=Paquete.DESPACHADO).prefetch_related("guias").order_by("numero"):
        if not caja.ya_salio:
            continue
        guia = next(g for g in caja.guias.all() if g.es_activa)
        motivo = f"Cierre de registro: la caja salió con su guía {guia.numero} ({guia.estado}); el registro seguía en {caja.estado}."
        if caja.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE):
            caja.transicionar(Paquete.EMPACADO, actor=actor, motivo=motivo[:300])
        caja.transicionar(Paquete.DESPACHADO, actor=actor, motivo=motivo[:300])
        registrar_evento(
            "pedido", pedido.pk, "caja_fantasma_cerrada", actor=actor, cliente=pedido.cliente,
            delta={"caja": caja.numero, "guia": guia.numero, "estado_guia": guia.estado},
            motivo=motivo[:300],
        )
        cerradas.append(caja)
    return cerradas


def asegurar_plan_para_empaque(pedido, actor):
    """Candado del wizard (Chema 2026-10-20): sin plan de cajas NO se empaca.
    Red de seguridad: en el flujo normal el plan nace al ingresar o en
    EMPEZAR (_completar_plan_si_falta); si aun así un pedido llega a empaque
    sin cajas, se planea una vez más aquí (síncrono) y, si nadie cotiza, nace
    la incidencia "Sin paquetería que cotice" que lo detiene
    (incidencias.abrir_sin_paqueteria) ANTES de pesar nada. Regresa True si
    hay plan; False si el pedido quedó detenido (evento `empaque_sin_plan`)."""
    if pedido.paquetes.exists():
        return True
    try:
        from apps.envios.cotizador import planificar_envio  # lazy por contrato
        planificar_envio(pedido)
    except ValueError as exc:
        try:
            from apps.incidencias.services import abrir_sin_paqueteria  # lazy por contrato
            abrir_sin_paqueteria(pedido, str(exc))
        except Exception:  # noqa: BLE001 — el aviso jamás tapa el candado
            registrar_evento("pedido", pedido.pk, "sin_paqueteria", cliente=pedido.cliente, motivo=str(exc)[:300])
        registrar_evento(
            "pedido", pedido.pk, "empaque_sin_plan", actor=actor, cliente=pedido.cliente,
            motivo=f"Llegó a empaque sin plan de cajas y nadie cotiza: {exc}"[:300],
        )
        pedido.refresh_from_db(fields=["detenido", "asignado_a"])
        return False
    return pedido.paquetes.exists()


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


# Mesa quita el pedido de las manos de un operador que no está (Chema 2026-10-15).
ESTADOS_EN_BODEGA = (
    Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO,
)


def desasignar_operador(pedido, actor, motivo=""):
    """Mesa desasigna al operador de un pedido en bodega (Chema 2026-10-15:
    PED-00444 lo tenía alguien que no vino): queda sin dueño ni transferencia
    pendiente, con su estado y su avance intactos, y vuelve a la cola de Mi
    turno para quien le toque. Si estaba en picking y lo toma OTRA persona,
    el avance se reinicia al reclamarlo (reiniciar_picking), como siempre.
    Evento `pedido_soltado` con por="mesa". ValueError si ya no está en el
    piso o no tiene a nadie asignado."""
    if pedido.estado not in ESTADOS_EN_BODEGA:
        raise ValueError(f"{pedido.folio} está {pedido.get_estado_display()}: ya no está en el piso.")
    if pedido.asignado_a_id is None and pedido.transferencia_a_id is None:
        raise ValueError(f"{pedido.folio} no tiene operador asignado.")
    anterior = pedido.asignado_a
    nombre = _nombre_usuario(anterior) if anterior else _nombre_usuario(pedido.transferencia_a)
    pedido.asignado_a = None
    pedido.transferencia_a = None
    pedido.save(update_fields=["asignado_a", "transferencia_a", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "pedido_soltado", actor=actor, cliente=pedido.cliente,
        delta={"de": nombre, "motivo": motivo, "por": "mesa"},
        motivo=f"Mesa desasignó a {nombre}; vuelve a la cola de Mi turno y lo toma quien le toque."[:300],
    )
    return pedido


MOTIVOS_DETENER = [
    ("No encuentro el producto", "No encuentro el producto"),
    ("Producto dañado", "Producto dañado"),
    ("No cabe / falta insumo de empaque", "No cabe / falta insumo de empaque"),
    ("Otro", "Otro"),
]


def detener_pedido(pedido, usuario, motivo, origen="manual", tipo="DET", texto=None):
    """Detenido en piso (Chema 2026-10-06): el pedido sale de la cola de Mi
    turno (detenido=True, sin dueño, con su avance), queda la incidencia
    interna DET con el motivo (una por pedido: un segundo motivo se suma al
    caso) y Mesa se entera por push y correo. `usuario` = quien lo detiene
    (su dueño o Mesa); None cuando Torre lo detiene sola (origen "auto":
    guía fallida con el carrier). Mesa lo reanuda al resolver la incidencia.
    Sustituye a "Soltar pedido": un bloqueo ya no es invisible para Mesa.
    `tipo`/`texto`: la incidencia interna que lo detiene (DET por default;
    "SKU" con su texto para un producto no registrado, 2026-10-07)."""
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Di por qué se detiene el pedido.")
    if usuario is not None:
        es_mesa = getattr(getattr(usuario, "perfil", None), "rol", "") == "mesa" or getattr(usuario, "is_superuser", False)
        if pedido.asignado_a_id not in (None, usuario.pk) and not es_mesa:
            raise ValueError(f"{pedido.folio} no es tuyo: solo su dueño (o Mesa) puede detenerlo.")
    if pedido.estado not in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
        raise ValueError(f"{pedido.folio} está {pedido.get_estado_display()}: ya no está en el piso.")
    with transaction.atomic():
        anterior = pedido.asignado_a
        pedido.detenido = True
        pedido.asignado_a = None
        pedido.transferencia_a = None
        pedido.save(update_fields=["detenido", "asignado_a", "transferencia_a", "actualizado"])
        if anterior is not None:
            registrar_evento(
                "pedido", pedido.pk, "pedido_soltado", actor=usuario, cliente=pedido.cliente,
                delta={"de": _nombre_usuario(anterior), "motivo": motivo},
                motivo=f"Detenido: {motivo}"[:300],
            )
        registrar_evento(
            "pedido", pedido.pk, "pedido_detenido", actor=usuario, cliente=pedido.cliente,
            delta={"origen": origen, "estado": pedido.estado, "de": _nombre_usuario(anterior) if anterior else None},
            motivo=motivo[:300],
        )
        incidencia = None
        try:
            from apps.incidencias.models import Incidencia  # lazy: modelo de otra app
            from apps.incidencias.services import abrir_incidencia  # lazy por contrato
        except ImportError:
            pass
        else:
            quien = f" ({_nombre_usuario(usuario)})" if usuario is not None else ""
            incidencia = abrir_incidencia(
                pedido.cliente, tipo,
                Incidencia.ORIGEN_AUTO if origen == "auto" else Incidencia.ORIGEN_MANUAL,
                pedido=pedido, texto=texto or f"Detenido en piso{quien}: {motivo}", interna=True,
            )
    return incidencia


def contenido_que_regresa(pedido, caja):
    """Qué regresaría a bodega si la caja se desarma al parar el pedido:
    [(linea, sku, piezas, ubicacion|None, lote|None)] según lo anotado al
    escanear (PickLinea en empaque), o el rack fijo del producto si no hay
    registro. Solo cajas pickeadas y empacadas (en empaque o empacadas)."""
    from apps.catalogo.services import rack_de  # lazy por contrato

    filas = []
    for linea, sku, unidades in contenido_caja_por_pieza(caja):
        picks = list(linea.picks.filter(estado=PickLinea.EN_EMPAQUE).select_related("ubicacion", "lote").order_by("pk"))
        restante = unidades
        for p in picks:
            if restante <= 0:
                break
            if p.ubicacion_id is None:
                continue
            tomo = min(p.cantidad, restante)
            filas.append((linea, sku, tomo, p.ubicacion, p.lote))
            restante -= tomo
        if restante > 0:
            rack = rack_de(sku)
            lote = next((p.lote for p in picks if p.lote_id), None)
            filas.append((linea, sku, restante, rack, lote))
    return filas


def _desarmar_caja_al_parar(pedido, caja, actor, motivo):
    """La caja se desarma al parar el pedido (Chema 2026-10-21): su contenido
    vuelve a bodega a la posición y lote de donde se tomó (lo anotado al
    escanear o el rack fijo), vuelve a quedar apartado para el pedido, la
    caja regresa a PLANEADO y los contadores de la línea bajan: al reanudar,
    el piso la vuelve a pickear y empacar. ValueError si alguna pieza no
    tiene posición a dónde volver."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.inventario.services import regresar_piezas_de_empaque, reservar  # lazy por contrato

    if caja.estado not in (Paquete.EN_EMPAQUE, Paquete.EMPACADO):
        raise ValueError(f"La caja {caja.numero} no está empacada: no hay nada que desarmar.")
    filas = contenido_que_regresa(pedido, caja)
    sin_posicion = [sku.codigo for _, sku, _, ubicacion, _ in filas if ubicacion is None]
    if sin_posicion:
        raise ValueError(
            f"La caja {caja.numero}: {', '.join(sorted(set(sin_posicion)))} no tiene posición a dónde volver "
            "(sin registro de picking ni rack fijo). Decide su destino desde Reingresos."
        )
    regresado = []
    por_linea = {}
    for linea, sku, piezas, ubicacion, lote in filas:
        if sku.requiere_lote and lote is None:
            raise ValueError(f"La caja {caja.numero}: {sku.codigo} lleva lote y no hay registro de cuál. Decide su destino desde Reingresos.")
        por_linea.setdefault(linea.pk, [linea, sku, []])[2].append({"cantidad": piezas, "destino": "bodega", "ubicacion": ubicacion, "lote": lote})
    for linea, sku, decisiones in por_linea.values():
        picks = list(linea.picks.filter(estado=PickLinea.EN_EMPAQUE).order_by("pk"))
        preferir = [(p.ubicacion_id, p.lote_id, p.cantidad) for p in picks if p.ubicacion_id is not None or p.lote_id is not None]
        regresar_piezas_de_empaque(sku, decisiones, pedido.folio, actor, preferir=preferir)
        total = sum(d["cantidad"] for d in decisiones)
        _mover_registros(linea, picks, total, PickLinea.REGRESADO)
        # Vuelve a quedar apartado para este pedido: al reanudar se pickea otra vez.
        reservar(sku, total, pedido.folio)
        regresado.append({"sku": sku.codigo, "piezas": total, "a": [[d["ubicacion"].codigo, d["lote"].codigo if d["lote"] else None, d["cantidad"]] for d in decisiones]})
    caja.transicionar(Paquete.PLANEADO, actor=actor, motivo=f"Parar en piso: la caja {caja.numero} se desarma; sus piezas vuelven a bodega.")
    for pl in caja.lineas.select_related("linea_pedido__sku"):
        if not pl.linea_pedido.sku.es_kit:
            _recalcular_contadores(pl.linea_pedido)
    return regresado


def parar_en_piso(pedido, actor, motivo, *, cajas_guia=(), cajas_contenido=()):
    """"Parar en piso" desde Mesa (Chema 2026-10-20/21): el pedido sale de la
    cola del piso como un DET del operador (detener_pedido con origen mesa).
    Antes, por caja seleccionada: `cajas_guia` = cajas en bodega con guía
    comprada a las que se les cancela la guía y se les quitan las
    confirmaciones del empaque (ni el piso ni la paquetería pueden escanearla;
    al reanudar el wizard la lleva otra vez por etiquetas, evidencia y lista en
    salida); `cajas_contenido` = cajas que se desarman: su contenido vuelve a
    bodega a su posición y lote (_desarmar_caja_al_parar). Sin cajas, solo se
    detiene. Con el pedido con guía y alguna caja desarmada o sin guía, vuelve
    a EMPACADO; con una caja desarmada vuelve a picking (EN_PICKING o
    PENDIENTE si algo ya salió). Regresa la incidencia DET."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app
    from apps.envios.services import cancelar_guia  # lazy por contrato

    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Di por qué se para el pedido.")
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        if fresco.estado not in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
            raise ValueError(f"{fresco.folio} está {fresco.get_estado_display()}: ya no está en el piso.")
        if fresco.detenido:
            raise ValueError(f"{fresco.folio} ya está detenido.")
        pks_guia = {int(x) for x in cajas_guia}
        pks_contenido = {int(x) for x in cajas_contenido}
        cajas = {c.pk: c for c in Paquete.objects.select_for_update().filter(pedido=fresco, pk__in=pks_guia | pks_contenido).prefetch_related("guias")}
        faltan = (pks_guia | pks_contenido) - set(cajas)
        if faltan:
            raise ValueError("Alguna caja seleccionada no es de este pedido.")
        canceladas, desarmadas = [], []
        for pk, caja in cajas.items():
            if caja.estado == Paquete.DESPACHADO:
                raise ValueError(f"La caja {caja.numero} ya salió: no se puede parar.")
            if pk in pks_guia or pk in pks_contenido:
                guia = caja.guia_activa
                if guia is not None and guia.estado == Guia.GUIA_CREADA:
                    cancelar_guia(guia, actor, motivo=f"Parar en piso: caja {caja.numero} de {fresco.folio}")
                    canceladas.append(guia.numero)
                elif guia is not None and pk in pks_guia:
                    raise ValueError(f"La guía {guia.numero} de la caja {caja.numero} ya salió ({guia.get_estado_display().lower()}): no se cancela.")
                _reabrir_cierre(caja)
            if pk in pks_contenido:
                desarmadas.append({"caja": caja.numero, "regreso": _desarmar_caja_al_parar(fresco, caja, actor, motivo)})
        if fresco.estado == Pedido.GUIA_GENERADA and (canceladas or desarmadas):
            fresco.transicionar(Pedido.EMPACADO, actor=actor, motivo=f"Parar en piso: guía cancelada o caja desarmada ({motivo})"[:300])
        if desarmadas:
            if fresco.estado == Pedido.EMPACADO:
                fresco.transicionar(Pedido.EN_PICKING, actor=actor, motivo=f"Parar en piso: hay piezas por volver a pickear ({motivo})"[:300])
            elif fresco.estado == Pedido.PARCIALMENTE_DESPACHADO:
                fresco.transicionar(Pedido.PENDIENTE, actor=actor, motivo=f"Parar en piso: hay piezas por volver a pickear ({motivo})"[:300])
        registrar_evento(
            "pedido", fresco.pk, "parado_en_piso", actor=actor, cliente=fresco.cliente,
            delta={"guias_canceladas": canceladas, "cajas_desarmadas": desarmadas, "estado": fresco.estado},
            motivo=motivo[:300],
        )
        incidencia = detener_pedido(fresco, actor, motivo, origen="mesa")
    pedido.estado = fresco.estado
    return incidencia


def reanudar_pedido(pedido, actor, motivo=""):
    """Mesa resolvió el problema: el pedido vuelve a la cola de Mi turno, el
    primero por antigüedad (sigue con su `creado` / `ts_picking`), sin dueño
    (lo toma quien le toque). Las incidencias DET/PAQ abiertas se resuelven
    al reanudar desde ellas; si quedara otra abierta, no se toca. Sin efecto
    si no estaba detenido."""
    if not pedido.detenido:
        return False
    pedido.detenido = False
    pedido.save(update_fields=["detenido", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "pedido_reanudado", actor=actor, cliente=pedido.cliente,
        motivo=(motivo or "Mesa reanudó el pedido: vuelve a la cola, el primero por antigüedad.")[:300],
    )
    return True


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
        # Lote por pedido: lo que se anotó al escanear se vuelve a anotar al re-escanear.
        PickLinea.objects.filter(linea__in=lineas, estado=PickLinea.PICKEADO).delete()
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


def confirmar_linea_pick(linea, cantidad, actor, codigo_escaneado=None, *, ubicacion=None, lote=None):
    """Confirma unidades pickeadas de una línea, por escaneo.

    Si viene codigo_escaneado, valida contra el código de barras (o código)
    del SKU de la línea: escaneo equivocado → ValueError.
    Atómico con select_for_update de la línea: dos escaneos concurrentes se
    serializan y el segundo valida sobre el avance FRESCO (nada se pierde ni
    se pasa del pedido).
    `ubicacion` / `lote` (Chema 2026-10-22, lote por pedido): de dónde salió
    la pieza según el escaneo (el piso resuelve el lote del QR y la posición
    del rack o del FEFO); queda en PickLinea para que empacar y la salida
    resten exactamente eso. Sin ellos la pieza queda "sin registro" (pick
    automático, hija de kit, seed) y se resta como siempre.
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
        if fresca.cantidad_pickeada + cantidad > fresca.con_stock:
            raise ValueError(
                f"Te pasas: la línea pide {fresca.con_stock} y ya llevas {fresca.cantidad_pickeada}. "
                "Revisa la cantidad antes de confirmar."
            )
        fresca.cantidad_pickeada += cantidad
        fresca.save(update_fields=["cantidad_pickeada"])
        PickLinea.objects.create(linea=fresca, ubicacion=ubicacion, lote=lote, cantidad=cantidad)
        registrar_evento(
            "linea_pedido", fresca.pk, "pick", actor=actor, cliente=pedido.cliente,
            delta={
                "sku": fresca.sku.codigo, "cantidad": cantidad, "pickeada": fresca.cantidad_pickeada,
                "posicion": ubicacion.codigo if ubicacion is not None else None,
                "lote": lote.codigo if lote is not None else None,
            },
            motivo=f"Pick de {pedido.folio}",
        )
    # La instancia del caller refleja el avance real (las vistas la re-pintan).
    linea.cantidad_pickeada = fresca.cantidad_pickeada
    return fresca


def _codigos_de(slices):
    """[(posición, lote, piezas)] legible a partir de [(ubicacion_id, lote_id, piezas)]."""
    from apps.catalogo.models import Lote, Ubicacion  # lazy: modelos de otra app

    ubicaciones = dict(Ubicacion.objects.filter(pk__in={u for u, _, _ in slices if u}).values_list("pk", "codigo"))
    lotes = dict(Lote.objects.filter(pk__in={l for _, l, _ in slices if l}).values_list("pk", "codigo"))
    return [[ubicaciones.get(u), lotes.get(l), n] for u, l, n in slices]


def _fusionar_slices(slices):
    """[(ubicacion_id, lote_id, piezas)] sumando las piezas de la misma posición y lote, en orden de aparición."""
    acumulado, orden = {}, []
    for ubicacion_id, lote_id, piezas in slices:
        clave = (ubicacion_id, lote_id)
        if clave not in acumulado:
            orden.append(clave)
        acumulado[clave] = acumulado.get(clave, 0) + int(piezas)
    return [(u, l, acumulado[(u, l)]) for u, l in orden if acumulado[(u, l)] > 0]


def _confirmar_pick_linea(linea, cantidad, folio):
    """Confirma en inventario `cantidad` piezas de la línea restando PRIMERO lo
    anotado al escanear (PickLinea pickeadas: posición y lote; lote por pedido,
    Chema 2026-10-22) y deja los registros "en empaque" con lo que el kardex
    restó de verdad. Lo anotado que el registro no tenía (restock sin
    registrar, traslado posterior) se resta como siempre y queda el evento
    `pick_fuera_de_registro` del pedido para que Mesa lo vea. Sin registros
    (pedido anterior al cambio, hija de kit) empaca como siempre y los
    registros nacen con lo que salió. Regresa [(ubicacion_id, lote_id, piezas)]."""
    from apps.inventario.services import confirmar_pick  # lazy

    picks = list(linea.picks.filter(estado=PickLinea.PICKEADO).order_by("pk"))
    preferir = [
        (p.ubicacion_id, p.lote_id, p.cantidad) for p in picks
        if p.ubicacion_id is not None or p.lote_id is not None
    ]
    if preferir:
        resultado = confirmar_pick(linea.sku, cantidad, folio, preferir=preferir)
    else:
        resultado = confirmar_pick(linea.sku, cantidad, folio)
    # (origen, no_cumplido); una prueba que simula el inventario regresa otra cosa: sin registros.
    origen, no_cumplido = resultado if isinstance(resultado, tuple) and len(resultado) == 2 else ([], [])
    ahora = timezone.now()
    PickLinea.objects.filter(pk__in=[p.pk for p in picks]).delete()
    for ubicacion_id, lote_id, piezas in _fusionar_slices(origen):
        PickLinea.objects.create(
            linea=linea, ubicacion_id=ubicacion_id, lote_id=lote_id, cantidad=piezas,
            estado=PickLinea.EN_EMPAQUE, ts_estado=ahora,
        )
    if no_cumplido:
        pedido = linea.pedido
        registrar_evento(
            "pedido", pedido.pk, "pick_fuera_de_registro", cliente=pedido.cliente,
            delta={"sku": linea.sku.codigo, "anotado": _codigos_de(no_cumplido), "resto": _codigos_de(origen)},
            motivo=(
                f"{linea.sku.codigo}: lo anotado al escanear no estaba en el registro de esa posición; "
                "se restó de donde sí había. Revisa el restock o la posición."
            )[:300],
        )
    return origen


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
            hija = LineaPedido.objects.create(
                pedido=pedido, sku=sku, cantidad=piezas,
                cantidad_pickeada=piezas, reservada=True, parte_de_kit=linea_kit,
                kit_caja=caja,
            )
            # Sin registro de posición ni lote: el packer las tiene en la mano; se restan FEFO al empacar.
            PickLinea.objects.create(linea=hija, cantidad=piezas)
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
        incompletas = [l for l in fresco.lineas_por_surtir if l.cantidad_pickeada < l.con_stock]
        if incompletas:
            detalle = ", ".join(
                f"{l.sku.codigo} ({l.cantidad_pickeada}/{l.con_stock})" for l in incompletas
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

        for linea in fresco.lineas.select_related("sku"):
            # La línea kit se escanea (la caja física) pero no tiene stock propio.
            # Segunda ola: lo que salió con la primera (cantidad_despachada) se
            # confirmó entonces; solo se confirma lo nuevo. El físico sale de
            # lo anotado al escanear (lote por pedido).
            por_confirmar = linea.cantidad_pickeada - linea.cantidad_despachada
            if por_confirmar > 0 and not linea.sku.es_kit:
                _confirmar_pick_linea(linea, por_confirmar, fresco.folio)

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
    incompletas = [l for l in pedido.lineas_por_surtir if l.cantidad_pickeada < l.con_stock]
    if incompletas:
        detalle = ", ".join(
            f"{l.sku.codigo} ({l.cantidad_pickeada}/{l.con_stock})" for l in incompletas
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
    # Una caja fantasma (guía ya ida, registro viejo) cuenta como hecha (2026-10-21).
    if all(c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO) or c.ya_salio for c in cajas):
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
    serializan. Desde 2026-10-20 es el cierre DE GOLPE de una caja (estampa
    también ts_etiquetas y ts_listo_salida); el wizard cierra por tanda
    (marcar_etiquetas_pegadas → cerrar_tanda → confirmar_lista_salida).
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
    # Cierre de golpe (seeds, pruebas, cajas sueltas): equivale a etiquetas
    # pegadas + evidencia + lista en salida; el wizard usa la tanda (2026-10-20).
    ahora = timezone.now()
    fresco.ts_cierre = ahora
    fresco.foto_cierre = evidencia
    fresco.ts_etiquetas = fresco.ts_etiquetas or ahora
    fresco.ts_listo_salida = ahora
    fresco.save(update_fields=["ts_cierre", "foto_cierre", "ts_etiquetas", "ts_listo_salida"])
    if fresco is not paquete:
        paquete.ts_cierre = fresco.ts_cierre
        paquete.foto_cierre = evidencia
        paquete.ts_etiquetas = fresco.ts_etiquetas
        paquete.ts_listo_salida = fresco.ts_listo_salida
    registrar_evento(
        "paquete", fresco.pk, "caja_cerrada_con_evidencia", actor=actor,
        cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "caja": fresco.numero, "evidencia_id": evidencia.pk},
        motivo=f"Caja {fresco.numero} de {pedido.folio} cerrada con la etiqueta pegada.",
    )
    return fresco


# ── Tanda de empaque (Chema 2026-10-20): etiquetas pegadas → UNA foto → lista en salida ──
# La tanda son las cajas EMPACADO del pedido que siguen en bodega (nacen juntas
# al pesar la última caja de la ola). El wizard deriva su paso de los sellos de
# cada caja; cada servicio estampa SOLO a las cajas que siguen EMPACADO y aún no
# tienen ese sello (una caja que el carrier sacó a media tanda ya no entra; una
# caja nueva que se suma después hace su propia ronda).


def _en_la_mesa(caja):
    """Una caja EMPACADO sigue en la mesa mientras no esté confirmada en salida
    ni su guía haya salido (Chema 2026-10-21: lo que ya está en el corral o ya
    se fue no vuelve a aparecer en el wizard)."""
    return caja.ts_listo_salida is None and not caja.ya_salio


def cajas_de_tanda(pedido):
    """Cajas EMPACADO del pedido que siguen en la mesa, por número: la tanda
    que el wizard lleva de las etiquetas a Salida. Las despachadas, las ya
    confirmadas en salida y las fantasma (guía ya ida) no piden nada."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    return [
        c for c in sorted(pedido.paquetes.all(), key=lambda c: c.numero)
        if c.estado == Paquete.EMPACADO and _en_la_mesa(c)
    ]


def _tanda_bajo_candado(pedido):
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    cajas = (
        Paquete.objects.select_for_update().filter(pedido=pedido, estado=Paquete.EMPACADO)
        .prefetch_related("guias").order_by("numero")
    )
    return [c for c in cajas if not c.ya_salio]


def _numeros(cajas):
    return ", ".join(str(c.numero) for c in cajas)


@transaction.atomic
def marcar_etiquetas_pegadas(pedido, actor):
    """"Etiquetas pegadas ✓" del wizard: el operador afirma que cada caja de
    la tanda ya trae su etiqueta. Exige guía viva en TODAS las cajas en
    bodega (sin guía no hay etiqueta que pegar: "Reintentar guía"); estampa
    Paquete.ts_etiquetas solo en las que no lo tenían. ValueError si no hay
    cajas, si alguna no tiene guía o si ya todas estaban estampadas. Evento
    `etiquetas_pegadas`. Regresa las cajas estampadas."""
    tanda = _tanda_bajo_candado(pedido)
    if not tanda:
        raise ValueError(f"{pedido.folio} no tiene cajas empacadas en bodega.")
    sin_guia = [c for c in tanda if c.guia_activa is None]
    if sin_guia:
        raise ValueError(
            f"La caja {_numeros(sin_guia)} de {pedido.folio} todavía no tiene guía: "
            "reintenta la guía antes de pegar las etiquetas."
        )
    pendientes = [c for c in tanda if c.ts_etiquetas is None]
    if not pendientes:
        raise ValueError(f"{pedido.folio} ya tiene las etiquetas pegadas en todas sus cajas.")
    ahora = timezone.now()
    for caja in pendientes:
        caja.ts_etiquetas = ahora
        caja.save(update_fields=["ts_etiquetas"])
    registrar_evento(
        "pedido", pedido.pk, "etiquetas_pegadas", actor=actor, cliente=pedido.cliente,
        delta={"cajas": [c.numero for c in pendientes]},
        motivo=f"Etiquetas pegadas en la caja {_numeros(pendientes)} de {pedido.folio}.",
    )
    return pendientes


@transaction.atomic
def cerrar_tanda(pedido, actor, foto_tanda):
    """Foto de evidencia POR TANDA (Chema 2026-10-20): UNA foto de todas las
    cajas de la tanda cerradas con sus etiquetas, en vez de una por caja.
    Entran las cajas EMPACADO en bodega con guía viva y etiquetas pegadas que
    aún no tienen cierre; la misma EvidenciaFoto (tipo caja_cerrada, ligada
    al pedido, como en cerrar_caja) queda apuntada por `foto_cierre` de cada
    una, con `ts_cierre`. Evento `caja_cerrada_con_evidencia` por caja
    (bitácora, mismo nombre que la primitiva). Regresa las cajas cerradas."""
    if foto_tanda is None:
        raise ValueError("Toma la foto de las cajas cerradas con sus etiquetas antes de confirmar.")
    tanda = _tanda_bajo_candado(pedido)
    pendientes = [c for c in tanda if c.ts_cierre is None and c.ts_etiquetas is not None and c.guia_activa is not None]
    if not pendientes:
        raise ValueError(f"{pedido.folio} no tiene cajas esperando su foto de evidencia.")
    evidencia = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo=TIPO_FOTO_CIERRE,
        archivo=foto_tanda, tomada_por=_actor_nombre(actor),
    )
    ahora = timezone.now()
    numeros = [c.numero for c in pendientes]
    for caja in pendientes:
        caja.ts_cierre, caja.foto_cierre = ahora, evidencia
        caja.save(update_fields=["ts_cierre", "foto_cierre"])
        registrar_evento(
            "paquete", caja.pk, "caja_cerrada_con_evidencia", actor=actor, cliente=pedido.cliente,
            delta={"pedido": pedido.folio, "caja": caja.numero, "evidencia_id": evidencia.pk, "tanda": numeros},
            motivo=f"Caja {caja.numero} de {pedido.folio} cerrada con la etiqueta pegada (foto de la tanda).",
        )
    return pendientes


@transaction.atomic
def confirmar_lista_salida(pedido, actor):
    """"Lista en salida ▶" del wizard: las cajas de la tanda con guía,
    etiquetas y evidencia quedan listas en el corral (Paquete.ts_listo_salida);
    desde aquí Salida las lista y el chofer se las puede llevar. ValueError
    si ninguna caja espera esa confirmación. Evento `lista_en_salida`.
    Regresa las cajas confirmadas."""
    tanda = _tanda_bajo_candado(pedido)
    pendientes = [c for c in tanda if c.ts_listo_salida is None and c.ts_cierre is not None and c.guia_activa is not None]
    if not pendientes:
        raise ValueError(f"{pedido.folio} no tiene cajas con evidencia esperando confirmar en salida.")
    ahora = timezone.now()
    for caja in pendientes:
        caja.ts_listo_salida = ahora
        caja.save(update_fields=["ts_listo_salida"])
    registrar_evento(
        "pedido", pedido.pk, "lista_en_salida", actor=actor, cliente=pedido.cliente,
        delta={"cajas": [c.numero for c in pendientes]},
        motivo=f"Caja {_numeros(pendientes)} de {pedido.folio} lista en el corral de salida.",
    )
    return pendientes


def _reabrir_cierre(caja):
    """La etiqueta de la caja cambia (guía cancelada por cambio de dirección o
    de paquetería): se borran etiquetas pegadas, cierre, foto y lista en
    salida para que el wizard la lleve otra vez por sus pantallas. Regresa
    los campos borrados."""
    campos = []
    if caja.ts_etiquetas is not None:
        caja.ts_etiquetas = None
        campos.append("ts_etiquetas")
    if caja.ts_cierre is not None or caja.foto_cierre_id:
        caja.ts_cierre, caja.foto_cierre = None, None
        campos += ["ts_cierre", "foto_cierre"]
    if caja.ts_listo_salida is not None:
        caja.ts_listo_salida = None
        campos.append("ts_listo_salida")
    if campos:
        caja.save(update_fields=campos)
    return campos


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
    """EMPACADO → GUIA_GENERADA vía envios.services.generar_guia (lazy,
    idempotente). También con PARCIALMENTE_DESPACHADO (2026-10-05): la caja
    que se quedó o que se quitó de salida recompra SU guía (las despachadas
    no se tocan) y el pedido conserva su estado."""
    if pedido.estado not in (Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
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
    Solo las guías que AÚN NO SALEN: una segunda ola o una reposición no
    reimprime las cajas ya despachadas (Chema 2026-09-28, PED-00045).
    Regresa (guias, mensajes)."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app
    from apps.piso.etiquetas import imprimir_etiqueta  # lazy por contrato

    guias = [
        g for g in pedido.guias.filter(estado=Guia.GUIA_CREADA).select_related("paquete").order_by("id")
        if g.paquete is None or (g.paquete.estado != Paquete.DESPACHADO and not g.paquete.ya_salio)
    ]
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
            _reabrir_cierre(caja)
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


def _unidades_en_otras_cajas(pedido, salen, *, medias_cajas_enteras=False):
    """{linea_pk: unidades de venta} que siguen en OTRAS cajas del pedido aún
    en bodega (no despachadas y fuera de `salen`). Eso NO sale del kardex con
    estas cajas (Chema 2026-10-15, PED-00151: la pieza de reposición vivía en
    la caja 2 cuando el carrier dio por salida la caja 1). Fracciones hacia
    arriba (salida por carrier); con `medias_cajas_enteras` hacia abajo: una
    media caja que se queda NO retiene la unidad, que sale entera con la
    primera mitad (regla del manifiesto por caja)."""
    import math
    from fractions import Fraction

    from apps.envios.models import Paquete  # lazy: modelo de otra app

    fuera = {c.pk for c in salen}
    acumulado = {}
    for caja in pedido.paquetes.exclude(estado=Paquete.DESPACHADO).prefetch_related("lineas"):
        if caja.pk in fuera:
            continue
        for pl in caja.lineas.all():
            acumulado[pl.linea_pedido_id] = acumulado.get(pl.linea_pedido_id, Fraction(0)) + Fraction(pl.cantidad, max(pl.fraccion_de, 1))
    redondeo = math.floor if medias_cajas_enteras else math.ceil
    return {pk: redondeo(v) for pk, v in acumulado.items()}


def _cupos_por_caja(linea, cajas):
    """[(caja, piezas)] que viajan de la línea en cada caja, por número (medias
    cajas redondeadas hacia arriba); sin cajas, [] (los registros salen sin caja)."""
    import math
    from fractions import Fraction

    cupos = []
    for caja in sorted(cajas or [], key=lambda c: c.numero):
        unidades = sum(
            (Fraction(pl.cantidad, max(pl.fraccion_de, 1)) for pl in caja.lineas.all() if pl.linea_pedido_id == linea.pk),
            Fraction(0),
        )
        if unidades > 0:
            cupos.append([caja, math.ceil(unidades)])
    return cupos


def _mover_registros(linea, picks, cantidad, estado, *, cajas=None, origen=None):
    """Pasa `cantidad` piezas de los registros `picks` (en orden) al `estado`
    dado, partiendo un registro cuando solo sale una parte, y les asigna caja
    según `cajas` (cupos por número de caja: lo que viaja de la línea en cada
    una). Si los registros no alcanzan (pedido anterior al cambio), nacen
    registros con `origen` [(ubicacion_id, lote_id, piezas)] o, sin origen,
    sin posición ni lote. Regresa las piezas movidas."""
    ahora = timezone.now()
    cupos = _cupos_por_caja(linea, cajas)
    pendientes = list(picks)
    extra = [list(s) for s in (origen or [])]
    restante = cantidad
    while restante > 0:
        pick = pendientes[0] if pendientes else None
        if pick is not None:
            ubicacion_id, lote_id, disponible = pick.ubicacion_id, pick.lote_id, pick.cantidad
        elif extra:
            ubicacion_id, lote_id, disponible = extra[0]
        else:
            ubicacion_id, lote_id, disponible = None, None, restante
        tomo = min(int(disponible), restante)
        caja = None
        for cupo in cupos:
            if cupo[1] > 0:
                caja = cupo[0]
                tomo = min(tomo, cupo[1])
                cupo[1] -= tomo
                break
        if tomo <= 0:
            break
        if pick is not None:
            pendientes.pop(0)
            if tomo < pick.cantidad:
                resto = PickLinea.objects.create(
                    linea=linea, ubicacion_id=pick.ubicacion_id, lote_id=pick.lote_id,
                    cantidad=pick.cantidad - tomo, estado=pick.estado, caja_id=pick.caja_id, ts_estado=pick.ts_estado,
                )
                pendientes.insert(0, resto)
                pick.cantidad = tomo
            pick.estado, pick.ts_estado = estado, ahora
            if caja is not None:
                pick.caja = caja
            pick.save(update_fields=["cantidad", "estado", "ts_estado", "caja"])
        else:
            if extra:
                extra[0][2] -= tomo
                if extra[0][2] <= 0:
                    extra.pop(0)
            PickLinea.objects.create(
                linea=linea, ubicacion_id=ubicacion_id, lote_id=lote_id, cantidad=tomo,
                estado=estado, caja=caja, ts_estado=ahora,
            )
        restante -= tomo
    return cantidad


def _despachar_linea(linea, cantidad, folio, cajas=None, *, tolerar=False):
    """Salida del kardex de `cantidad` piezas de la línea restando PRIMERO lo
    que la línea tiene anotado en empaque (PickLinea: posición y lote, en
    orden de pick; lote por pedido, Chema 2026-10-22) y marcando esos
    registros como despachados con la caja en que salieron (`cajas`: cupos
    por número según el plan). `tolerar` (salida por carrier, Chema
    2026-10-15): si el kardex no tiene todo en empaque descuenta lo que haya y
    deja el evento `salida_kardex_corto`; nunca truena por eso. Lo anotado
    que el registro de empaque no tenía sale FEFO y queda el evento
    `salida_fuera_de_registro`. Regresa las piezas que salieron del kardex."""
    from apps.inventario.models import Saldo  # lazy: modelo de otra app
    from apps.inventario.services import despachar  # lazy por contrato

    pedido = linea.pedido
    del_kardex = cantidad
    if tolerar:
        disponible = Saldo.objects.filter(sku=linea.sku, estado=Saldo.EN_EMPAQUE).aggregate(t=Sum("cantidad"))["t"] or 0
        del_kardex = min(cantidad, disponible)
    picks = list(linea.picks.filter(estado=PickLinea.EN_EMPAQUE).order_by("pk"))
    preferir = [(p.ubicacion_id, p.lote_id, p.cantidad) for p in picks if p.ubicacion_id is not None or p.lote_id is not None]
    origen, no_cumplido = [], []
    if del_kardex > 0:
        resultado = despachar(linea.sku, del_kardex, folio, preferir=preferir) if preferir else despachar(linea.sku, del_kardex, folio)
        if isinstance(resultado, tuple) and len(resultado) == 2:
            origen, no_cumplido = resultado
    _mover_registros(linea, picks, cantidad, PickLinea.DESPACHADO, cajas=cajas, origen=origen)
    if no_cumplido:
        registrar_evento(
            "pedido", pedido.pk, "salida_fuera_de_registro", cliente=pedido.cliente,
            delta={"sku": linea.sku.codigo, "anotado": _codigos_de(no_cumplido), "resto": _codigos_de(origen)},
            motivo=f"{linea.sku.codigo}: lo anotado en empaque no estaba en el registro; salió lo que había (lote más viejo primero)."[:300],
        )
    if del_kardex < cantidad:
        registrar_evento(
            "pedido", pedido.pk, "salida_kardex_corto", cliente=pedido.cliente,
            delta={"sku": linea.sku.codigo, "por_despachar": cantidad, "kardex": del_kardex, "solo_marcadas": cantidad - del_kardex},
            motivo="Salida por carrier: el kardex no tenía todas las piezas en empaque; se descontó lo que había y el resto solo se marcó.",
        )
    return del_kardex


def _despachar_lo_que_haya(linea, por_despachar, pedido, cajas=None):
    """Salida por carrier con el kardex corto (Chema 2026-10-15): descuenta lo
    que haya en empaque de ese producto y deja evento `salida_kardex_corto`
    con lo que solo se marcó; nunca truena por eso."""
    return _despachar_linea(linea, por_despachar, pedido.folio, cajas, tolerar=True)


def cajas_por_salir(pedido):
    """Cajas EMPACADO del pedido (aún en bodega) cuando se empacó POR CAJA;
    [] si el pedido se empacó entero (legacy: sin plan o plan sin empacar por
    caja), que sale completo en un solo manifiesto."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    cajas = sorted(pedido.paquetes.all(), key=lambda c: c.numero)
    if not any(c.estado in (Paquete.EMPACADO, Paquete.DESPACHADO) for c in cajas):
        return []
    return [c for c in cajas if c.estado == Paquete.EMPACADO]


def cajas_listas_en_salida(pedido):
    """De cajas_por_salir, las que ya pasaron por "Lista en salida"
    (ts_listo_salida) y tienen guía viva que aún no sale: las que Salida lista
    y el chofer se puede llevar (Chema 2026-10-20: la puerta a Salida es por
    caja; una caja fantasma cuya guía ya se fue no se ofrece)."""
    return [
        c for c in cajas_por_salir(pedido)
        if c.ts_listo_salida is not None and c.guia_activa is not None and not c.ya_salio
    ]


def salida_por_carrier(pedido, guia, ts, descripcion=""):
    """Salida automática "sin escaneo" (Chema 2026-10-12; sin condiciones
    desde 2026-10-15): el carrier reportó que ya tiene una caja que Torre
    todavía ve en bodega, así que fue error nuestro no escanearla y la caja
    SALE. Pasa a DESPACHADO (una caja aún planeada o en empaque con guía se
    cierra primero); del kardex se descuenta solo lo que no esté en otra caja
    del mismo pedido que siga en bodega, y si el kardex no alcanza se
    descuenta lo que hay y el resto solo se marca. Con el pedido con guía
    lista o parcial, el pedido sigue a sus cajas como con el manifiesto
    (parcial / recolectado, compromiso, fulfillment y aviso); en cualquier
    otro estado (empacado con guías a medias, pendiente por una reposición,
    cancelación pendiente, o ya en la calle con una caja fantasma como la
    caja 1 del PED-00151) sale SOLO la caja y el pedido no se mueve. Una
    guía del pedido entero saca todo lo que siga en bodega. Regresa False
    solo si no hay nada que sacar; lo que truene lo reintenta el rastreo en
    la siguiente pasada. Evento `salida_sin_escaneo` con origen "carrier".
    Si el carrier se equivocó, Mesa lo revierte con "Quitar de salida"."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    if guia.paquete_id:
        if guia.paquete.estado == Paquete.DESPACHADO:
            return False
        cajas = [guia.paquete]
    else:
        cajas = [c for c in pedido.paquetes.all() if c.estado != Paquete.DESPACHADO]
        if not cajas and pedido.paquetes.exists():
            return False
    listo = pedido.estado in (Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO)
    if not cajas and not listo:
        return False  # pedido viejo sin cajas que no está en Salida: nada que sacar
    with transaction.atomic():
        for caja in cajas:
            if caja.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE):
                caja.transicionar(
                    Paquete.EMPACADO,
                    motivo=f"El carrier ya tiene la caja {caja.numero} de {pedido.folio}: se cierra para darle salida.",
                )
        marcar_recolectado(
            pedido, None, paquetes=cajas or None,
            respetar_otras_cajas=True, tolerar_kardex=True, mover_pedido=listo,
        )
        registrar_evento(
            "pedido", pedido.pk, "salida_sin_escaneo", cliente=pedido.cliente,
            delta={"origen": "carrier", "caja": guia.paquete.numero if guia.paquete_id else None,
                   "cajas": [c.numero for c in cajas], "guia": guia.numero, "carrier": guia.carrier,
                   "ts_carrier": ts.isoformat() if ts else None, "pedido_movido": listo},
            motivo=f"El carrier reportó la caja antes de registrar su salida ({descripcion or 'recolección'}): salió sin escaneo."[:300],
        )
    return True


def salida_sin_registro(pedido, caja, actor, motivo=""):
    """Mesa da por salida una caja que sigue listada en Salida pero no está
    en el corral (Chema 2026-10-12, desde la incidencia "Caja sin salida"):
    misma salida que el manifiesto, evento `salida_sin_escaneo` origen "mesa".
    `caja` None = el pedido salió entero. Resuelve la incidencia CSS si ya no
    queda nada por salir."""
    if pedido.estado not in (Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
        raise ValueError(f"{pedido.folio} está {pedido.get_estado_display()}: no tiene cajas listas en Salida.")
    pendientes = cajas_por_salir(pedido)
    if caja is not None and caja.pk not in {c.pk for c in pendientes}:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} ya no está por salir.")
    with transaction.atomic():
        marcar_recolectado(pedido, actor, paquetes=[caja] if caja is not None else None)
        registrar_evento(
            "pedido", pedido.pk, "salida_sin_escaneo", actor=actor, cliente=pedido.cliente,
            delta={"origen": "mesa", "caja": caja.numero if caja is not None else None},
            motivo=(motivo or "Mesa dio la caja por salida sin escaneo (no estaba en el corral).")[:300],
        )
        _cerrar_css_si_nada_por_salir(pedido, actor)
    return pedido


HORAS_SIN_SALIDA = 24


def _domingos_entre(desde, hasta):
    """Cuántos domingos (fecha local) hay en [desde, hasta]."""
    d, fin = timezone.localtime(desde).date(), timezone.localtime(hasta).date()
    cuenta = 0
    while d <= fin:
        if d.weekday() == 6:
            cuenta += 1
        d += timedelta(days=1)
    return cuenta


def cajas_sin_salida(ahora=None):
    """Cajas listas en Salida que ya debieron irse (Chema 2026-10-12): la guía
    nació hace más de HORAS_SIN_SALIDA horas sin contar domingos, el carrier
    no ha reportado la caja, y desde que llegó ya cerró al menos una salida
    de su paquetería sin ella (el camión pasó y no se subió). Pedido con guía
    lista (GUIA_GENERADA / PARCIALMENTE_DESPACHADO) y caja EMPACADO con guía
    viva y confirmada "Lista en salida" (sin confirmar no está en el corral);
    sin cajas por caja, la guía del pedido. Regresa [{pedido, caja,
    guia, manifiesto, horas}] del más viejo al más nuevo."""
    from apps.envios.models import Guia, Manifiesto, Paquete  # lazy: modelos de otra app
    from apps.envios.services import CARRIER_LOCAL  # lazy por contrato

    ahora = ahora or timezone.now()
    guias = (
        Guia.objects.filter(
            estado=Guia.GUIA_CREADA, ts_recolectado_carrier__isnull=True,
            pedido__estado__in=[Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO],
        )
        .exclude(carrier=CARRIER_LOCAL)
        .select_related("pedido__cliente", "paquete").order_by("creado")
    )
    filas = []
    for guia in guias:
        if guia.paquete_id and guia.paquete.estado != Paquete.EMPACADO:
            continue
        if guia.paquete_id and guia.paquete.ts_listo_salida is None:
            continue  # no está en el corral: es "Completar empaquetado", no "Caja sin salida" (2026-10-20)
        if not guia.paquete_id and cajas_por_salir(guia.pedido):
            continue
        plazo = timedelta(hours=HORAS_SIN_SALIDA * (1 + _domingos_entre(guia.creado, ahora)))
        if ahora - guia.creado < plazo:
            continue
        manifiesto = Manifiesto.objects.filter(carrier=guia.carrier, ts__gt=guia.creado).order_by("ts").first()
        if manifiesto is None:
            continue
        filas.append({
            "pedido": guia.pedido, "caja": guia.paquete if guia.paquete_id else None, "guia": guia,
            "manifiesto": manifiesto, "horas": int((ahora - guia.creado).total_seconds() // 3600),
        })
    return filas


def revisar_cajas_sin_salida(ahora=None):
    """Job (lo corre poll_tracking): abre UNA incidencia interna "Caja sin
    salida" (P1) por pedido con cajas que ya debieron irse, y resuelve las
    abiertas cuyas cajas ya salieron. Regresa {"abiertas", "resueltas"}."""
    from apps.incidencias.models import Incidencia  # lazy: modelo de otra app
    from apps.incidencias.services import abrir_incidencia, caja_sin_salida_abierta, resolver  # lazy por contrato

    resumen = {"abiertas": 0, "resueltas": 0}
    por_pedido = {}
    for fila in cajas_sin_salida(ahora):
        por_pedido.setdefault(fila["pedido"].pk, (fila["pedido"], []))[1].append(fila)
    for pedido, filas in por_pedido.values():
        if caja_sin_salida_abierta(pedido) is not None:
            continue
        partes = []
        for f in filas:
            donde = f"caja {f['caja'].numero}" if f["caja"] else "el pedido entero"
            partes.append(
                f"{donde} (guía {f['guia'].numero}, {f['guia'].carrier}): {f['horas']} h lista en Salida; "
                f"la salida {f['manifiesto'].folio} de {f['guia'].carrier} cerró sin ella y el carrier no la ha reportado"
            )
        texto = (
            "Caja sin salida: " + "; ".join(partes)
            + ". Búscala en el corral: si aparece, escanéala en la siguiente salida; si ya se la llevaron, "
            "márcala aquí como salida sin registro; si no aparece, es paquete extraviado."
        )
        abrir_incidencia(pedido.cliente, Incidencia.TIPO_CSS, Incidencia.ORIGEN_AUTO, pedido=pedido, texto=texto, interna=True, pese_a_pausa=True)
        resumen["abiertas"] += 1
    abiertas = Incidencia.objects.filter(tipo=Incidencia.TIPO_CSS, interna=True, estado__in=Incidencia.ESTADOS_ABIERTOS).select_related("pedido")
    for inc in abiertas:
        if inc.pedido_id and inc.pedido.pk not in por_pedido and not _por_salir(inc.pedido):
            resolver(inc, "Las cajas ya salieron (escaneadas, reportadas por el carrier o dadas por salidas desde Mesa).", "sistema")
            resumen["resueltas"] += 1
    return resumen


def _por_salir(pedido):
    """True si al pedido le queda algo listo en Salida (cajas EMPACADO, o el pedido entero con guía lista)."""
    pedido.refresh_from_db(fields=["estado"])
    if pedido.estado not in (Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
        return False
    return bool(cajas_por_salir(pedido)) or pedido.estado == Pedido.GUIA_GENERADA


def _cerrar_css_si_nada_por_salir(pedido, actor):
    """Resuelve la incidencia "Caja sin salida" del pedido cuando ya nada queda en Salida."""
    try:
        from apps.incidencias.services import caja_sin_salida_abierta, resolver  # lazy por contrato
    except ImportError:
        return
    inc = caja_sin_salida_abierta(pedido)
    if inc is not None and not _por_salir(pedido):
        resolver(inc, "Ya no queda nada por salir de este pedido.", actor)


def avisar_recoleccion_carrier(pedido, guia, notificar=True):
    """El carrier recogió una caja que Torre aún no registró en Salida (Chema
    2026-09-25): el comprador y Shopify se enteran ya (plantilla B y
    fulfillment de ESA caja), una sola vez; cuando llegue el manifiesto,
    marcar_recolectado vuelve a llamarlos y ambos son idempotentes."""
    if notificar:
        try:
            from apps.mensajeria.services import enviar_en_camino  # lazy — plantilla B
            enviar_en_camino(pedido)
        except Exception:  # noqa: BLE001, S110 — best-effort
            pass
    cajas = [guia.paquete] if guia.paquete_id else None
    def _fulfillment():
        try:
            from apps.integraciones.services import marcar_fulfillment  # lazy
            marcar_fulfillment(pedido, cajas=cajas, notificar=notificar)
        except Exception:  # noqa: BLE001, S110 — best-effort
            pass
    transaction.on_commit(_fulfillment)


def marcar_recolectado(pedido, actor, paquetes=None, respetar_otras_cajas=False, tolerar_kardex=False, mover_pedido=True, kardex_por_caja=False):
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

    Banderas de la salida automática por carrier (Chema 2026-10-15):
    `respetar_otras_cajas` descuenta del kardex solo lo que no esté en otra
    caja del pedido que siga en bodega, y la línea avanza solo lo de estas
    cajas; `tolerar_kardex` descuenta lo que haya en empaque y solo marca el
    resto (evento `salida_kardex_corto`) en vez de tronar; `mover_pedido=False`
    saca SOLO las cajas dadas (DESPACHADO, kardex, compromiso y fulfillment)
    sin tocar el estado del pedido, para pedidos que no están en Salida.
    Salida del pedido entero (sin cajas por caja): también salen los
    registros de cajas que no estaban empacadas (planeadas después del
    empaque, PED-00151) para que ninguna se quede viva.
    """
    if mover_pedido and pedido.estado not in (Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO):
        raise ValueError(
            f"El pedido {pedido.folio} no tiene guía lista (está {pedido.get_estado_display()}); "
            "no se puede marcar recolectado."
        )
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    primer_manifiesto = mover_pedido and pedido.estado == Pedido.GUIA_GENERADA
    with transaction.atomic():
        Pedido.objects.select_for_update().get(pk=pedido.pk)
        if mover_pedido:
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
        else:
            # Solo cajas (salida por carrier con el pedido fuera de Salida): las dadas que sigan en bodega.
            pendientes = []
            salen = [c for c in (paquetes or []) if c.estado != Paquete.DESPACHADO]
            if not salen:
                raise ValueError(f"{pedido.folio}: ninguna de esas cajas sigue en bodega.")
        if salen:
            lineas = list(_lineas_de_cajas(salen).values())
        else:
            lineas = list(pedido.lineas.select_related("sku"))
        # `kardex_por_caja` (fix del manifiesto, Chema 2026-10-21; PED-00281 y
        # PED-00507): con varias cajas enteras del mismo producto, del kardex
        # sale SOLO lo que viaja en las cajas que suben; lo que se queda en
        # otra caja sale cuando esa caja salga. Una media caja que se queda no
        # retiene la unidad (sale entera con la primera mitad, como siempre).
        if respetar_otras_cajas:
            en_otras = _unidades_en_otras_cajas(pedido, salen)
        elif kardex_por_caja and salen:
            en_otras = _unidades_en_otras_cajas(pedido, salen, medias_cajas_enteras=True)
        else:
            en_otras = {}
        for linea in lineas:
            # Sale del kardex lo pickeado que aún no ha salido: una línea
            # repartida en dos cajas sale entera con la primera (la unidad de
            # venta ya se abrió) y en una segunda ola lo de la primera ya se
            # fue (cantidad_despachada): nada se despacha dos veces. Con
            # `respetar_otras_cajas` lo que siga en otra caja en bodega se queda.
            por_despachar = linea.cantidad_pickeada - linea.cantidad_despachada - en_otras.get(linea.pk, 0)
            if por_despachar <= 0:
                continue
            if not linea.sku.es_kit:
                # Lote por pedido: sale lo anotado en empaque para esta línea, en orden
                # de pick, y los registros quedan con la caja en que salieron.
                cajas_que_salen = salen or list(pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero"))
                _despachar_linea(linea, por_despachar, pedido.folio, cajas_que_salen, tolerar=tolerar_kardex)
            if respetar_otras_cajas or (kardex_por_caja and salen):
                linea.cantidad_despachada = min(linea.cantidad_pickeada, linea.cantidad_despachada + por_despachar)
            else:
                linea.cantidad_despachada = linea.cantidad_pickeada
            linea.save(update_fields=["cantidad_despachada"])
        for caja in salen:
            caja.transicionar(
                Paquete.DESPACHADO, actor=actor,
                motivo=f"Caja {caja.numero} de {pedido.folio} subió al camión (manifiesto firmado).",
            )
        if mover_pedido and not salen:
            # Pedido entero (Chema 2026-10-15, PED-00151): los registros de cajas
            # que no estaban empacadas (planeadas después del empaque) también
            # salen; si no, se quedan vivos y una reposición los vuelve a ofrecer.
            for caja in pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero"):
                if caja.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE):
                    caja.transicionar(Paquete.EMPACADO, actor=actor, motivo=f"Salida del pedido entero {pedido.folio}: la caja {caja.numero} se cierra para salir.")
                caja.transicionar(Paquete.DESPACHADO, actor=actor, motivo=f"Salida del pedido entero {pedido.folio}: la caja {caja.numero} también salió.")
        # Compromiso de entrega (Chema 2026-09-25): la fecha límite corre
        # desde que la caja sale; el carrier puede haberla estampado antes
        # (recolección primero), y entonces no se mueve.
        from apps.envios.services import estampar_compromiso  # lazy por contrato
        pks_salen = {c.pk for c in salen}
        for guia in pedido.guias.all():
            if guia.es_activa and (guia.paquete_id is None or guia.paquete_id in pks_salen):
                estampar_compromiso(guia)
        if respetar_otras_cajas:
            # Salida por carrier: cualquier caja no despachada (aunque siga planeada o en empaque) sigue en bodega.
            fuera = {c.pk for c in salen}
            quedan = [c for c in pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero") if c.pk not in fuera]
        else:
            quedan = [c for c in pendientes if c not in salen]
        sin_inventario = sorted({l.sku.codigo for l in pedido.lineas_faltantes})
        if not mover_pedido:
            pass  # solo salieron cajas: el estado del pedido no se toca (salida por carrier fuera de Salida)
        elif quedan:
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
            # Tras el commit (de la transacción externa del manifiesto, si la hay):
            # un aviso lento jamás alarga la transacción ni frena la salida.
            transaction.on_commit(lambda: enviar_en_camino(pedido))
    # Fulfillment al llegar a Salida (Chema 2026-10-08): normalmente ya existe
    # desde que nació la guía (integraciones.fulfillment_en_salida); aquí solo
    # va el evento "recogido por la paquetería". Las cajas que llegan sin
    # fulfillment (empacadas antes del cambio, o Shopify caído entonces) lo
    # reciben como respaldo: UN fulfillment por caja que sube a ESTE camión;
    # sin cajas, el pedido entero. Desde 2026-10-14 (Chema: la salida se
    # trababa) NADA de esto habla con Shopify en la petición: tras el commit
    # se ENCOLA (integraciones.encolar_salida_shopify) y un hilo en segundo
    # plano lo escribe de inmediato (integraciones.drenar_escrituras_en_segundo_plano);
    # el cron sync_shopify queda de red cada 15 min.
    cajas_fuera = list(salen)

    def _fulfillment():
        try:
            from apps.integraciones.services import (  # lazy por contrato
                drenar_escrituras_en_segundo_plano, encolar_salida_shopify,
            )

            if encolar_salida_shopify(pedido, cajas_fuera, primer_manifiesto):
                drenar_escrituras_en_segundo_plano(pedido)
        except Exception:  # noqa: BLE001, S110 — Shopify jamás frena un manifiesto; la fila queda para el cron
            pass
    transaction.on_commit(_fulfillment)
    return pedido


def motivo_no_quitable_de_salida(pedido, caja):
    """Por qué una caja NO se puede quitar de salida; "" si sí se puede. Regla
    (Chema 2026-10-05, PED-00319): la caja firmó manifiesto (DESPACHADO) pero
    nunca se fue — su guía está cancelada (ninguna viva; una guía que viajó y
    regresó es un retorno, no esto) y ningún carrier reportó haberla recogido
    (ts_recolectado_carrier). Solo con el pedido RECOLECTADO o PARCIALMENTE
    DESPACHADO: en tránsito o entregado ya hay algo del pedido en la calle
    que el carrier movió, y eso se resuelve con él. Lee `caja.guias.all()`
    para respetar el prefetch del detalle."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app

    if pedido.estado not in (Pedido.RECOLECTADO, Pedido.PARCIALMENTE_DESPACHADO):
        return (
            f"{pedido.folio} está {pedido.get_estado_display().lower()}: solo se quita de salida una caja "
            "de un pedido recolectado o parcialmente despachado."
        )
    if caja.estado != Paquete.DESPACHADO:
        return f"La caja {caja.numero} de {pedido.folio} no ha salido: no hay nada que quitar."
    guias = list(caja.guias.all())
    if not guias:
        return f"La caja {caja.numero} de {pedido.folio} salió sin guía registrada: no se puede quitar de salida."
    viva = next((g for g in guias if g.es_activa), None)
    if viva is not None:
        return (
            f"La caja {caja.numero} de {pedido.folio} todavía tiene su guía viva ({viva.numero}): "
            "si el carrier no la recogió, cancela la guía primero."
        )
    recogida = next((g for g in guias if g.ts_recolectado_carrier is not None), None)
    if recogida is not None:
        return (
            f"El carrier reportó que recogió la caja {caja.numero} de {pedido.folio} (guía {recogida.numero}, "
            f"{timezone.localtime(recogida.ts_recolectado_carrier):%d/%b %H:%M}): no se puede quitar de salida."
        )
    if any(g.estado == Guia.RETORNO for g in guias):
        return f"La caja {caja.numero} de {pedido.folio} viajó y regresó (retorno): eso es un reingreso, no una salida que no fue."
    return ""


def _regresar_contenido_caja(pedido, caja, actor, ubicacion=None):
    """Reverso del kardex de marcar_recolectado para UNA caja que nunca se
    fue: lo que viaja en ella (PaqueteLinea, en unidades de venta enteras:
    una media caja reempacada no devuelve nada hasta que vuelvan las dos
    mitades) regresa de despachado a en_empaque y baja de
    `LineaPedido.cantidad_despachada`, con tope en lo despachado. Un kit
    arrastra a sus hijas (el kardex despachó a las hijas, nunca al kit
    virtual): todas si la caja llevaba el kit completo, proporcional si no.
    Regresa [[sku, unidades]] de lo devuelto."""
    from fractions import Fraction

    from apps.inventario.services import regresar_de_salida  # lazy

    unidades = {}
    lineas = {}
    for pl in caja.lineas.select_related("linea_pedido__sku"):
        lineas[pl.linea_pedido_id] = pl.linea_pedido
        unidades[pl.linea_pedido_id] = unidades.get(pl.linea_pedido_id, Fraction(0)) + Fraction(pl.cantidad, pl.fraccion_de or 1)
    devuelto = []

    def _devolver(linea, cuantas):
        cuantas = min(int(cuantas), linea.cantidad_despachada)
        if cuantas <= 0:
            return
        if not linea.sku.es_kit:
            # Lote por pedido: lo que salió con ESTA caja vuelve a su posición con su lote.
            picks = list(linea.picks.filter(estado=PickLinea.DESPACHADO, caja=caja).order_by("pk"))
            slices = _fusionar_slices((p.ubicacion_id, p.lote_id, p.cantidad) for p in picks if p.ubicacion_id is not None or p.lote_id is not None)
            if slices:
                regresar_de_salida(linea.sku, cuantas, pedido.folio, actor, ubicacion=ubicacion, slices=slices)
            else:
                regresar_de_salida(linea.sku, cuantas, pedido.folio, actor, ubicacion=ubicacion)
            _mover_registros(linea, picks, cuantas, PickLinea.EN_EMPAQUE)
            devuelto.append([linea.sku.codigo, cuantas])
        linea.cantidad_despachada -= cuantas
        linea.save(update_fields=["cantidad_despachada"])

    for pk, linea in lineas.items():
        enteras = int(unidades[pk])
        if linea.sku.es_kit:
            for hija in linea.componentes.select_related("sku"):
                _devolver(hija, hija.cantidad if enteras >= linea.cantidad else hija.cantidad * enteras // max(linea.cantidad, 1))
        _devolver(linea, enteras)
    return devuelto


def quitar_de_salida(pedido, caja, actor, motivo=""):
    """"Quitar de salida" (Chema 2026-10-05, PED-00319): la caja subió al
    manifiesto y firmó, pero nunca se fue — Mesa canceló su guía y el carrier
    no la recogió — y regresa a bodega para salir por otra paquetería o con
    la flota propia. Solo con motivo_no_quitable_de_salida en blanco. En UNA
    transacción: 1) la línea del manifiesto se marca `no_salio` (la hoja y
    la firma se conservan; deja de contar como salida en la línea de tiempo);
    2) el contenido de la caja vuelve al kardex (despachado → en_empaque, en
    el corral del manifiesto) y baja de cantidad_despachada
    (_regresar_contenido_caja); 3) la caja vuelve a EMPACADO conservando su
    cierre, peso y fotos; 4) con otras cajas aún fuera el pedido queda
    PARCIALMENTE_DESPACHADO; si era la última, vuelve a EMPACADO sin dueño y
    sin ts_recolectado (la siguiente salida lo vuelve a estampar). Después,
    desde el detalle, Mesa le cambia la paquetería a la caja (incluida la
    salida sin guía) y el piso recompra la guía. 5) Shopify (fulfillment al
    llegar a Salida, 2026-10-08): el fulfillment de la caja se queda tal
    cual, la caja sigue en bodega; la guía nueva le actualiza el rastreo
    (integraciones.fulfillment_en_salida). Auditado como
    caja_quitada_de_salida (manifiesto, chofer, lo devuelto). Regresa la caja."""
    from apps.catalogo.models import Ubicacion  # lazy: modelo de otra app
    from apps.envios.models import LineaManifiesto, Paquete  # lazy: modelos de otra app

    razon = motivo_no_quitable_de_salida(pedido, caja)
    if razon:
        raise ValueError(razon)
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        caja = Paquete.objects.select_for_update().get(pk=caja.pk)
        razon = motivo_no_quitable_de_salida(fresco, caja)
        if razon:
            raise ValueError(razon)
        ahora = timezone.now()
        hojas = list(
            LineaManifiesto.objects.filter(paquete=caja, no_salio=False).select_related("manifiesto").order_by("pk")
        )
        for linea in hojas:
            linea.no_salio, linea.ts_no_salio = True, ahora
            linea.save(update_fields=["no_salio", "ts_no_salio"])
        corral = None
        if hojas and hojas[-1].manifiesto.corral:
            corral = Ubicacion.objects.filter(codigo=hojas[-1].manifiesto.corral, tipo=Ubicacion.SALIDA, activo=True).first()
        devuelto = _regresar_contenido_caja(fresco, caja, actor, ubicacion=corral)
        caja.transicionar(
            Paquete.EMPACADO, actor=actor,
            motivo=f"Caja {caja.numero} de {fresco.folio} quitada de salida: nunca se fue, vuelve a bodega.",
        )
        quedan_fuera = [c.numero for c in fresco.paquetes.all() if c.pk != caja.pk and c.estado == Paquete.DESPACHADO]
        if quedan_fuera:
            if fresco.estado != Pedido.PARCIALMENTE_DESPACHADO:
                fresco.transicionar(
                    Pedido.PARCIALMENTE_DESPACHADO, actor=actor,
                    motivo=f"La caja {caja.numero} nunca se fue; la caja {', '.join(str(n) for n in quedan_fuera)} sigue fuera.",
                )
        else:
            fresco.asignado_a, fresco.ts_recolectado = None, None
            fresco.save(update_fields=["asignado_a", "ts_recolectado", "actualizado"])
            fresco.transicionar(
                Pedido.EMPACADO, actor=actor,
                motivo=f"La caja {caja.numero} nunca se fue y no queda ninguna fuera: el pedido vuelve a bodega.",
            )
        registrar_evento(
            "pedido", fresco.pk, "caja_quitada_de_salida", actor=actor, cliente=fresco.cliente,
            delta={
                "caja": caja.numero,
                "manifiestos": [l.manifiesto.folio for l in hojas],
                "chofer": hojas[-1].manifiesto.chofer if hojas else "",
                "guias": [g.numero for g in caja.guias.all()],
                "devuelto": devuelto,
                "quedan_fuera": quedan_fuera,
                "estado": fresco.estado,
            },
            motivo=(motivo or f"Caja {caja.numero} quitada de salida: guía cancelada y el carrier no la recogió.")[:300],
        )
    pedido.estado = fresco.estado
    return caja


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
                faltan = linea.por_pickear
                if faltan > 0:
                    confirmar_linea_pick(linea, faltan, actor)
        for linea in lineas:
            por_despachar = linea.cantidad_pickeada - linea.cantidad_despachada
            if por_despachar <= 0:
                continue  # sin pick, o ya salió con la primera ola
            if not linea.sku.es_kit:
                if estado_inicial != Pedido.EMPACADO:
                    _confirmar_pick_linea(linea, por_despachar, pedido.folio)
                _despachar_linea(
                    linea, por_despachar, pedido.folio,
                    list(pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero")),
                )
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


# ── Entrega propia (POD por caja) ──

def fijar_costo_entrega_propia(pedido, monto, actor):
    """Costo de la entrega propia de un pedido (Chema 2026-10-06): sin flota,
    salir "sin guía" no cuesta nada o cuesta una cantidad arbitraria que Mesa
    captura aquí (sin IVA). `monto` = Decimal/str ≥ 0, o None/"" para
    quitarlo (vuelve a $0). Finanzas lo reparte entre las guías internas
    LOCAL-* vivas del pedido como su costo; no cambia lo que se cobra al
    cliente. Auditado como costo_entrega_propia. Regresa el pedido."""
    if monto in (None, ""):
        nuevo = None
    else:
        try:
            nuevo = Decimal(str(monto).strip().replace(",", ""))
        except InvalidOperation:
            raise ValueError("El costo de entrega propia no es un número válido.") from None
        if nuevo < 0:
            raise ValueError("El costo de entrega propia no puede ser negativo.")
        nuevo = nuevo.quantize(Decimal("0.01"))
    anterior = pedido.costo_entrega_propia
    pedido.costo_entrega_propia = nuevo
    pedido.save(update_fields=["costo_entrega_propia", "actualizado"])
    registrar_evento(
        "pedido", pedido.pk, "costo_entrega_propia", actor=actor, cliente=pedido.cliente,
        delta={"de": str(anterior) if anterior is not None else None, "a": str(nuevo) if nuevo is not None else None},
        motivo="Costo de la entrega propia capturado en Mesa (no es cobro al cliente).",
    )
    return pedido


CARRIER_LOCAL = "local"
TIPO_FOTO_POD = "pod"


def guias_entrega_propia(pedido):
    """Guías internas "local" del pedido (una por caja; sin plan de cajas, una
    del pedido), vivas o entregadas, en orden de caja. Lee `guias.all()` para
    respetar el prefetch de las vistas."""
    return sorted(
        (g for g in pedido.guias.all() if g.carrier == CARRIER_LOCAL and g.es_activa),
        key=lambda g: (g.paquete.numero if g.paquete_id else 0, g.pk),
    )


def motivo_sin_pod(guia):
    """Por qué una guía NO admite POD todavía; "" si la caja está en reparto:
    guía interna "local" viva y sin entregar, con su caja ya fuera
    (DESPACHADO) o, sin plan de cajas, el pedido en la calle."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app

    pedido = guia.pedido
    if guia.carrier != CARRIER_LOCAL:
        return f"La guía {guia.numero} es de {guia.carrier}, no de entrega propia: la cierra el rastreo del carrier."
    if guia.estado == Guia.ENTREGADO:
        return f"La guía {guia.numero} ya está entregada."
    if not guia.es_activa:
        return f"La guía {guia.numero} está {guia.get_estado_display().lower()}: no se entrega."
    if guia.paquete_id:
        if guia.paquete.estado != Paquete.DESPACHADO:
            return f"La caja {guia.paquete.numero} de {pedido.folio} aún no sale a reparto (falta su manifiesto)."
    elif pedido.estado not in (Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.PARCIALMENTE_DESPACHADO):
        return f"{pedido.folio} no está en reparto (está {pedido.get_estado_display().lower()})."
    return ""


def registrar_pod_caja(guia, actor, foto=None, recibio="", reusar=None):
    """POD de entrega propia POR CAJA (Chema 2026-10-05: "un POD por paquete,
    aunque sea la misma imagen; sin verificación de edad, solo quién recibió
    y la foto"; es administrativo y vive en Mesa). `guia` = la guía interna
    "local" de la caja (sin plan de cajas, la del pedido); solo con
    motivo_sin_pod en blanco. Exige el nombre de quien recibió y una foto:
    archivo nuevo, o `reusar` = una EvidenciaFoto POD de otra caja del MISMO
    pedido (salieron juntas y la misma imagen las ampara) — cada caja queda
    con su propio registro, mismo archivo y misma huella. La guía pasa a
    ENTREGADO; el pedido a ENTREGADO solo cuando TODAS sus guías activas lo
    están (envios._estado_por_guias): una caja entregada no entrega el
    pedido. Evento pod_entrega_local (caja, guía, receptor, evidencia).
    Shopify best-effort en on_commit: evento DELIVERED del fulfillment de
    esa caja. Regresa la EvidenciaFoto."""
    from apps.envios.models import Guia  # lazy: modelo de otra app
    from apps.envios.services import _estado_por_guias  # lazy por contrato

    razon = motivo_sin_pod(guia)
    if razon:
        raise ValueError(razon)
    recibio = (recibio or "").strip()
    if not recibio:
        raise ValueError("Falta el nombre de quien recibió: sin él no se cierra la entrega.")
    pedido = guia.pedido
    if reusar is not None and (
        reusar.entidad != "entrega_local" or reusar.entidad_id != str(pedido.pk) or reusar.tipo != TIPO_FOTO_POD
    ):
        raise ValueError("La foto a reutilizar no es un POD de este pedido.")
    if foto is None and reusar is None:
        raise ValueError("Falta la foto de entrega: toma una nueva o reutiliza la de la caja anterior.")
    caja = guia.paquete
    quien = getattr(actor, "username", str(actor)) if actor is not None else ""
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        if reusar is not None:
            evidencia = EvidenciaFoto.objects.create(
                entidad="entrega_local", entidad_id=str(fresco.pk), tipo=TIPO_FOTO_POD,
                archivo=reusar.archivo.name, hash_sha256=reusar.hash_sha256, tomada_por=quien,
            )
        else:
            evidencia = EvidenciaFoto.objects.create(
                entidad="entrega_local", entidad_id=str(fresco.pk), tipo=TIPO_FOTO_POD, archivo=foto, tomada_por=quien,
            )
        donde = f"caja {caja.numero}" if caja is not None else "pedido"
        guia.transicionar(Guia.ENTREGADO, actor=actor, motivo=f"POD de entrega propia ({donde}): recibió {recibio}.")
        registrar_evento(
            "pedido", fresco.pk, "pod_entrega_local", actor=actor, cliente=fresco.cliente,
            delta={
                "caja": caja.numero if caja is not None else None, "guia": guia.numero,
                "receptor": recibio, "evidencia_id": evidencia.pk, "foto_reutilizada": reusar is not None,
            },
            motivo=f"Entrega propia, {donde}: recibió {recibio}.",
        )
        if _estado_por_guias(fresco) == "ENTREGADO" and fresco.estado != Pedido.ENTREGADO:
            fresco.transicionar(
                Pedido.ENTREGADO, actor=actor,
                motivo=f"POD de entrega propia: todas las cajas entregadas (la última la recibió {recibio}).",
            )
        ahora = timezone.now()

        def _shopify():
            try:
                from apps.integraciones.services import registrar_evento_fulfillment  # lazy
                registrar_evento_fulfillment(fresco, guia, Guia.ENTREGADO, descripcion=f"Entregado: recibió {recibio}", ts=ahora)
            except Exception:  # noqa: BLE001, S110 — best-effort: Shopify jamás frena una entrega
                pass
        transaction.on_commit(_shopify)
    pedido.estado = fresco.estado
    return evidencia


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
    (put-away en la zona de recepción, desde empaque si ESTA ola ya se empacó,
    desde vendible si sigue en carrito) y el resto libera su reserva; si hubo
    reingreso nace una OrdenEntrada tipo reingreso EN_RECEPCION con 0 recibidas:
    el piso escanea esas cajas a una tarima y al confirmarla el stock sale de
    la zona de recepción (2026-10-13); el pedido pasa a CANCELADO. Las guías activas se cancelan con el carrier fuera de la
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
                # Lote por pedido: vuelve exactamente lo anotado (posición y lote) y el lote viaja al put-away.
                estado_pick = PickLinea.EN_EMPAQUE if empacado else PickLinea.PICKEADO
                picks = list(linea.picks.filter(estado=estado_pick).order_by("pk"))
                slices = _fusionar_slices((p.ubicacion_id, p.lote_id, p.cantidad) for p in picks if p.ubicacion_id is not None or p.lote_id is not None)
                if slices:
                    reingresar_desde_pedido(linea.sku, pickeada, pedido.folio, actor, desde_empaque=empacado, slices=slices)
                else:
                    reingresar_desde_pedido(linea.sku, pickeada, pedido.folio, actor, desde_empaque=empacado)
                _mover_registros(linea, picks, pickeada, PickLinea.REGRESADO)
                reingreso.append((linea.sku, pickeada))
            resto = linea.con_stock - linea.cantidad_pickeada
            if resto > 0:
                liberar_reserva(linea.sku, resto, pedido.folio)
            campos = []
            if linea.reservada and not linea.cantidad_despachada:
                linea.reservada = False
                campos.append("reservada")
            if linea.cantidad_repuesta_reservada and linea.cantidad_repuesta_reservada > max(linea.cantidad_despachada - linea.cantidad, 0):
                # Repuestas apartadas que no salieron: su reserva se liberó arriba.
                linea.cantidad_repuesta_reservada = max(linea.cantidad_despachada - linea.cantidad, 0)
                campos.append("cantidad_repuesta_reservada")
            if campos:
                linea.save(update_fields=campos)
        orden = None
        if reingreso:
            ahora = timezone.now()
            orden = OrdenEntrada.objects.create(
                cliente=pedido.cliente, tipo=OrdenEntrada.TIPO_REINGRESO, pedido=pedido,
                estado=OrdenEntrada.EN_RECEPCION, fecha_compromiso=timezone.localdate(),
                ts_descarga_fin=ahora,
            )
            for sku, piezas in reingreso:
                LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=piezas)
            registrar_evento(
                "asn", orden.folio, "reingreso_creado", actor=actor, cliente=pedido.cliente,
                delta={"pedido": pedido.folio, "desde": "empaque" if empacado else "carrito",
                       "lineas": [{"sku": s.codigo, "cantidad": n} for s, n in reingreso]},
                motivo=f"Mercancía de {pedido.folio} de vuelta en la zona de recepción; el piso la escanea a una tarima.",
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
    _cancelar_fulfillments_en_bodega(pedido, actor)
    return pedido


def _cancelar_fulfillments_en_bodega(pedido, actor):
    """Fulfillment al llegar a Salida (Chema 2026-10-08): una caja cancelada en
    bodega puede tener ya su fulfillment en Shopify; se cancela para que sus
    líneas vuelvan a "por surtir" (integraciones.cancelar_fulfillments_en_bodega;
    las despachadas siguen el camino de la CAN). Best-effort, lazy."""
    try:
        from apps.integraciones.services import cancelar_fulfillments_en_bodega  # lazy
        cancelar_fulfillments_en_bodega(pedido, actor)
    except Exception:  # noqa: BLE001, S110 — best-effort: Shopify jamás frena una cancelación
        pass


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


def cajas_por_reingresar(pedido):
    """Cajas del pedido cuya mercancía puede volver y Mesa no ha decidido
    (Chema 2026-10-06, PED-00067: el reingreso es POR CAJA): despachadas, sin
    decisión, que el carrier regresó (guía en RETORNO) o, con cancelación
    tardía, todas las que salieron. [] si ninguna caja salió por caja (sin
    plan, o plan sin empacar por caja: salió entero y se decide por pedido).
    Lee `paquetes.all()` y `guias.all()` para respetar los prefetch de las
    vistas."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    cajas = []
    for caja in sorted(pedido.paquetes.all(), key=lambda c: c.numero):
        if caja.reingreso_estado != Paquete.REINGRESO_PENDIENTE:
            continue
        # Reposición cancelada ya pickeada/empacada (2026-10-20): la caja
        # cancelada guarda su contenido por decidir aunque nunca haya salido.
        if caja.estado == Paquete.CANCELADO:
            cajas.append(caja)
            continue
        if caja.estado != Paquete.DESPACHADO:
            continue
        # Contenido erróneo (2026-10-10): salió otro producto; la caja se
        # decide con su contenido real aunque el carrier no la haya regresado.
        # Regreso reportado (2026-10-21): envia "Redirected" o Mesa lo marcó.
        if pedido.cancelacion_tardia or caja.regresada or caja.contenido_erroneo or caja.regreso_reportado:
            cajas.append(caja)
    return cajas


def reingresos_por_decidir():
    """Pedidos con mercancía que puede volver y sin decisión de Mesa. Por CAJA
    (2026-10-06): alguna caja despachada sin decidir que el carrier regresó
    (guía en RETORNO) —aunque las demás se hayan entregado y el pedido esté
    ENTREGADO— o, con cancelación tardía, cualquiera que salió. Pedidos sin
    cajas despachadas (salió entero): RETORNADO por el carrier o cancelación
    tardía, decididos por pedido (aunque la CAN ya se haya resuelto y el
    pedido esté CANCELADO: la mercancía sigue por decidir)."""
    por_caja = Q(paquetes__estado="DESPACHADO", paquetes__reingreso_estado="") & (
        Q(paquetes__guias__estado="RETORNO") | Q(cancelacion_tardia=True) | Q(paquetes__contenido_erroneo=True)
        | Q(paquetes__regreso_reportado__isnull=False)
    )
    # Reposición cancelada ya empacada (2026-10-20): la caja CANCELADO guarda el contenido por decidir.
    por_caja = por_caja | Q(paquetes__estado="CANCELADO", paquetes__reingreso_estado="")
    # Sin cajas despachadas (salió entero: sin plan, o plan sin empacar por
    # caja): por pedido, como siempre.
    por_pedido = ~Q(paquetes__estado="DESPACHADO") & Q(reingreso_estado=Pedido.REINGRESO_PENDIENTE) & (
        Q(estado=Pedido.RETORNADO) | Q(cancelacion_tardia=True)
    )
    # Por caja no se mira el estado del pedido (Chema 2026-10-09, PED-00119):
    # una caja despachada y regresada sigue por decidir aunque el pedido se
    # haya reiniciado por una reposición y ande en PENDIENTE o en picking.
    por_pedido = por_pedido & Q(estado__in=ESTADOS_CON_MERCANCIA_FUERA + (Pedido.CANCELADO,))
    return (
        Pedido.objects.filter(por_caja | por_pedido)
        .select_related("cliente").order_by("actualizado").distinct()
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
    """(cajas por decidir) o ValueError. Con plan de cajas la decisión es por
    caja (las que el carrier regresó o, con cancelación tardía, las que
    salieron, sin decisión aún) y no depende del estado del pedido (Chema
    2026-10-09, PED-00119: reiniciado por reposición y en PENDIENTE, sus
    cajas regresadas se deciden igual); sin plan, por pedido como siempre."""
    if pedido.paquetes.filter(estado="DESPACHADO").exists():
        cajas = cajas_por_reingresar(pedido)
        if not cajas:
            raise ValueError(f"El pedido {pedido.folio} no tiene cajas regresadas por decidir (o ya tienen decisión).")
        return cajas
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
    return []


def _contenido_de_cajas(cajas):
    """[(sku, piezas)] de lo que viaja en esas cajas, en unidades de venta
    enteras (una media caja reempacada cuenta entera), sin kits virtuales."""
    import math
    from fractions import Fraction

    por_sku = {}
    for caja in cajas:
        for pl in caja.lineas.select_related("linea_pedido__sku", "sku_real"):
            # Producto erróneo (2026-10-10): regresa lo que de verdad viajó.
            sku = pl.sku_real or pl.linea_pedido.sku
            if sku.es_kit:
                continue
            por_sku[sku] = por_sku.get(sku, Fraction(0)) + Fraction(pl.cantidad, max(pl.fraccion_de, 1))
    return [(sku, math.ceil(u)) for sku, u in por_sku.items() if u > 0]


def _cerrar_decision(pedido, cajas, decision, actor, motivo_cierre):
    """Estampa la decisión en las cajas (o en el pedido si no tiene plan) y,
    cuando ya no queda ninguna caja por decidir, en el pedido; una
    cancelación tardía pasa entonces a CANCELADO."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    for caja in cajas:
        Paquete.objects.filter(pk=caja.pk).update(reingreso_estado=decision)
        caja.reingreso_estado = decision
    quedan = cajas_por_reingresar(pedido) if cajas else []
    if not quedan:
        pedido.reingreso_estado = decision
        pedido.save(update_fields=["reingreso_estado", "actualizado"])
        cerrar_cancelacion_tardia(pedido, actor, motivo_cierre)
    return quedan


def contenido_caja_por_pieza(caja):
    """[(linea, sku, unidades)] de lo que viaja en la caja en unidades de venta
    enteras (una media caja cuenta entera): producto erróneo → el que de
    verdad salió; un kit → sus componentes (todas si la caja llevaba el kit
    completo, proporcional si no), nunca el kit virtual."""
    import math
    from fractions import Fraction

    acumulado, orden = {}, []
    def _sumar(linea, sku, unidades):
        if linea.pk not in acumulado:
            orden.append(linea.pk)
            acumulado[linea.pk] = [linea, sku, Fraction(0)]
        acumulado[linea.pk][2] += unidades

    for pl in caja.lineas.select_related("linea_pedido__sku", "sku_real"):
        linea = pl.linea_pedido
        unidades = Fraction(pl.cantidad, max(pl.fraccion_de, 1))
        if linea.sku.es_kit:
            enteras = int(unidades)
            for hija in linea.componentes.select_related("sku"):
                _sumar(hija, hija.sku, Fraction(hija.cantidad if enteras >= linea.cantidad else hija.cantidad * enteras // max(linea.cantidad, 1)))
            continue
        _sumar(linea, pl.sku_real or linea.sku, unidades)
    return [(l, s, math.ceil(u)) for pk in orden for l, s, u in [acumulado[pk]] if u > 0]


def sobre_descontadas(linea):
    """Piezas de la línea que el kardex ya dio por salidas sin que su caja
    haya salido: `cantidad_despachada` menos lo que viaja en cajas
    DESPACHADO. El manifiesto por caja de antes restaba la línea entera con la
    primera caja (PED-00281, PED-00507); 0 cuando cuadra."""
    import math
    from fractions import Fraction

    fuera = sum(
        (Fraction(pl.cantidad, max(pl.fraccion_de, 1)) for pl in linea.en_paquetes.select_related("paquete") if pl.paquete.estado == "DESPACHADO"),
        Fraction(0),
    )
    return max(0, linea.cantidad_despachada - math.ceil(fuera))


def _recalcular_contadores(linea):
    """Contadores físicos desde las cajas que quedan (Chema 2026-10-21): lo
    despachado es lo que viaja en cajas que salieron; lo pickeado, eso más lo
    que sigue en cajas en bodega (empacadas o en empaque). Regresa
    {"antes": [...], "despues": [...]} para el evento."""
    import math
    from fractions import Fraction

    fuera = bodega = Fraction(0)
    for pl in linea.en_paquetes.select_related("paquete"):
        unidades = Fraction(pl.cantidad, max(pl.fraccion_de, 1))
        if pl.paquete.estado == "DESPACHADO":
            fuera += unidades
        elif pl.paquete.estado in ("EMPACADO", "EN_EMPAQUE"):
            bodega += unidades
    antes = [linea.cantidad_pickeada, linea.cantidad_despachada]
    linea.cantidad_despachada = math.ceil(fuera)
    linea.cantidad_pickeada = linea.cantidad_despachada + math.ceil(bodega)
    linea.save(update_fields=["cantidad_pickeada", "cantidad_despachada"])
    return {"antes": antes, "despues": [linea.cantidad_pickeada, linea.cantidad_despachada]}


def _mensaje_en_incidencia_abierta(pedido, texto):
    """Nota de sistema en la incidencia abierta del pedido (CAN/RET/RF/la que haya), best-effort."""
    try:
        from apps.incidencias.models import Incidencia, MensajeIncidencia  # lazy: modelos de otra app
        from apps.incidencias.services import responder  # lazy por contrato
    except ImportError:
        return
    abierta = (
        Incidencia.objects.filter(pedido=pedido, estado__in=Incidencia.ESTADOS_ABIERTOS)
        .order_by("-ts_apertura").first()
    )
    if abierta is not None:
        responder(abierta, "Torre", MensajeIncidencia.ROL_SISTEMA, texto[:1000])


def resolver_contenido_caja(pedido, caja, decisiones, actor, motivo=""):
    """Mesa decidió pieza por pieza qué pasa con el contenido de una caja por
    decidir (regresada por el carrier, regreso reportado, cancelación tardía,
    producto erróneo o reposición cancelada; Chema 2026-10-21). `decisiones`
    = [{"sku", "cantidad", "destino" bodega|cuarentena|merma, "ubicacion",
    "lote", "descontar", "motivo"}]; por producto deben sumar exactamente lo
    que viaja en la caja. Torre no adivina: ejecuta eso.

    Fuente por producto: si la caja salió (DESPACHADO) o la pieza ya está
    descontada de más (sobre_descontadas), reingresa como retorno
    (inventario.reingresar_piezas); si sigue en empaque se mueve desde
    empaque (inventario.regresar_piezas_de_empaque) restando primero lo
    anotado al escanear. Registros PickLinea → regresados; con la caja en
    bodega los contadores se recalculan desde las cajas que quedan;
    `reingreso_estado` = reingresado si algo volvió, no recuperado si nada
    (y, sin más cajas por decidir, el pedido cierra su decisión como hoy).
    Evento `reingreso_resuelto` y nota en la incidencia abierta."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.inventario.services import (  # lazy por contrato
        DESTINO_MERMA, regresar_piezas_de_empaque, reingresar_piezas,
    )

    if caja.pedido_id != pedido.pk:
        raise ValueError(f"La caja {caja.numero} no es de {pedido.folio}.")
    if caja.pk not in {c.pk for c in cajas_por_reingresar(pedido)}:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} no está por decidir.")
    contenido = {}
    for linea, sku, unidades in contenido_caja_por_pieza(caja):
        fila = contenido.setdefault(sku.pk, [linea, sku, 0])
        fila[2] += unidades
    grupos = {}
    for d in decisiones:
        sku = d.get("sku")
        if sku is None or sku.pk not in contenido:
            raise ValueError(f"{getattr(sku, 'codigo', 'ese producto')} no viaja en la caja {caja.numero}.")
        grupos.setdefault(sku.pk, []).append(d)
    for pk, (linea, sku, unidades) in contenido.items():
        suma = sum(int(d.get("cantidad") or 0) for d in grupos.get(pk, []))
        if suma != unidades:
            raise ValueError(f"{sku.codigo}: la caja {caja.numero} lleva {unidades} pieza(s) y decidiste {suma}. Decide todas, ni una más.")
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        caja = Paquete.objects.select_for_update().get(pk=caja.pk)
        salio = caja.estado == Paquete.DESPACHADO
        regreso_algo, detalle = False, []
        for pk, (linea, sku, unidades) in contenido.items():
            ds = grupos[pk]
            fuera_n = unidades if salio else min(unidades, sobre_descontadas(linea))
            fuera_dec, empaque_dec = [], []
            restante_fuera = fuera_n
            for d in ds:
                n = int(d.get("cantidad") or 0)
                if restante_fuera > 0 and n > 0:
                    tomo = min(n, restante_fuera)
                    fuera_dec.append({**d, "cantidad": tomo})
                    restante_fuera -= tomo
                    n -= tomo
                if n > 0:
                    empaque_dec.append({**d, "cantidad": n})
            if fuera_dec:
                reingresar_piezas(sku, fuera_dec, fresco.folio, actor)
            if empaque_dec:
                picks = list(linea.picks.filter(estado=PickLinea.EN_EMPAQUE).order_by("pk"))
                preferir = [(p.ubicacion_id, p.lote_id, p.cantidad) for p in picks if p.ubicacion_id is not None or p.lote_id is not None]
                regresar_piezas_de_empaque(sku, empaque_dec, fresco.folio, actor, preferir=preferir)
                _mover_registros(linea, picks, sum(int(d["cantidad"]) for d in empaque_dec), PickLinea.REGRESADO)
            if salio:
                picks_caja = list(linea.picks.filter(estado=PickLinea.DESPACHADO, caja=caja).order_by("pk"))
                if picks_caja:
                    _mover_registros(linea, picks_caja, min(unidades, sum(p.cantidad for p in picks_caja)), PickLinea.REGRESADO)
            regreso_algo = regreso_algo or any(d.get("destino") != DESTINO_MERMA for d in ds)
            detalle.append({
                "sku": sku.codigo, "fuera_del_kardex": fuera_n,
                "decisiones": [
                    {"cantidad": int(d.get("cantidad") or 0), "destino": d.get("destino"),
                     "ubicacion": getattr(d.get("ubicacion"), "codigo", None), "lote": getattr(d.get("lote"), "codigo", None),
                     "descontar": bool(d.get("descontar", True))}
                    for d in ds
                ],
            })
            if not salio and not linea.sku.es_kit:
                detalle[-1]["contadores"] = _recalcular_contadores(linea)
        decision = Paquete.REINGRESADO if regreso_algo else Paquete.NO_RECUPERADO
        _cerrar_decision(fresco, [caja], decision, actor, "Decisión pieza por pieza tomada en Mesa.")
        registrar_evento(
            "pedido", fresco.pk, "reingreso_resuelto", actor=actor, cliente=fresco.cliente,
            delta={"caja": caja.numero, "decision": decision, "detalle": detalle},
            motivo=(motivo or f"Caja {caja.numero}: contenido decidido pieza por pieza.")[:300],
        )
        resumen = "; ".join(
            f"{x['sku']}: " + ", ".join(f"{d['cantidad']} {d['destino']}" + (f" en {d['ubicacion']}" if d['ubicacion'] else "") for d in x["decisiones"])
            for x in detalle
        )
        _mensaje_en_incidencia_abierta(fresco, f"Caja {caja.numero} decidida en Mesa: {resumen}." + (f" Motivo: {motivo}" if motivo else ""))
    pedido.estado = fresco.estado
    return caja


def reportar_regreso_caja(pedido, caja, actor, motivo="", *, manual=True):
    """Una caja despachada regresa o va a regresar (Chema 2026-10-20, PED-00151:
    envia.com no siempre avisa). La caja queda "regreso reportado" y entra a
    Reingresos por decidir. `manual=True` (Mesa, "Registrar reingreso"): la
    guía viva pasa a RETORNO por el mismo camino que un retorno del carrier
    (envios.marcar_retorno_manual: RF y estado del pedido). `manual=False`
    (envia "Redirected"): solo la marca; la guía sigue viva y "entregado" la
    limpia. Regresa la caja."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app

    if caja.pedido_id != pedido.pk:
        raise ValueError(f"La caja {caja.numero} no es de {pedido.folio}.")
    if caja.estado != Paquete.DESPACHADO:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} no ha salido: no hay regreso que registrar.")
    if caja.reingreso_estado != Paquete.REINGRESO_PENDIENTE:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} ya tiene decisión: {caja.get_reingreso_estado_display().lower()}.")
    with transaction.atomic():
        if caja.regreso_reportado is None:
            caja.regreso_reportado = timezone.now()
            caja.save(update_fields=["regreso_reportado"])
        registrar_evento(
            "paquete", caja.pk, "regreso_reportado", actor=actor, cliente=pedido.cliente,
            delta={"pedido": pedido.folio, "caja": caja.numero, "origen": "mesa" if manual else "carrier"},
            motivo=(motivo or f"Caja {caja.numero} de {pedido.folio}: regreso reportado.")[:300],
        )
        if manual:
            guia = caja.guia_activa
            if guia is not None:
                from apps.envios.services import marcar_retorno_manual  # lazy por contrato

                marcar_retorno_manual(guia, actor, motivo=motivo or f"Caja {caja.numero} de {pedido.folio} regresó (Mesa)")
    return caja


def deshacer_reposicion_empacada(pedido, caja, seleccion, actor, incidencia=None, motivo=""):
    """Cancela una reposición aprobada cuya caja ya se pickeó o empacó (Chema
    2026-10-20, PED-00281): la caja pasa a CANCELADO (se queda en el
    expediente con fotos y guías), su guía comprada se cancela con el carrier
    y se reabren sus sellos; por línea bajan `cantidad_repuesta` y
    `cantidad_repuesta_reservada` (sin liberar reserva: las piezas ya se
    pickearon) y se quita su entrada de `origen_reposicion`; los contadores
    físicos se recalculan desde las cajas que quedan; si no queda nada por
    surtir ni cajas vivas en bodega el pedido vuelve al estado anterior a la
    reposición (_estado_antes_de_reponer). El contenido de la caja queda POR
    DECIDIR (Reingresos) hasta resolver_contenido_caja. El pedido no puede
    estar en manos de un operador. `seleccion` = [(línea, cantidad,
    caja_origen pk|None)] de lo que se cancela. Regresa el estado final."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app
    from apps.envios.services import cancelar_guia  # lazy por contrato

    folio_inc = getattr(incidencia, "folio", "") or ""
    with transaction.atomic():
        fresco = Pedido.objects.select_for_update().get(pk=pedido.pk)
        caja = Paquete.objects.select_for_update().get(pk=caja.pk)
        if caja.pedido_id != fresco.pk:
            raise ValueError(f"La caja {caja.numero} no es de {fresco.folio}.")
        if caja.estado not in (Paquete.PLANEADO, Paquete.EN_EMPAQUE, Paquete.EMPACADO):
            raise ValueError(f"La caja {caja.numero} de {fresco.folio} está {caja.get_estado_display().lower()}: una caja que ya salió se quita de salida primero.")
        if fresco.asignado_a_id and not fresco.detenido:
            raise ValueError(f"{fresco.folio} está en manos del piso: desasigna al operador o para el pedido antes de cancelar la reposición.")
        guia = caja.guia_activa
        canceladas = []
        if guia is not None:
            if guia.estado != Guia.GUIA_CREADA:
                raise ValueError(f"La guía {guia.numero} de la caja {caja.numero} ya salió ({guia.get_estado_display().lower()}).")
            cancelar_guia(guia, actor, motivo=f"Reposición cancelada: caja {caja.numero} de {fresco.folio}")
            canceladas.append(guia.numero)
        _reabrir_cierre(caja)
        caja.transicionar(
            Paquete.CANCELADO, actor=actor,
            motivo=(f"Reposición cancelada{f' ({folio_inc})' if folio_inc else ''}: la caja {caja.numero} se queda en el expediente y su contenido se decide en Reingresos.")[:300],
        )
        revertidas = []
        for item in seleccion:
            linea, cantidad = item[0], int(item[1] or 0)
            caja_pk = item[2] if len(item) > 2 else None
            caja_pk = caja_pk.pk if isinstance(caja_pk, Paquete) else caja_pk
            folio_item = item[3] if len(item) > 3 and item[3] else folio_inc
            if cantidad <= 0:
                continue
            linea.refresh_from_db()
            cantidad = min(cantidad, linea.cantidad_repuesta)
            if cantidad <= 0:
                continue
            linea.cantidad_repuesta -= cantidad
            linea.cantidad_repuesta_reservada = min(linea.cantidad_repuesta_reservada, linea.cantidad_repuesta)
            origen = list(linea.origen_reposicion or [])
            indice = _entrada_de_reposicion(origen, caja_pk, cantidad, folio_item)
            if indice is not None:
                origen.pop(indice)
            linea.origen_reposicion = origen
            linea.save(update_fields=["cantidad_repuesta", "cantidad_repuesta_reservada", "origen_reposicion"])
            revertidas.append([linea.sku.codigo, cantidad, Paquete.objects.filter(pk=caja_pk).values_list("numero", flat=True).first() if caja_pk else None])
        contadores = {}
        for pl in caja.lineas.select_related("linea_pedido__sku"):
            if not pl.linea_pedido.sku.es_kit:
                contadores[pl.linea_pedido.sku.codigo] = _recalcular_contadores(pl.linea_pedido)
        queda = any(l.por_surtir - l.cantidad_despachada > 0 for l in fresco.lineas.all())
        vivas = fresco.paquetes.filter(estado__in=(Paquete.PLANEADO, Paquete.EN_EMPAQUE, Paquete.EMPACADO)).exists()
        destino = fresco.estado
        if not queda and not vivas and fresco.estado in ESTADOS_REPOSICION_CANCELABLE:
            destino = _estado_antes_de_reponer(fresco)
            if destino != fresco.estado and destino in Pedido.TRANSICIONES.get(fresco.estado, set()):
                fresco.asignado_a = None
                fresco.save(update_fields=["asignado_a", "actualizado"])
                fresco.transicionar(
                    destino, actor=actor,
                    motivo=f"Reposición cancelada{f' ({folio_inc})' if folio_inc else ''}: el pedido sigue con lo que ya salió.",
                )
            else:
                destino = fresco.estado
        registrar_evento(
            "pedido", fresco.pk, "reposicion_cancelada", actor=actor, cliente=fresco.cliente,
            delta={
                "incidencia": folio_inc, "estado": destino, "caja": caja.numero, "guias_canceladas": canceladas,
                "lineas": revertidas, "contadores": contadores, "desde": "empacada",
            },
            motivo=(motivo or f"Se cancela la reposición de la caja {caja.numero} (ya pickeada); su contenido se decide en Reingresos.")[:300],
        )
    pedido.estado = fresco.estado
    return destino


# Estados desde los que un pedido vuelve atrás al cancelar una reposición ya pickeada.
ESTADOS_REPOSICION_CANCELABLE = ("PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA", "PARCIALMENTE_DESPACHADO")


def registrar_reingreso(pedido, actor, motivo=""):
    """Mesa: la mercancía que salió está de vuelta físicamente. Crea la
    OrdenEntrada tipo reingreso ANUNCIADA con el contenido de las CAJAS por
    decidir (regresadas por el carrier o, con cancelación tardía, las que
    salieron); sin plan de cajas, con lo despachado por línea. El piso la
    recibe (ok → put-away, dañado → cuarentena) y la ubica. Una cancelación
    tardía pasa a CANCELADO cuando ya no queda caja por decidir; un RETORNADO
    se queda así. Otra caja que regrese después vuelve a aparecer por decidir."""
    from apps.inventario.models import LineaASN, OrdenEntrada  # lazy: modelo de otra app

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
        cajas = _validar_decision_reingreso(pedido)
        if cajas:
            lineas = _contenido_de_cajas(cajas)
        else:
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
        _cerrar_decision(pedido, cajas, Pedido.REINGRESADO, actor, "Cancelación tardía: la mercancía regresa como reingreso.")
        registrar_evento(
            "asn", orden.folio, "reingreso_creado", actor=actor, cliente=pedido.cliente,
            delta={"pedido": pedido.folio, "desde": "retorno", "cajas": [c.numero for c in cajas],
                   "lineas": [{"sku": s.codigo, "cantidad": n} for s, n in lineas]},
            motivo=motivo or f"Mercancía de {pedido.folio} de vuelta; el piso la recibe y ubica.",
        )
    return orden


def marcar_no_recuperado(pedido, actor, motivo=""):
    """Mesa: la mercancía que salió no volverá (las cajas por decidir, o el
    pedido entero sin plan). Sin movimiento de stock (ya salió del kardex en
    el manifiesto); queda el evento y, cuando ya no hay caja por decidir, se
    resuelven las incidencias CAN/RF abiertas y una cancelación tardía pasa a
    CANCELADO."""
    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().get(pk=pedido.pk)
        cajas = _validar_decision_reingreso(pedido)
        quedan = _cerrar_decision(pedido, cajas, Pedido.NO_RECUPERADO, actor, "Cancelación tardía: inventario no recuperado.")
        registrar_evento(
            "pedido", pedido.pk, "inventario_no_recuperado", actor=actor, cliente=pedido.cliente,
            delta={"cajas": [c.numero for c in cajas]},
            motivo=motivo or "Mesa dio por perdida la mercancía del pedido.",
        )
        if quedan:
            return pedido
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
    for linea in pedido.lineas.filter(Q(reservada=True) | Q(cantidad_repuesta_reservada__gt=0)).select_related("sku"):
        if linea.sku.es_kit:
            continue  # reservada=True del kit es bookkeeping: no hay RESERVADO real
        if linea.con_stock > 0:
            liberar_reserva(linea.sku, linea.con_stock, pedido.folio)
        linea.reservada = False
        linea.cantidad_repuesta_reservada = 0
        linea.save(update_fields=["reservada", "cantidad_repuesta_reservada"])


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
