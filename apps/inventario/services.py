"""Servicios de inventario: la ÚNICA puerta de entrada y salida del stock.

Contrato (CONVENTIONS.md §inventario). Ningún módulo toca Saldo/Movimiento
directo: todo pasa por aquí para que kardex, auditoría y push a Shopify cuadren.

Semántica clave (ver models.py): `reservado` es una capa sobre
`ubicado_vendible` — la unidad reservada sigue contada en su saldo vendible.
    disponible = suma(ubicado_vendible) − suma(reservado) − buffer del cliente
"""
from datetime import date

from django.conf import settings
from django.db import transaction
from django.db.models import F, Max, Sum
from django.utils import timezone

from apps.catalogo.models import Lote, Ubicacion
from apps.core.services import registrar_evento

from .models import Ajuste, Conteo, Movimiento, OrdenEntrada, Saldo, TareaConteo

# ─────────────────────────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────────────────────────


def _actor_str(actor):
    if actor is None:
        return ""
    if hasattr(actor, "username"):
        return actor.username
    return str(actor)


def _mov(sku, tipo, delta, *, lote=None, origen="", destino="", referencia="", actor=""):
    """Escribe una línea del kardex (append-only)."""
    return Movimiento.objects.create(
        sku=sku, lote=lote, tipo=tipo, delta=delta,
        estado_origen=origen, estado_destino=destino,
        referencia=str(referencia), actor=_actor_str(actor),
    )


def _suma(sku, estado):
    total = Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"]
    return total or 0


def _caducidades(saldos):
    """Mapa {lote_id: fecha_caducidad} para ordenar FEFO sin joins bajo lock."""
    ids = {s.lote_id for s in saldos if s.lote_id}
    if not ids:
        return {}
    return dict(Lote.objects.filter(pk__in=ids).values_list("pk", "fecha_caducidad"))


def _clave_fefo(saldo, caducidades):
    cad = caducidades.get(saldo.lote_id)
    return (cad or date.max, saldo.pk)


def _restar(saldos_ordenados, cantidad):
    """Resta `cantidad` recorriendo la lista en orden; borra filas que quedan en cero.

    Regresa cuánto logró restar (el llamador ya validó que alcanza).
    """
    restante = cantidad
    for saldo in saldos_ordenados:
        if restante == 0:
            break
        if saldo.cantidad <= 0:
            continue
        tomo = min(saldo.cantidad, restante)
        saldo.cantidad -= tomo
        if saldo.cantidad == 0:
            saldo.delete()
        else:
            saldo.save(update_fields=["cantidad"])
        restante -= tomo
    return cantidad - restante


def _incrementar(sku, ubicacion_id, lote_id, estado, cantidad):
    """Suma cantidad a un saldo (lo crea si no existe). Usa F() para no pisar concurrentes."""
    saldo, creado = Saldo.objects.get_or_create(
        sku=sku, ubicacion_id=ubicacion_id, lote_id=lote_id, estado=estado,
        defaults={"cantidad": cantidad},
    )
    if not creado:
        Saldo.objects.filter(pk=saldo.pk).update(cantidad=F("cantidad") + cantidad)
        saldo.refresh_from_db(fields=["cantidad"])
    return saldo


def _notificar_cambio_disponible(sku):
    """Encola el push de inventario a Shopify. Lazy: integraciones puede no existir aún."""
    try:
        from apps.integraciones.services import encolar_push_inventario
    except ImportError:
        return None
    return encolar_push_inventario(sku)


def _reintentar_pendientes(sku):
    """Al ENTRAR stock (putaway, ajuste, liberación, restock): reintenta las
    reservas pendientes de pedidos que nacieron sin stock. En on_commit: la
    reserva toma locks de Saldo y la operación que dispara trae los suyos.
    Jamás desde reservar/pick/salida (ahí el stock SALE, no entra)."""
    def _correr():
        try:
            from apps.pedidos.services import reintentar_reservas_sku  # lazy por contrato
        except ImportError:
            return
        reintentar_reservas_sku(sku)
    transaction.on_commit(_correr)


def _ubicacion_tipo(*tipos):
    """Primera ubicación activa de los tipos dados, en orden de preferencia."""
    for tipo in tipos:
        ubic = Ubicacion.objects.filter(tipo=tipo, activo=True).order_by("codigo").first()
        if ubic is not None:
            return ubic
    return None


def _validar_cantidad(cantidad, accion):
    if int(cantidad) <= 0:
        raise ValueError(f"La cantidad a {accion} debe ser mayor a cero.")
    return int(cantidad)


# ─────────────────────────────────────────────────────────────────────────────
# Contrato: disponible / reservar / liberar / pick / despacho
# ─────────────────────────────────────────────────────────────────────────────


def disponible(sku):
    """Disponible para vender = ubicado_vendible − reservado − buffer del cliente."""
    vendible = _suma(sku, Saldo.UBICADO_VENDIBLE)
    reservado = _suma(sku, Saldo.RESERVADO)
    return vendible - reservado - (sku.cliente.buffer_stock or 0)


def reservar(sku, cantidad, referencia):
    """Reserva atómica (SELECT FOR UPDATE): jamás check-then-act.

    Dos órdenes simultáneas por la última unidad: una reserva, la otra recibe
    False y pasa al flujo de faltante. Regresa True si la reserva quedó.
    """
    cantidad = _validar_cantidad(cantidad, "reservar")
    with transaction.atomic():
        saldos = list(
            Saldo.objects.select_for_update().filter(
                sku=sku, estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO],
            )
        )
        vendibles = [s for s in saldos if s.estado == Saldo.UBICADO_VENDIBLE]
        reservados = {(s.ubicacion_id, s.lote_id): s for s in saldos if s.estado == Saldo.RESERVADO}
        total_vendible = sum(s.cantidad for s in vendibles)
        total_reservado = sum(s.cantidad for s in reservados.values())
        buffer_cliente = sku.cliente.buffer_stock or 0
        if total_vendible - total_reservado - buffer_cliente < cantidad:
            return False

        # Asignación FEFO: reservamos contra el lote que caduca primero.
        caducidades = _caducidades(vendibles)
        restante = cantidad
        for saldo_v in sorted(vendibles, key=lambda s: _clave_fefo(s, caducidades)):
            if restante == 0:
                break
            clave = (saldo_v.ubicacion_id, saldo_v.lote_id)
            capa = reservados.get(clave)
            libre = saldo_v.cantidad - (capa.cantidad if capa else 0)
            if libre <= 0:
                continue
            tomo = min(libre, restante)
            if capa is not None:
                capa.cantidad += tomo
                capa.save(update_fields=["cantidad"])
            else:
                reservados[clave] = Saldo.objects.create(
                    sku=sku, ubicacion_id=saldo_v.ubicacion_id, lote_id=saldo_v.lote_id,
                    estado=Saldo.RESERVADO, cantidad=tomo,
                )
            restante -= tomo
        if restante > 0 and vendibles:
            # Deriva rara (capa mayor que su vendible por ajustes): el total sí
            # alcanza; anclamos el remanente al primer vendible FEFO.
            ancla = sorted(vendibles, key=lambda s: _clave_fefo(s, caducidades))[0]
            _incrementar(sku, ancla.ubicacion_id, ancla.lote_id, Saldo.RESERVADO, restante)
            restante = 0

        _mov(
            sku, Movimiento.RESERVA, cantidad,
            origen=Saldo.UBICADO_VENDIBLE, destino=Saldo.RESERVADO, referencia=referencia,
        )
        registrar_evento(
            "sku", sku.codigo, "reserva", cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia)},
        )
    _notificar_cambio_disponible(sku)
    return True


def liberar_reserva(sku, cantidad, referencia):
    """Libera reserva (cancelación pre-pick). El stock nunca dejó el anaquel."""
    cantidad = _validar_cantidad(cantidad, "liberar")
    with transaction.atomic():
        reservados = list(
            Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.RESERVADO)
        )
        total = sum(s.cantidad for s in reservados)
        if total < cantidad:
            raise ValueError(
                f"No puedo liberar {cantidad} de {sku.codigo}: solo hay {total} reservadas."
            )
        caducidades = _caducidades(reservados)
        # Liberamos primero lo que caduca al último (conservamos el FEFO de lo reservado).
        orden = sorted(reservados, key=lambda s: _clave_fefo(s, caducidades), reverse=True)
        _restar(orden, cantidad)
        _mov(
            sku, Movimiento.RESERVA, -cantidad,
            origen=Saldo.RESERVADO, destino=Saldo.UBICADO_VENDIBLE, referencia=referencia,
        )
        registrar_evento(
            "sku", sku.codigo, "liberar_reserva", cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia)},
        )
    _notificar_cambio_disponible(sku)
    _reintentar_pendientes(sku)


def confirmar_pick(sku, cantidad, referencia):
    """Pick confirmado: consume la reserva y mueve el físico vendible → en_empaque."""
    cantidad = _validar_cantidad(cantidad, "pickear")
    with transaction.atomic():
        saldos = list(
            Saldo.objects.select_for_update().filter(
                sku=sku, estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO],
            )
        )
        vendibles = [s for s in saldos if s.estado == Saldo.UBICADO_VENDIBLE]
        reservados = [s for s in saldos if s.estado == Saldo.RESERVADO]
        if sum(s.cantidad for s in reservados) < cantidad:
            raise ValueError(
                f"No hay {cantidad} unidades reservadas de {sku.codigo} para pickear."
            )
        if sum(s.cantidad for s in vendibles) < cantidad:
            raise ValueError(
                f"Inconsistencia de saldo en {sku.codigo}: hay reserva pero el físico "
                "vendible no alcanza. Levanta un conteo antes de continuar."
            )
        caducidades = _caducidades(saldos)
        restante = cantidad
        for capa in sorted(reservados, key=lambda s: _clave_fefo(s, caducidades)):
            if restante == 0:
                break
            tomo = min(capa.cantidad, restante)
            if tomo == 0:
                continue
            # 1) baja la capa de reserva
            capa.cantidad -= tomo
            if capa.cantidad == 0:
                capa.delete()
            else:
                capa.save(update_fields=["cantidad"])
            # 2) baja el físico vendible: primero misma ubicación/lote, luego FEFO
            mismos = [
                v for v in vendibles
                if v.cantidad > 0 and (v.ubicacion_id, v.lote_id) == (capa.ubicacion_id, capa.lote_id)
            ]
            otros = sorted(
                (v for v in vendibles
                 if v.cantidad > 0 and (v.ubicacion_id, v.lote_id) != (capa.ubicacion_id, capa.lote_id)),
                key=lambda s: _clave_fefo(s, caducidades),
            )
            _restar(mismos + otros, tomo)
            # 3) sube en_empaque conservando ubicación/lote de origen (trazabilidad)
            _incrementar(sku, capa.ubicacion_id, capa.lote_id, Saldo.EN_EMPAQUE, tomo)
            restante -= tomo

        _mov(
            sku, Movimiento.PICK, cantidad,
            origen=Saldo.RESERVADO, destino=Saldo.EN_EMPAQUE, referencia=referencia,
        )
        registrar_evento(
            "sku", sku.codigo, "pick", cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia)},
        )
    _notificar_cambio_disponible(sku)


def despachar(sku, cantidad, referencia):
    """Salida física: en_empaque → despachado (delta negativo en el kardex)."""
    cantidad = _validar_cantidad(cantidad, "despachar")
    with transaction.atomic():
        en_empaque = list(
            Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_EMPAQUE)
        )
        total = sum(s.cantidad for s in en_empaque)
        if total < cantidad:
            raise ValueError(
                f"No hay {cantidad} unidades en empaque de {sku.codigo}; hay {total}. "
                "Confirma el pick antes de despachar."
            )
        caducidades = _caducidades(en_empaque)
        _restar(sorted(en_empaque, key=lambda s: _clave_fefo(s, caducidades)), cantidad)
        _mov(
            sku, Movimiento.SALIDA, -cantidad,
            origen=Saldo.EN_EMPAQUE, destino="despachado", referencia=referencia,
        )
        registrar_evento(
            "sku", sku.codigo, "salida", cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia)},
        )
    _notificar_cambio_disponible(sku)


# ─────────────────────────────────────────────────────────────────────────────
# Contrato: recepción y put-away
# ─────────────────────────────────────────────────────────────────────────────


def recibir(linea_asn, cantidad_ok, cantidad_danada, actor):
    """Recibe una línea de ASN: lo bueno entra en_putaway, lo dañado a cuarentena.

    Lo recibido sin ubicar NO es vendible (el portal lo muestra como
    "en recepción"). Auto-transiciona la orden: ANUNCIADA→EN_RECEPCION al primer
    registro; →RECIBIDA cuando todas las líneas quedan completas.
    """
    cantidad_ok = max(int(cantidad_ok), 0)
    cantidad_danada = max(int(cantidad_danada), 0)
    if cantidad_ok == 0 and cantidad_danada == 0:
        raise ValueError("Nada que recibir: captura cantidad buena o dañada.")
    orden = linea_asn.orden
    if orden.estado == OrdenEntrada.CERRADA:
        raise ValueError(f"La orden {orden.folio} ya está cerrada; no acepta más recepciones.")

    with transaction.atomic():
        if orden.estado == OrdenEntrada.ANUNCIADA:
            orden.transicionar(OrdenEntrada.EN_RECEPCION, actor=actor, motivo="Inicio de descarga")

        ubic_recepcion = _ubicacion_tipo(Ubicacion.RECEPCION)
        if ubic_recepcion is None:
            raise ValueError("No hay ubicación de recepción activa. Da de alta una (tipo recepción).")

        sku = linea_asn.sku
        if cantidad_ok:
            _incrementar(sku, ubic_recepcion.pk, None, Saldo.EN_PUTAWAY, cantidad_ok)
            _mov(
                sku, Movimiento.RECEPCION, cantidad_ok,
                destino=Saldo.EN_PUTAWAY, referencia=orden.folio, actor=actor,
            )
        if cantidad_danada:
            _incrementar(sku, ubic_recepcion.pk, None, Saldo.CUARENTENA, cantidad_danada)
            _mov(
                sku, Movimiento.RECEPCION, cantidad_danada,
                destino=Saldo.CUARENTENA, referencia=orden.folio, actor=actor,
            )

        linea_asn.cantidad_recibida += cantidad_ok
        linea_asn.cantidad_danada += cantidad_danada
        linea_asn.save(update_fields=["cantidad_recibida", "cantidad_danada"])

        registrar_evento(
            "asn", orden.folio, "recepcion_linea", actor=actor, cliente=orden.cliente,
            delta={"sku": sku.codigo, "ok": cantidad_ok, "danada": cantidad_danada},
        )

        if orden.estado == OrdenEntrada.EN_RECEPCION and all(
            linea.completa for linea in orden.lineas.all()
        ):
            orden.transicionar(OrdenEntrada.RECIBIDA, actor=actor, motivo="Todas las líneas recibidas")
    return linea_asn


def ubicar(sku, cantidad, ubicacion, lote, actor):
    """Put-away: en_putaway → ubicado_vendible en la ubicación destino.

    Aquí es donde el stock se vuelve vendible; por eso dispara push a Shopify.
    """
    cantidad = _validar_cantidad(cantidad, "ubicar")
    if not ubicacion.activo:
        raise ValueError(f"La ubicación {ubicacion.codigo} está inactiva.")
    if ubicacion.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA):
        raise ValueError(
            f"{ubicacion.codigo} es de tipo {ubicacion.get_tipo_display()}: "
            "el stock vendible solo vive en picking o reserva."
        )
    if sku.requiere_lote and lote is None:
        raise ValueError(f"{sku.codigo} requiere lote: escanea o captura el lote antes de ubicar.")
    if lote is not None and lote.sku_id != sku.pk:
        raise ValueError(f"El lote {lote.codigo} no corresponde al SKU {sku.codigo}.")

    with transaction.atomic():
        en_putaway = list(
            Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY)
        )
        total = sum(s.cantidad for s in en_putaway)
        if total < cantidad:
            raise ValueError(
                f"Solo hay {total} unidades de {sku.codigo} en put-away; no puedo ubicar {cantidad}."
            )
        lote_id = lote.pk if lote else None

        def preferencia(saldo):
            if saldo.lote_id == lote_id:
                prioridad = 0
            elif saldo.lote_id is None:
                prioridad = 1
            else:
                prioridad = 2
            return (prioridad, saldo.pk)

        _restar(sorted(en_putaway, key=preferencia), cantidad)
        _incrementar(sku, ubicacion.pk, lote_id, Saldo.UBICADO_VENDIBLE, cantidad)
        _mov(
            sku, Movimiento.PUTAWAY, cantidad, lote=lote,
            origen=Saldo.EN_PUTAWAY, destino=Saldo.UBICADO_VENDIBLE,
            referencia=ubicacion.codigo, actor=actor,
        )
        registrar_evento(
            "sku", sku.codigo, "putaway", actor=actor, cliente=sku.cliente,
            delta={"cantidad": cantidad, "ubicacion": ubicacion.codigo,
                   "lote": lote.codigo if lote else None},
        )
    _notificar_cambio_disponible(sku)
    _reintentar_pendientes(sku)


def _abrir_incidencia_discrepancia_recepcion(orden, discrepancias):
    """Abre UNA incidencia DES por las diferencias de la recepción.

    Lazy: incidencias puede no existir aún. La descripción lista SKU por SKU
    lo anunciado contra lo que realmente llegó.
    """
    try:
        from apps.incidencias import services as incidencias_services  # lazy por contrato
    except ImportError:
        return None
    renglones = []
    for d in discrepancias:
        partes = []
        if d["faltante"]:
            partes.append(f"faltaron {d['faltante']}")
        if d["sobrante"]:
            partes.append(f"sobraron {d['sobrante']}")
        if d["danada"]:
            partes.append(f"{d['danada']} llegaron dañadas")
        renglones.append(
            f"- {d['sku']}: anunciadas {d['anunciada']}, llegaron "
            f"{d['recibida'] + d['danada']} ({', '.join(partes)})."
        )
    texto = (
        f"La recepción {orden.folio} de {orden.cliente.nombre} cerró con "
        "diferencias contra lo anunciado:\n"
        + "\n".join(renglones)
        + "\nRevisar con el cliente y ajustar con doble firma si procede."
    )
    sku = None
    if len(discrepancias) == 1:
        sku = next(
            (linea.sku for linea in orden.lineas.all()
             if linea.sku.codigo == discrepancias[0]["sku"]),
            None,
        )
    return incidencias_services.abrir_incidencia(
        cliente=orden.cliente, tipo="DES", origen="auto", sku=sku, texto=texto,
    )


def _notificar_recepcion_cerrada(orden):
    """Avisa al cliente que su entrega quedó cerrada. Lazy: mensajeria puede no existir."""
    try:
        from apps.mensajeria.services import enviar_recepcion_cerrada  # lazy por contrato
    except ImportError:
        return None
    return enviar_recepcion_cerrada(orden)


def cerrar_recepcion(orden, actor, tarimas_recibidas=None):
    """Cierra la recepción: valida que nada quede en put-away, guarda las tarimas
    recibidas, transiciona a CERRADA y hace la confirmación honesta.

    Si hubo faltantes o sobrantes contra lo anunciado, registra el evento
    `cerrada_con_discrepancia` y abre UNA incidencia DES con el desglose por SKU.
    Siempre avisa al cliente (lazy a mensajeria.enviar_recepcion_cerrada).

    Con faltantes la orden nunca llega sola a RECIBIDA (la línea no se
    completa): cerrar desde EN_RECEPCION pasa primero por RECIBIDA — es la
    decisión honesta de "ya no va a llegar más".

    OJO: el filtro de put-away es por SKU global (mismo criterio histórico del
    piso): put-away de otra orden del mismo SKU también bloquea el cierre.
    """
    with transaction.atomic():
        # Bajo lock: un re-POST del form de cierre o dos cierres concurrentes
        # no deben mutar tarimas ni duplicar incidencia/evento.
        orden = OrdenEntrada.objects.select_for_update().get(pk=orden.pk)
        if orden.estado == OrdenEntrada.ANUNCIADA:
            raise ValueError(
                f"La orden {orden.folio} no ha recibido nada todavía; no hay recepción que cerrar."
            )
        if orden.estado not in (OrdenEntrada.EN_RECEPCION, OrdenEntrada.RECIBIDA):
            raise ValueError(f"La orden {orden.folio} ya está cerrada.")
        lineas = list(orden.lineas.select_related("sku"))
        pendiente = (
            Saldo.objects.filter(
                sku_id__in=[linea.sku_id for linea in lineas], estado=Saldo.EN_PUTAWAY,
            ).aggregate(t=Sum("cantidad"))["t"] or 0
        )
        if pendiente > 0:
            raise ValueError(
                f"Aún hay {pendiente} piezas sin ubicar. Ubica todo antes de cerrar la orden."
            )
        if tarimas_recibidas is not None:
            tarimas_recibidas = int(tarimas_recibidas)
            if tarimas_recibidas < 0:
                raise ValueError("Las tarimas recibidas no pueden ser negativas.")

        # Validado todo: recién aquí se escribe (nada muta si el cierre no procede).
        if tarimas_recibidas is not None:
            orden.tarimas_recibidas = tarimas_recibidas
            orden.save(update_fields=["tarimas_recibidas"])

        discrepancias = []
        for linea in lineas:
            llegado = linea.cantidad_recibida + linea.cantidad_danada
            faltante = max(linea.cantidad_anunciada - llegado, 0)
            sobrante = max(llegado - linea.cantidad_anunciada, 0)
            if faltante or sobrante:
                discrepancias.append({
                    "sku": linea.sku.codigo,
                    "anunciada": linea.cantidad_anunciada,
                    "recibida": linea.cantidad_recibida,
                    "danada": linea.cantidad_danada,
                    "faltante": faltante,
                    "sobrante": sobrante,
                })

        if orden.estado == OrdenEntrada.EN_RECEPCION:
            # Cierre con líneas incompletas: la descarga real terminó con el
            # último movimiento de recepción, no al momento del cierre — si no,
            # el SLA de recepción quedaría siempre en ~0 y la facturación por
            # tarima se movería al mes del cierre. `transicionar` respeta el
            # valor ya estampado (solo escribe si es None).
            if orden.ts_descarga_fin is None:
                ultimo = (
                    Movimiento.objects.filter(
                        tipo=Movimiento.RECEPCION, referencia=orden.folio,
                    )
                    .order_by("-ts", "-id")
                    .first()
                )
                orden.ts_descarga_fin = ultimo.ts if ultimo else timezone.now()
                orden.save(update_fields=["ts_descarga_fin"])
            orden.transicionar(
                OrdenEntrada.RECIBIDA, actor=actor,
                motivo="Descarga terminada: cierre con líneas incompletas",
            )
        orden.transicionar(OrdenEntrada.CERRADA, actor=actor, motivo="Todo ubicado y vendible")

        if discrepancias:
            registrar_evento(
                "asn", orden.folio, "cerrada_con_discrepancia",
                actor=actor, cliente=orden.cliente,
                delta={"discrepancias": discrepancias},
                motivo="La recepción cerró con diferencias contra lo anunciado.",
            )
            _abrir_incidencia_discrepancia_recepcion(orden, discrepancias)

        _notificar_recepcion_cerrada(orden)
    return orden


# ─────────────────────────────────────────────────────────────────────────────
# Contrato: conteos y ajustes con doble firma
# ─────────────────────────────────────────────────────────────────────────────


def _excede_umbral_descuadre(sku, diferencia):
    """True si |dif| supera el umbral de unidades O de valor (settings.TORRE)."""
    if diferencia == 0:
        return False
    from decimal import Decimal
    torre = settings.TORRE
    if abs(diferencia) > torre["UMBRAL_DISCREPANCIA_UNIDADES"]:
        return True
    valor = abs(diferencia) * (sku.precio_declarado or 0)
    return valor > Decimal(str(torre["UMBRAL_DISCREPANCIA_MXN"]))


def _abrir_incidencia_descuadre(conteo):
    """Abre incidencia DES por descuadre. Lazy: incidencias puede no existir aún."""
    try:
        from apps.incidencias import services as incidencias_services
    except ImportError:
        return None
    diferencia = conteo.diferencia
    texto = (
        f"Descuadre en conteo {conteo.folio}: SKU {conteo.sku.codigo} — "
        f"el sistema esperaba {conteo.esperado} y contamos {conteo.contado} "
        f"(diferencia {diferencia:+d}). Requiere investigación y ajuste con doble firma."
    )
    return incidencias_services.abrir_incidencia(
        cliente=conteo.sku.cliente, tipo="DES", origen="auto", sku=conteo.sku, texto=texto,
    )


def registrar_conteo(sku, contado, contador):
    """Registra un conteo ciego: el esperado lo pone el sistema (físico vendible).

    Deja rastro en el kardex (delta 0: contar no mueve stock), completa la tarea
    del día si existe, y abre incidencia DES si la diferencia excede umbral.
    """
    contado = int(contado)
    if contado < 0:
        raise ValueError("El conteo no puede ser negativo.")
    esperado = _suma(sku, Saldo.UBICADO_VENDIBLE)
    conteo = Conteo.objects.create(
        sku=sku, contador=_actor_str(contador), esperado=esperado, contado=contado,
    )
    _mov(
        sku, Movimiento.CONTEO, 0,
        origen=Saldo.UBICADO_VENDIBLE, destino=Saldo.UBICADO_VENDIBLE,
        referencia=conteo.folio, actor=contador,
    )
    registrar_evento(
        "conteo", conteo.folio, "conteo_registrado", actor=contador, cliente=sku.cliente,
        delta={"sku": sku.codigo, "esperado": esperado, "contado": contado,
               "diferencia": conteo.diferencia},
    )
    TareaConteo.objects.filter(
        sku=sku, fecha=timezone.localdate(), estado=TareaConteo.PENDIENTE,
    ).update(estado=TareaConteo.COMPLETADA, conteo=conteo)

    if _excede_umbral_descuadre(sku, conteo.diferencia):
        _abrir_incidencia_descuadre(conteo)
    return conteo


FIRMA_INVALIDA = "Firma inválida: revisa el usuario y el PIN."


def _validar_firma(usuario, pin):
    """Valida una firma: el usuario existe, es de piso/mesa y su PIN coincide.

    UN solo mensaje para todos los fallos: distinguir "no existe" / "sin PIN" /
    "PIN incorrecto" era un oráculo que le ahorraba la mitad del trabajo a
    quien intentara adivinar la firma de un colega. El PIN se compara contra
    hash (PerfilUsuario.check_pin), jamás en claro.
    """
    from django.contrib.auth import get_user_model

    from apps.core.models import PerfilUsuario

    username = usuario.username if hasattr(usuario, "username") else str(usuario)
    user = get_user_model().objects.filter(username=username).first()
    perfil = getattr(user, "perfil", None) if user else None
    if (
        perfil is None
        or perfil.rol not in (PerfilUsuario.ROL_PISO, PerfilUsuario.ROL_MESA)
        or not perfil.check_pin(pin)
    ):
        raise ValueError(FIRMA_INVALIDA)
    return perfil


def aplicar_ajuste(
    sku, delta, motivo, pin1_usuario, pin1, pin2_usuario, pin2,
    lote=None, conteo=None, incidencia_ref="",
):
    """Ajuste de inventario con doble firma obligatoria: dos PINs de dos personas
    distintas con rol piso/mesa. Motivo de catálogo cerrado. Regresa el Ajuste.
    """
    delta = int(delta)
    if delta == 0:
        raise ValueError("Un ajuste de cero unidades no ajusta nada.")
    if motivo not in dict(Ajuste.MOTIVOS):
        validos = ", ".join(clave for clave, _ in Ajuste.MOTIVOS)
        raise ValueError(f"Motivo '{motivo}' fuera del catálogo. Válidos: {validos}.")

    perfil_1 = _validar_firma(pin1_usuario, pin1)
    perfil_2 = _validar_firma(pin2_usuario, pin2)
    if perfil_1.usuario_id == perfil_2.usuario_id:
        raise ValueError("La doble firma exige dos personas distintas: un PIN no firma dos veces.")

    if lote is not None and lote.sku_id != sku.pk:
        raise ValueError(f"El lote {lote.codigo} no corresponde al SKU {sku.codigo}.")

    with transaction.atomic():
        filtro = {"sku": sku, "estado": Saldo.UBICADO_VENDIBLE}
        if lote is not None:
            filtro["lote"] = lote
        vendibles = list(Saldo.objects.select_for_update().filter(**filtro))
        total = sum(s.cantidad for s in vendibles)

        if delta < 0:
            if total < -delta:
                raise ValueError(
                    f"El ajuste dejaría el saldo negativo: hay {total} vendibles de "
                    f"{sku.codigo} y el ajuste quita {-delta}."
                )
            caducidades = _caducidades(vendibles)
            _restar(sorted(vendibles, key=lambda s: _clave_fefo(s, caducidades)), -delta)
        else:
            if vendibles:
                caducidades = _caducidades(vendibles)
                ancla = sorted(vendibles, key=lambda s: _clave_fefo(s, caducidades))[0]
                _incrementar(sku, ancla.ubicacion_id, ancla.lote_id, Saldo.UBICADO_VENDIBLE, delta)
            else:
                ubic = _ubicacion_tipo(Ubicacion.PICKING, Ubicacion.RESERVA)
                if ubic is None:
                    raise ValueError("No hay ubicación de picking/reserva activa para recibir el ajuste.")
                _incrementar(sku, ubic.pk, lote.pk if lote else None, Saldo.UBICADO_VENDIBLE, delta)

        ajuste = Ajuste.objects.create(
            sku=sku, lote=lote, delta=delta, motivo=motivo,
            autorizo_1=perfil_1.usuario.username, autorizo_2=perfil_2.usuario.username,
            conteo=conteo, incidencia_ref=incidencia_ref,
        )
        _mov(
            sku, Movimiento.AJUSTE, delta, lote=lote,
            origen=Saldo.UBICADO_VENDIBLE, destino=Saldo.UBICADO_VENDIBLE,
            referencia=ajuste.folio,
            actor=f"{ajuste.autorizo_1}+{ajuste.autorizo_2}",
        )
        registrar_evento(
            "ajuste", ajuste.folio, "ajuste_aplicado",
            actor=perfil_1.usuario, cliente=sku.cliente,
            delta={"sku": sku.codigo, "delta": delta,
                   "firmas": [ajuste.autorizo_1, ajuste.autorizo_2],
                   "conteo": conteo.folio if conteo else None,
                   "incidencia": incidencia_ref or None},
            motivo=dict(Ajuste.MOTIVOS)[motivo],
        )
    _notificar_cambio_disponible(sku)
    if delta > 0:
        _reintentar_pendientes(sku)
    return ajuste


# ─────────────────────────────────────────────────────────────────────────────
# Contrato: retornos
# ─────────────────────────────────────────────────────────────────────────────


def retornar(sku, cantidad, referencia, actor):
    """Reingreso por retorno: entra a CUARENTENA en la zona de retornos hasta
    inspección (el retorno nace como incidencia P1 con reingreso fotografiado).
    """
    cantidad = _validar_cantidad(cantidad, "retornar")
    ubic = _ubicacion_tipo(Ubicacion.RETORNO, Ubicacion.RECEPCION)
    if ubic is None:
        raise ValueError("No hay ubicación de retorno ni de recepción activa para el reingreso.")
    with transaction.atomic():
        _incrementar(sku, ubic.pk, None, Saldo.CUARENTENA, cantidad)
        _mov(
            sku, Movimiento.RETORNO, cantidad,
            destino=Saldo.CUARENTENA, referencia=referencia, actor=actor,
        )
        registrar_evento(
            "sku", sku.codigo, "retorno", actor=actor, cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia), "ubicacion": ubic.codigo},
        )
    _notificar_cambio_disponible(sku)


def restock_empaque(sku, cantidad, referencia, actor=None):
    """Extra (no contrato): regresa unidades de en_empaque a vendible.

    Lo usa la tarea de restock cuando un pedido se cancela después del pick.
    """
    cantidad = _validar_cantidad(cantidad, "regresar a anaquel")
    with transaction.atomic():
        en_empaque = list(
            Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_EMPAQUE)
        )
        total = sum(s.cantidad for s in en_empaque)
        if total < cantidad:
            raise ValueError(
                f"No hay {cantidad} unidades en empaque de {sku.codigo} para regresar; hay {total}."
            )
        caducidades = _caducidades(en_empaque)
        origenes = sorted(en_empaque, key=lambda s: _clave_fefo(s, caducidades))
        restante = cantidad
        for saldo in origenes:
            if restante == 0:
                break
            tomo = min(saldo.cantidad, restante)
            _restar([saldo], tomo)
            _incrementar(sku, saldo.ubicacion_id, saldo.lote_id, Saldo.UBICADO_VENDIBLE, tomo)
            restante -= tomo
        _mov(
            sku, Movimiento.PUTAWAY, cantidad,
            origen=Saldo.EN_EMPAQUE, destino=Saldo.UBICADO_VENDIBLE,
            referencia=referencia, actor=actor,
        )
        registrar_evento(
            "sku", sku.codigo, "restock_empaque", actor=actor, cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia)},
        )
    _notificar_cambio_disponible(sku)
    _reintentar_pendientes(sku)


# ─────────────────────────────────────────────────────────────────────────────
# Contrato: dictamen de cuarentena (doble firma)
# ─────────────────────────────────────────────────────────────────────────────


def dictaminar_cuarentena(
    sku, cantidad, destino, autorizo_1, pin_1, autorizo_2, pin_2, actor,
    lote=None, motivo_texto="", ubicacion=None,
):
    """Dictamen de cuarentena: nada sale de cuarentena sin dos firmas.

    destino "revendible": CUARENTENA → EN_PUTAWAY en la zona de recepción
    (igual que recibir); vuelve al flujo normal de put-away para quedar
    vendible. destino "merma": sale físico del inventario (Movimiento tipo
    merma con delta negativo). FEFO si no se indica lote.

    Con `ubicacion` el dictamen se acota a ESA fila (sku + cuarentena +
    ubicación + lote EXACTO, incluido lote None): lo que promete la pantalla
    por fila es lo que se descuenta. Sin `ubicacion`: FEFO global del SKU.
    """
    if destino not in ("revendible", "merma"):
        raise ValueError(
            "Destino inválido: el dictamen solo puede ser 'revendible' o 'merma'."
        )
    cantidad = _validar_cantidad(cantidad, "dictaminar")

    perfil_1 = _validar_firma(autorizo_1, pin_1)
    perfil_2 = _validar_firma(autorizo_2, pin_2)
    if perfil_1.usuario_id == perfil_2.usuario_id:
        raise ValueError("La doble firma exige dos personas distintas: un PIN no firma dos veces.")
    firmas = [perfil_1.usuario.username, perfil_2.usuario.username]

    if lote is not None and lote.sku_id != sku.pk:
        raise ValueError(f"El lote {lote.codigo} no corresponde al SKU {sku.codigo}.")

    with transaction.atomic():
        filtro = {"sku": sku, "estado": Saldo.CUARENTENA}
        if ubicacion is not None:
            # Fila exacta: ubicación + lote tal cual (lote None filtra
            # lote__isnull=True; jamás cae en otra fila del mismo SKU).
            filtro["ubicacion"] = ubicacion
            filtro["lote"] = lote
        elif lote is not None:
            filtro["lote"] = lote
        en_cuarentena = list(Saldo.objects.select_for_update().filter(**filtro))
        total = sum(s.cantidad for s in en_cuarentena)
        if total < cantidad:
            if ubicacion is not None:
                raise ValueError(f"Esa fila solo tiene {total} piezas en cuarentena.")
            del_lote = f" del lote {lote.codigo}" if lote is not None else ""
            raise ValueError(
                f"Solo hay {total} piezas de {sku.codigo}{del_lote} en cuarentena; "
                f"no puedo dictaminar {cantidad}."
            )
        caducidades = _caducidades(en_cuarentena)
        origenes = sorted(en_cuarentena, key=lambda s: _clave_fefo(s, caducidades))

        if destino == "revendible":
            ubic_recepcion = _ubicacion_tipo(Ubicacion.RECEPCION)
            if ubic_recepcion is None:
                raise ValueError(
                    "No hay ubicación de recepción activa. Da de alta una (tipo recepción)."
                )
            # Regresa a put-away conservando el lote de cada saldo (trazabilidad).
            restante = cantidad
            for saldo in origenes:
                if restante == 0:
                    break
                tomo = min(saldo.cantidad, restante)
                if tomo == 0:
                    continue
                _restar([saldo], tomo)
                _incrementar(sku, ubic_recepcion.pk, saldo.lote_id, Saldo.EN_PUTAWAY, tomo)
                restante -= tomo
            _mov(
                sku, Movimiento.AJUSTE, cantidad, lote=lote,
                origen=Saldo.CUARENTENA, destino=Saldo.EN_PUTAWAY,
                referencia="DICTAMEN", actor=f"{firmas[0]}+{firmas[1]}",
            )
            accion = "dictamen_revendible"
        else:
            _restar(origenes, cantidad)
            _mov(
                sku, Movimiento.MERMA, -cantidad, lote=lote,
                origen=Saldo.CUARENTENA, destino="",
                referencia="DICTAMEN", actor=f"{firmas[0]}+{firmas[1]}",
            )
            accion = "dictamen_merma"

        registrar_evento(
            "cuarentena", sku.codigo, accion, actor=actor, cliente=sku.cliente,
            delta={
                "cantidad": cantidad,
                "lote": lote.codigo if lote else None,
                "firmas": firmas,
                "motivo_texto": motivo_texto,
            },
            motivo=motivo_texto,
        )
    _notificar_cambio_disponible(sku)


# ─────────────────────────────────────────────────────────────────────────────
# Consultas de apoyo (portal / mesa)
# ─────────────────────────────────────────────────────────────────────────────


def resumen_sku(sku):
    """Resumen por SKU para el portal: físico / en recepción / apartado /
    cuarentena / disponible + fecha del último conteo.
    """
    filas = Saldo.objects.filter(sku=sku).values("estado").annotate(t=Sum("cantidad"))
    por_estado = {f["estado"]: f["t"] or 0 for f in filas}
    vendible = por_estado.get(Saldo.UBICADO_VENDIBLE, 0)
    reservado = por_estado.get(Saldo.RESERVADO, 0)
    en_putaway = por_estado.get(Saldo.EN_PUTAWAY, 0)
    en_empaque = por_estado.get(Saldo.EN_EMPAQUE, 0)
    cuarentena = por_estado.get(Saldo.CUARENTENA, 0)
    ultimo = sku.conteos.order_by("-ts").first()
    return {
        "sku": sku,
        "fisico": vendible + en_putaway + en_empaque + cuarentena,
        "en_recepcion": en_putaway,
        "vendible": vendible,
        "apartado": reservado,
        "en_empaque": en_empaque,
        "cuarentena": cuarentena,
        "disponible": vendible - reservado - (sku.cliente.buffer_stock or 0),
        "ultimo_conteo": ultimo.ts if ultimo else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Job: conteo cíclico (idempotente; lo invoca el command `conteo_ciclico`)
# ─────────────────────────────────────────────────────────────────────────────


def generar_conteo_ciclico(fecha=None):
    """Elige los SKUs del día (3 por default, por antigüedad de conteo: el que
    lleva más tiempo sin contarse va primero) y crea sus tareas.

    Idempotente: correrlo dos veces el mismo día no duplica tareas.
    Regresa TODAS las tareas del día (existentes + nuevas).
    """
    from apps.catalogo.models import SKU

    fecha = fecha or timezone.localdate()
    objetivo = settings.TORRE.get("CONTEOS_DIARIOS", 3)
    existentes = list(TareaConteo.objects.filter(fecha=fecha))
    faltan = objetivo - len(existentes)
    if faltan <= 0:
        return existentes

    ya_asignados = [t.sku_id for t in existentes]
    candidatos = (
        SKU.objects.filter(activo=True, cliente__activo=True)
        .exclude(pk__in=ya_asignados)
        .annotate(ultimo_conteo=Max("conteos__ts"))
        .order_by(F("ultimo_conteo").asc(nulls_first=True), "codigo")[:faltan]
    )
    nuevas = []
    for sku in candidatos:
        tarea = TareaConteo.objects.create(fecha=fecha, sku=sku)
        registrar_evento(
            "tarea_conteo", tarea.pk, "tarea_creada", cliente=sku.cliente,
            delta={"sku": sku.codigo, "fecha": str(fecha)},
            motivo="Conteo cíclico del día",
        )
        nuevas.append(tarea)
    return existentes + nuevas
