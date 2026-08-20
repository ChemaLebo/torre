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
from django.utils import timezone

from apps.core.models import EventoAuditoria, EvidenciaFoto
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


def _abrir_incidencia_faltante(pedido, faltantes):
    """Sin stock suficiente al dar de alta el pedido → incidencia FAL automática."""
    texto = "Faltante al ingerir la orden: " + "; ".join(faltantes)
    try:
        from apps.incidencias.services import abrir_incidencia  # lazy
    except ImportError:
        pass
    else:
        abrir_incidencia(pedido.cliente, "FAL", "auto", pedido=pedido, texto=texto)


def _planificar_best_effort(pedido):
    """División de envío (≤20 kg, optimizada por costo) planificada desde el alta.

    Best-effort: sin plan no se detiene el alta; generar_guias replanifica.

    Sale en transaction.on_commit: cotizar un lane frío pega a la API real de
    Envia (varios segundos) y el alta corre dentro de un atomic con locks de
    Saldo tomados — otro pedido del mismo SKU (u operador pickeando) quedaría
    esperando detrás de un HTTP externo. Primero el commit, luego el plan.
    """
    def _planificar():
        try:
            from apps.envios.cotizador import planificar_envio  # lazy
            planificar_envio(pedido)
        except (ImportError, ValueError):
            pass
    transaction.on_commit(_planificar)


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

            piezas = sum(linea.cantidad for linea in pedido.lineas.all())
            destino = (
                str((pedido.direccion or {}).get("city") or "").strip()
                or pedido.cp or "sin destino"
            )
            if pedido.corte_vigente_al_ingreso:
                corte = pedido.corte_vigente_al_ingreso.strftime("%H:%M")
            else:
                corte = str(settings.TORRE["CORTE_CONTRACTUAL"])
            push.enviar_push_a_rol(
                "piso",
                f"📦 Nuevo pedido {pedido.folio}",
                f"{piezas} pzas · {destino} · corte {corte}",
                url="/piso/picking/",
            )
        except Exception:
            pass
    transaction.on_commit(_enviar)


# ── Ingesta desde Shopify ──

@transaction.atomic
def ingerir_pedido_shopify(tienda, payload, origen="webhook"):
    """Upsert idempotente por (tienda, shopify_order_id).

    Orden nueva: crea Pedido + líneas, estampa corte vigente, calcula es_local
    por CP contra la zona local CDMX (bodega 01780) y reserva stock por línea vía
    inventario.reservar. Sin stock suficiente → el pedido queda PENDIENTE con
    incidencia_activa y se abre incidencia FAL (lazy).
    Orden repetida: refresca datos de contacto/dirección; NO duplica ni
    re-reserva. Orden con cancelled_at → pasa por la matriz de cancelación.
    Orden NUEVA que llega ya fulfilled → NO se ingiere (solo evento): fue
    atendida fuera de Torre (histórico tocado, suscripción auto-fulfilled) y
    crearla reservaría stock por trabajo que nadie va a hacer.
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
    if (payload.get("fulfillment_status") or "") == "fulfilled":
        registrar_evento(
            "pedido", shopify_order_id, "ingesta_omitida_fulfilled", actor=origen,
            cliente=tienda.cliente, delta={"origen": origen},
            motivo="Orden nueva pero ya fulfilled en Shopify: atendida fuera de Torre.",
        )
        return None
    return _crear_pedido_nuevo(tienda, payload, origen, shopify_order_id, cancelada)


def _actualizar_pedido_existente(pedido, payload, origen, cancelada):
    """Rama idempotente del upsert: solo refresca datos blandos."""
    campos = []
    direccion = payload.get("shipping_address")
    if direccion:
        pedido.direccion = direccion
        campos.append("direccion")
        cp = str(direccion.get("zip") or "").strip()
        if cp and cp != pedido.cp:
            pedido.cp = cp
            pedido.es_local = _es_local(cp)
            campos += ["cp", "es_local"]
    nombre, tel, email = _datos_comprador(payload)
    for campo, valor in [("comprador_nombre", nombre), ("comprador_tel", tel), ("comprador_email", email)]:
        if valor and getattr(pedido, campo) != valor:
            setattr(pedido, campo, valor)
            campos.append(campo)
    nota = payload.get("note") or ""
    if nota and nota != pedido.nota_regalo:
        pedido.nota_regalo = nota
        campos.append("nota_regalo")
    if campos:
        pedido.save(update_fields=campos + ["actualizado"])
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


def _crear_pedido_nuevo(tienda, payload, origen, shopify_order_id, cancelada):
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    cliente = tienda.cliente
    direccion = payload.get("shipping_address") or payload.get("billing_address") or {}
    cp = str(direccion.get("zip") or "").strip()
    nombre, tel, email = _datos_comprador(payload)
    try:
        valor_declarado = Decimal(str(payload.get("total_price") or "0"))
    except InvalidOperation:
        valor_declarado = Decimal("0")

    pedido = Pedido.objects.create(
        tienda=tienda,
        cliente=cliente,
        shopify_order_id=shopify_order_id,
        origen=origen,
        comprador_nombre=nombre,
        comprador_tel=tel,
        comprador_email=email,
        direccion=direccion,
        cp=cp,
        es_local=_es_local(cp),
        valor_declarado=valor_declarado,
        nota_regalo=payload.get("note") or "",
        corte_vigente_al_ingreso=_corte_contractual(),
        estado=Pedido.PENDIENTE,
    )

    faltantes = []
    peso_esperado = 0
    for item in payload.get("line_items") or []:
        codigo = str(item.get("sku") or "").strip()
        try:
            cantidad = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            cantidad = 0
        if cantidad <= 0:
            continue
        sku = SKU.objects.filter(cliente=cliente, codigo=codigo).first() if codigo else None
        if sku is None:
            faltantes.append(f"SKU desconocido: {codigo or item.get('title', '?')} × {cantidad}")
            continue
        linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=cantidad)
        peso_esperado += (sku.peso_gr or 0) * cantidad
        if cancelada:
            continue  # orden que llega ya cancelada: no se aparta stock
        if not _reservar_linea(linea):
            faltantes.append(f"Sin stock suficiente: {sku.codigo} × {cantidad}")

    pedido.peso_esperado_gr = peso_esperado
    campos = ["peso_esperado_gr"]
    if faltantes and not cancelada:
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
        linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=cantidad)
        peso_esperado += (sku.peso_gr or 0) * cantidad
        if not _reservar_linea(linea):
            faltantes.append(f"Sin stock suficiente: {sku.codigo} × {cantidad}")

    pedido.peso_esperado_gr = peso_esperado
    campos = ["peso_esperado_gr"]
    if faltantes:
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
    """PENDIENTE → EN_PICKING. La ola del piso empieza aquí."""
    pedido.transicionar(Pedido.EN_PICKING, actor=actor, motivo="Inicio de picking")
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


# ── Empaque ──

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
        incompletas = [l for l in fresco.lineas.all() if l.cantidad_pickeada < l.cantidad]
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
            raise ValueError("Captura el peso de la báscula en gramos antes de empacar.")
        if peso_real <= 0:
            raise ValueError("Captura el peso de la báscula en gramos antes de empacar.")
        tolerancia_pct = float(settings.TORRE["TOLERANCIA_PESO_PCT"])
        esperado = fresco.peso_esperado_gr or 0
        if esperado > 0 and not peso_ya_verificado:
            diferencia_pct = abs(peso_real - esperado) * 100.0 / esperado
            if diferencia_pct > tolerancia_pct:
                raise ValueError(
                    f"El peso no cuadra: se esperaban {esperado} g y la báscula marca {peso_real} g "
                    f"({diferencia_pct:.1f}% de diferencia; la tolerancia es ±{tolerancia_pct:g}%). "
                    "Revisa el contenido antes de cerrar la caja."
                )

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
        for linea in fresco.lineas.all():
            if linea.cantidad_pickeada:
                confirmar_pick(linea.sku, linea.cantidad_pickeada, fresco.folio)

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


@transaction.atomic
def empacar_caja(paquete, actor, peso_real_gr, foto_contenido):
    """Empaque POR CAJA (wizard del carril único): peso contra SU plan + foto contenido.

    Valida el peso de la báscula contra el plan de la caja
    (paquete.peso_kg ± settings.TORRE["TOLERANCIA_PESO_PCT"], mismo criterio
    que empacar), guarda Paquete.peso_real_gr, adjunta la foto de contenido
    al pedido (el evento lleva el número de caja) y transiciona el paquete
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
    if pedido.estado != Pedido.EN_PICKING:
        raise ValueError(
            f"El pedido {pedido.folio} no se puede empacar: está {pedido.get_estado_display()} "
            "y el empaque solo aplica a pedidos en picking."
        )
    incompletas = [l for l in pedido.lineas.all() if l.cantidad_pickeada < l.cantidad]
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
    tolerancia_pct = float(settings.TORRE["TOLERANCIA_PESO_PCT"])
    esperado = int(fresco.peso_kg * 1000) if fresco.peso_kg else 0
    if esperado > 0:
        diferencia_pct = abs(peso_real - esperado) * 100.0 / esperado
        if diferencia_pct > tolerancia_pct:
            raise ValueError(
                f"El peso de la caja {fresco.numero} no cuadra: el plan marca {esperado} g "
                f"y la báscula {peso_real} g ({diferencia_pct:.1f}% de diferencia; "
                f"la tolerancia es ±{tolerancia_pct:g}%). "
                "Revisa el contenido de ESA caja antes de cerrarla."
            )
    if foto_contenido is None:
        raise ValueError(
            f"Toma la foto del contenido de la caja {fresco.numero} antes de confirmarla."
        )

    evidencia = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo=TIPO_FOTO_CONTENIDO,
        archivo=foto_contenido, tomada_por=_actor_nombre(actor),
    )
    fresco.peso_real_gr = peso_real
    fresco.save(update_fields=["peso_real_gr"])
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
    return fresco


@transaction.atomic
def cerrar_caja(paquete, actor, foto_caja_cerrada):
    """Cierre de caja con evidencia REAL: la caja cerrada CON su etiqueta pegada.

    Solo aplica a cajas EMPACADO/DESPACHADO y con guía (antes de la guía la
    etiqueta no existe — por eso la foto ya no se pide en empacar). Adjunta
    EvidenciaFoto tipo "caja_cerrada" ligada al pedido; el evento lleva el
    número de caja. Una caja no se cierra dos veces: el candado (evento
    caja_cerrada_con_evidencia) se revisa con el paquete bajo
    select_for_update — dos POSTs concurrentes se serializan.
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
    if EventoAuditoria.objects.filter(
        entidad="paquete", entidad_id=str(fresco.pk), accion="caja_cerrada_con_evidencia",
    ).exists():
        raise ValueError(
            f"La caja {fresco.numero} de {pedido.folio} ya tiene su foto de cierre."
        )

    evidencia = EvidenciaFoto.objects.create(
        entidad="pedido", entidad_id=str(pedido.pk), tipo=TIPO_FOTO_CIERRE,
        archivo=foto_caja_cerrada, tomada_por=_actor_nombre(actor),
    )
    registrar_evento(
        "paquete", fresco.pk, "caja_cerrada_con_evidencia", actor=actor,
        cliente=pedido.cliente,
        delta={"pedido": pedido.folio, "caja": fresco.numero, "evidencia_id": evidencia.pk},
        motivo=f"Caja {fresco.numero} de {pedido.folio} cerrada con la etiqueta pegada.",
    )
    return fresco


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


def despachar_a_corral(pedido, actor):
    """Guía + impresión de etiquetas en el MISMO POST del empaque (carril único).

    Encadena generar_guia (idempotente; transiciona a GUIA_GENERADA) y por
    cada guía activa manda su etiqueta a la térmica BEST-EFFORT: una falla de
    impresora se acumula en `mensajes` y JAMÁS revierte la guía (la
    reimpresión vive en Salida). Regresa {"guias": [...], "mensajes": [...]}.
    """
    generar_guia(pedido)
    from apps.envios.models import Guia  # lazy: modelo de otra app
    guias = list(
        pedido.guias.exclude(estado__in=list(Guia.ESTADOS_INACTIVOS)).order_by("id")
    )
    from apps.piso.etiquetas import imprimir_etiqueta  # lazy por contrato
    mensajes = []
    for guia in guias:
        try:
            mensajes.append(imprimir_etiqueta(guia))
        except Exception as exc:  # best-effort: la guía ya existe y NUNCA se revierte
            mensajes.append(f"No se imprimió la etiqueta de la guía {guia.numero}: {exc}")
    registrar_evento(
        "pedido", pedido.pk, "despachado_a_corral", actor=actor, cliente=pedido.cliente,
        delta={"guias": [g.numero for g in guias], "mensajes_impresion": mensajes},
        motivo="Guía(s) e impresión encadenadas al empaque: el pedido va directo a su corral.",
    )
    return {"guias": guias, "mensajes": mensajes}


def marcar_recolectado(pedido, actor):
    """GUIA_GENERADA → RECOLECTADO: escaneo de salida + manifiesto.

    Despacha el inventario (en_empaque → salida) y dispara la plantilla B
    ("en camino") — SOLO aquí: jamás se avisa de un paquete que sigue en bodega.
    Todo el efecto de dominio (kardex + transición) va en UNA transacción:
    una línea que falle revierte completo — jamás kardex a medias con el
    pedido irrecuperable. La plantilla B sale ya con el commit hecho.
    """
    if pedido.estado != Pedido.GUIA_GENERADA:
        raise ValueError(
            f"El pedido {pedido.folio} no tiene guía lista (está {pedido.get_estado_display()}); "
            "no se puede marcar recolectado."
        )
    from apps.inventario.services import despachar  # lazy
    with transaction.atomic():
        for linea in pedido.lineas.all():
            if linea.cantidad_pickeada:
                despachar(linea.sku, linea.cantidad_pickeada, pedido.folio)
        pedido.transicionar(
            Pedido.RECOLECTADO, actor=actor,
            motivo="Escaneo de salida + manifiesto firmado (RECOLECTADO autoritativo).",
        )
    try:
        from apps.mensajeria.services import enviar_en_camino  # lazy — plantilla B: SOLO aquí
    except ImportError:
        pass
    else:
        enviar_en_camino(pedido)

    # Hermano del "va en camino": el fulfillment en Shopify sale del MISMO
    # momento canónico (correo nativo de envío + Fulfilled en el admin del
    # cliente). Best-effort total en on_commit: Shopify jamás bloquea un
    # manifiesto.
    def _fulfillment():
        try:
            from apps.integraciones.services import marcar_fulfillment  # lazy
            marcar_fulfillment(pedido)
        except Exception:
            pass
    transaction.on_commit(_fulfillment)
    return pedido


# ── Cancelación ──

def cancelar(pedido, actor, motivo=""):
    """Matriz de cancelación por estado (BLUEPRINT §2.3).

    - PENDIENTE: libera reservas y cancela directo.
    - EN_PICKING / EMPACADO / GUIA_GENERADA: → CANCELACION_PENDIENTE con
      tarea de restock para el piso (se cierra con confirmar_restock).
    - Ya despachado (RECOLECTADO / EN_TRANSITO / ...): el paquete ya salió →
      se abre incidencia CAN (cancelación tardía), el estado no cambia.
    - Terminales: ValueError.
    """
    estado = pedido.estado
    if estado == Pedido.PENDIENTE:
        _liberar_reservas(pedido)
        pedido.transicionar(Pedido.CANCELADO, actor=actor, motivo=motivo or "Cancelación directa")
    elif estado in (Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
        pedido.transicionar(
            Pedido.CANCELACION_PENDIENTE, actor=actor,
            motivo=motivo or "Cancelación con mercancía en proceso",
        )
        registrar_evento(
            "pedido", pedido.pk, "restock_pendiente", actor=actor, cliente=pedido.cliente,
            delta={"lineas": pedido.lineas.count()},
            motivo="Tarea de restock: regresar la mercancía a su ubicación y confirmar en piso.",
        )
    elif estado in (
        Pedido.RECOLECTADO, Pedido.EN_TRANSITO,
        Pedido.PARCIALMENTE_DESPACHADO, Pedido.ENTREGA_PRESUNTA,
    ):
        pedido.incidencia_activa = True
        pedido.save(update_fields=["incidencia_activa", "actualizado"])
        registrar_evento(
            "pedido", pedido.pk, "cancelacion_tardia", actor=actor, cliente=pedido.cliente,
            motivo=motivo or "Cancelación solicitada con el paquete ya despachado.",
        )
        try:
            from apps.incidencias.services import abrir_incidencia  # lazy
        except ImportError:
            pass
        else:
            texto = (
                f"Cancelación tardía: el pedido {pedido.folio} ya salió de bodega "
                f"({pedido.get_estado_display()}). {motivo}".strip()
            )
            abrir_incidencia(pedido.cliente, "CAN", "auto", pedido=pedido, texto=texto)
    else:
        raise ValueError(
            f"No se puede cancelar el pedido {pedido.folio}: está {pedido.get_estado_display()}."
        )
    return pedido


def confirmar_restock(pedido, actor, motivo=""):
    """Cierra la tarea de restock: CANCELACION_PENDIENTE → CANCELADO.

    Regresa el stock según hasta dónde llegó el pedido: si ya se había
    empacado (picks confirmados en inventario), lo pickeado reingresa vía
    retornar; lo aún reservado se libera.
    """
    if pedido.estado != Pedido.CANCELACION_PENDIENTE:
        raise ValueError(
            f"El pedido {pedido.folio} no tiene cancelación pendiente "
            f"(está {pedido.get_estado_display()})."
        )
    from apps.inventario.services import liberar_reserva, retornar  # lazy
    empacado = pedido.ts_empacado is not None
    for linea in pedido.lineas.all():
        pickeada = linea.cantidad_pickeada
        if empacado and pickeada:
            # confirmar_pick ya movió esto a en_empaque: reingresa como retorno.
            retornar(linea.sku, pickeada, pedido.folio, actor)
            resto = linea.cantidad - pickeada
        else:
            # Nada confirmado en inventario: todo sigue reservado.
            resto = linea.cantidad
        if resto > 0 and linea.reservada:
            liberar_reserva(linea.sku, resto, pedido.folio)
        if linea.reservada:
            linea.reservada = False
            linea.save(update_fields=["reservada"])
    pedido.transicionar(
        Pedido.CANCELADO, actor=actor,
        motivo=motivo or "Restock confirmado en piso; cancelación cerrada.",
    )
    return pedido


def _liberar_reservas(pedido):
    """Libera en inventario las líneas que sí alcanzaron reserva."""
    from apps.inventario.services import liberar_reserva  # lazy
    for linea in pedido.lineas.filter(reservada=True):
        liberar_reserva(linea.sku, linea.cantidad, pedido.folio)
        linea.reservada = False
        linea.save(update_fields=["reservada"])


# ── Job: entregas presuntas ──

def cerrar_entregas_presuntas(dias=None):
    """Pedidos EN_TRANSITO sin evento en N días → ENTREGA_PRESUNTA.

    Ningún pedido queda "EN TRÁNSITO para siempre" (BLUEPRINT §1.4): el
    cierre queda documentado para confirmar con el comprador. Idempotente:
    correrlo dos veces no re-cierra nada. N sale de
    settings.TORRE["ENTREGA_PRESUNTA_DIAS"] (default 7).
    """
    if dias is None:
        dias = int(settings.TORRE.get("ENTREGA_PRESUNTA_DIAS", 7))
    limite = timezone.now() - timedelta(days=dias)
    cerrados = []
    for pedido in Pedido.objects.filter(estado=Pedido.EN_TRANSITO):
        ultimo_evento = (
            EventoAuditoria.objects
            .filter(entidad="pedido", entidad_id=str(pedido.pk))
            .order_by("-ts")
            .first()
        )
        referencia = ultimo_evento.ts if ultimo_evento else (pedido.ts_en_transito or pedido.creado)
        if referencia is not None and referencia <= limite:
            pedido.transicionar(
                Pedido.ENTREGA_PRESUNTA,
                motivo=(
                    f"Sin evento en {dias} días: cierre presunto documentado, "
                    "confirmar entrega con el comprador."
                ),
            )
            cerrados.append(pedido)
    return cerrados
