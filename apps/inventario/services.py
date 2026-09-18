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
from apps.core.fechas import FORMATOS_LEGIBLES, parsear_fecha_csv
from apps.core.services import registrar_evento

from .models import Ajuste, Conteo, LineaASN, Movimiento, OrdenEntrada, Saldo, TareaConteo

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
        cliente=orden.cliente, tipo="DES", origen="auto", sku=sku, texto=texto, orden=orden,
    )


def _notificar_recepcion_cerrada(orden):
    """Avisa al cliente que su entrega quedó cerrada. Lazy: mensajeria puede no existir."""
    try:
        from apps.mensajeria.services import enviar_recepcion_cerrada  # lazy por contrato
    except ImportError:
        return None
    return enviar_recepcion_cerrada(orden)


def detalle_recepciones(ordenes):
    """Anota cada OrdenEntrada con lo que el acordeón de recepciones muestra al
    expandirla: `detalle_lineas` (por SKU: anunciada, recibida, dañada, diferencia,
    lote y caducidad; la diferencia solo se evalúa en órdenes CERRADAS),
    `fotos_llegada` (evidencia de la recepción, ligada al folio) e
    `incidencias_orden` (las DES nacidas de ella). Regresa la lista."""
    from apps.core.models import EvidenciaFoto

    ordenes = list(ordenes)
    fotos = {}
    for foto in EvidenciaFoto.objects.filter(
        entidad="asn", entidad_id__in=[o.folio for o in ordenes],
    ).order_by("ts", "pk"):
        fotos.setdefault(foto.entidad_id, []).append(foto)
    for orden in ordenes:
        orden.evaluada = orden.estado == OrdenEntrada.CERRADA
        orden.detalle_lineas = []
        for linea in orden.lineas.all():
            llegaron = linea.cantidad_recibida + linea.cantidad_danada
            orden.detalle_lineas.append({
                "sku": linea.sku, "anunciada": linea.cantidad_anunciada,
                "recibida": linea.cantidad_recibida, "danada": linea.cantidad_danada,
                "diferencia": llegaron - linea.cantidad_anunciada,
                "lote": linea.lote_codigo, "caducidad": linea.fecha_caducidad,
                "con_diferencia": orden.evaluada and (llegaron != linea.cantidad_anunciada or linea.cantidad_danada > 0),
            })
        orden.fotos_llegada = fotos.get(orden.folio, [])
        orden.incidencias_orden = list(orden.incidencias.all())
    return ordenes


def skus_recibibles(cliente):
    """SKUs que pueden venir en una ASN: activos y no kit (un kit se arma al empacar)."""
    from apps.catalogo.models import SKU

    return SKU.objects.filter(cliente=cliente, activo=True, es_kit=False).order_by("codigo")


COLUMNAS_PLANTILLA_ASN = ("codigo", "descripcion", "cantidad", "lote", "caducidad")


def filas_plantilla_asn(cliente, sku_pks):
    """Renglones del formato CSV del anuncio (portal y Mesa): las columnas que lee
    FormAnuncioASN más `descripcion` para que la hoja se entienda; un renglón por
    SKU pedido con codigo y descripcion prellenados, cantidad y lote en blanco y
    la caducidad con el placeholder AAAA-MM-DD (enseña el formato; sin tocar
    cuenta como vacío). SKUs de otro cliente o kits se ignoran."""
    from apps.core.fechas import PLACEHOLDER_FECHA

    pks = [v for v in sku_pks if str(v).isdigit()]
    skus = skus_recibibles(cliente).filter(pk__in=pks) if pks else []
    return [[sku.codigo, sku.descripcion, "", "", PLACEHOLDER_FECHA] for sku in skus]


def anunciar_asn(cliente, fecha_compromiso, tarimas, lineas, actor, origen, motivo=""):
    """Alta de una ASN (Mesa o portal): OrdenEntrada + una LineaASN por
    (SKU, lote anunciado) + evento `anunciada_<origen>`. `lineas` viene del form
    como [(sku, cantidad, lote_codigo, fecha_caducidad)]; lote y caducidad son
    opcionales y quedan en la línea para preseleccionarlos al ubicar en piso.
    """
    with transaction.atomic():
        orden = OrdenEntrada.objects.create(
            cliente=cliente, fecha_compromiso=fecha_compromiso, tarimas=tarimas or 0,
        )
        detalle = []
        for sku, cantidad, lote_codigo, fecha_caducidad in lineas:
            if sku.cliente_id != cliente.pk:
                raise ValueError(f"El SKU {sku.codigo} no es de {cliente.nombre}.")
            LineaASN.objects.create(
                orden=orden, sku=sku, cantidad_anunciada=cantidad,
                lote_codigo=lote_codigo or "", fecha_caducidad=fecha_caducidad,
            )
            fila = {"sku": sku.codigo, "cantidad": cantidad}
            if lote_codigo:
                fila["lote"] = lote_codigo
            detalle.append(fila)
        registrar_evento(
            "asn", orden.folio, f"anunciada_{origen}", actor=actor, cliente=cliente,
            delta={"fecha_compromiso": str(fecha_compromiso), "tarimas": tarimas or 0, "lineas": detalle},
            motivo=motivo,
        )
    return orden


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

        if orden.tipo == OrdenEntrada.TIPO_ASN:
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


def _validar_doble_firma(pin1_usuario, pin1, pin2_usuario, pin2):
    """Dos firmas válidas de dos personas distintas. Regresa (perfil_1, perfil_2)."""
    perfil_1 = _validar_firma(pin1_usuario, pin1)
    perfil_2 = _validar_firma(pin2_usuario, pin2)
    if perfil_1.usuario_id == perfil_2.usuario_id:
        raise ValueError("La doble firma exige dos personas distintas: un PIN no firma dos veces.")
    return perfil_1, perfil_2


def aplicar_ajuste(
    sku, delta, motivo, pin1_usuario, pin1, pin2_usuario, pin2,
    lote=None, conteo=None, incidencia_ref="", ubicacion=None,
):
    """Ajuste de inventario con doble firma obligatoria: dos PINs de dos personas
    distintas con rol piso/mesa. Motivo de catálogo cerrado. Regresa el Ajuste.

    `ubicacion` (opcional, tipo picking/reserva): acota el ajuste a ESE anaquel —
    quita solo de su fila y agrega ahí. Sin ubicación: FEFO sobre todo el
    vendible del SKU (quita) o el anaquel donde ya vive el SKU (agrega).
    """
    perfil_1, perfil_2 = _validar_doble_firma(pin1_usuario, pin1, pin2_usuario, pin2)
    return _aplicar_ajuste_firmado(
        sku, delta, motivo, perfil_1.usuario, perfil_2.usuario,
        lote=lote, conteo=conteo, incidencia_ref=incidencia_ref, ubicacion=ubicacion,
    )


def _aplicar_ajuste_firmado(
    sku, delta, motivo, usuario_1, usuario_2,
    lote=None, conteo=None, incidencia_ref="", ubicacion=None,
):
    """Núcleo del ajuste con las firmas YA resueltas: `usuario_1`/`usuario_2`
    son los User que quedan como autorizo_1/autorizo_2 (dos personas con PIN
    en el ajuste individual; la misma persona de Mesa dos veces en la
    reconciliación por CSV, que Mesa aplica como admin sin doble PIN)."""
    delta = int(delta)
    if delta == 0:
        raise ValueError("Un ajuste de cero unidades no ajusta nada.")
    if motivo not in dict(Ajuste.MOTIVOS):
        validos = ", ".join(clave for clave, _ in Ajuste.MOTIVOS)
        raise ValueError(f"Motivo '{motivo}' fuera del catálogo. Válidos: {validos}.")

    if lote is not None and lote.sku_id != sku.pk:
        raise ValueError(f"El lote {lote.codigo} no corresponde al SKU {sku.codigo}.")
    if ubicacion is not None:
        if ubicacion.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA) or not ubicacion.activo:
            raise ValueError(
                f"La ubicación {ubicacion.codigo} no es un anaquel de picking/reserva activo."
            )

    with transaction.atomic():
        filtro = {"sku": sku, "estado": Saldo.UBICADO_VENDIBLE}
        if lote is not None:
            filtro["lote"] = lote
        if ubicacion is not None:
            filtro["ubicacion"] = ubicacion
        vendibles = list(Saldo.objects.select_for_update().filter(**filtro))
        total = sum(s.cantidad for s in vendibles)

        if delta < 0:
            if total < -delta:
                donde = f" en {ubicacion.codigo}" if ubicacion is not None else ""
                raise ValueError(
                    f"El ajuste dejaría el saldo negativo: hay {total} vendibles de "
                    f"{sku.codigo}{donde} y el ajuste quita {-delta}."
                )
            caducidades = _caducidades(vendibles)
            _restar(sorted(vendibles, key=lambda s: _clave_fefo(s, caducidades)), -delta)
        else:
            if ubicacion is not None:
                _incrementar(sku, ubicacion.pk, lote.pk if lote else None, Saldo.UBICADO_VENDIBLE, delta)
            elif vendibles:
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
            autorizo_1=usuario_1.username, autorizo_2=usuario_2.username,
            conteo=conteo, incidencia_ref=incidencia_ref,
        )
        _mov(
            sku, Movimiento.AJUSTE, delta, lote=lote,
            origen=Saldo.UBICADO_VENDIBLE, destino=Saldo.UBICADO_VENDIBLE,
            referencia=ajuste.folio,
            actor=(ajuste.autorizo_1 if ajuste.autorizo_1 == ajuste.autorizo_2
                   else f"{ajuste.autorizo_1}+{ajuste.autorizo_2}"),
        )
        registrar_evento(
            "ajuste", ajuste.folio, "ajuste_aplicado",
            actor=usuario_1, cliente=sku.cliente,
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


def reingresar_desde_pedido(sku, cantidad, referencia, actor, desde_empaque):
    """Mercancía de un pedido cancelado en bodega vuelve a put-away (zona de
    recepción) para que el piso la ubique por recepción, sin ubicación automática.

    desde_empaque=True: sale de EN_EMPAQUE (el pick ya se confirmó al empacar).
    desde_empaque=False: sale de UBICADO_VENDIBLE (pickeado en carrito, aún
    reservado): se libera la reserva y se resta el vendible FEFO. Kardex RETORNO
    con origen real → en_putaway; el lote se vuelve a declarar al ubicar.
    """
    cantidad = _validar_cantidad(cantidad, "reingresar")
    ubic = _ubicacion_tipo(Ubicacion.RECEPCION)
    if ubic is None:
        raise ValueError("No hay ubicación de recepción activa para el reingreso.")
    with transaction.atomic():
        if desde_empaque:
            origen = Saldo.EN_EMPAQUE
            filas = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_EMPAQUE))
            total = sum(f.cantidad for f in filas)
            if total < cantidad:
                raise ValueError(
                    f"Inconsistencia de saldo: hay {total} de {sku.codigo} en empaque y el "
                    f"reingreso pide {cantidad}. Levanta un conteo."
                )
            _restar(sorted(filas, key=lambda f: f.pk), cantidad)
        else:
            origen = Saldo.UBICADO_VENDIBLE
            liberar_reserva(sku, cantidad, referencia)
            filas = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.UBICADO_VENDIBLE))
            total = sum(f.cantidad for f in filas)
            if total < cantidad:
                raise ValueError(
                    f"Inconsistencia de saldo: hay {total} vendibles de {sku.codigo} y el "
                    f"reingreso pide {cantidad}. Levanta un conteo."
                )
            caducidades = _caducidades(filas)
            _restar(sorted(filas, key=lambda f: _clave_fefo(f, caducidades)), cantidad)
        _incrementar(sku, ubic.pk, None, Saldo.EN_PUTAWAY, cantidad)
        _mov(
            sku, Movimiento.RETORNO, cantidad,
            origen=origen, destino=Saldo.EN_PUTAWAY, referencia=referencia, actor=actor,
        )
        registrar_evento(
            "sku", sku.codigo, "reingreso_pedido", actor=actor, cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia), "origen": origen, "ubicacion": ubic.codigo},
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


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliación de inventario por CSV (Mesa): recuento físico → ajustes
# ─────────────────────────────────────────────────────────────────────────────

COLUMNAS_CSV_CONTEO = (
    "codigo", "descripcion", "lote", "caducidad", "ubicacion", "vendible_actual", "contado",
)
_COLUMNAS_CSV_CONTEO_MINIMAS = ("codigo", "contado")


def exportar_conteo(cliente):
    """Filas del CSV de conteo (columnas COLUMNAS_CSV_CONTEO): una por anaquel
    donde vive cada SKU/lote vendible, con `contado` prellenado con el vendible
    actual para editar solo lo que difiere. Los SKUs activos sin stock salen con
    una fila vacía para poder capturarlos; kits excluidos (jamás tienen stock).

    La importación lee SOLO codigo, lote, caducidad, ubicacion y contado; las
    demás columnas viajan para que el archivo sea legible (descripcion) y para
    avisar si el stock se movió desde la exportación (vendible_actual).
    """
    from apps.catalogo.models import SKU

    skus = list(
        SKU.objects.filter(cliente=cliente, activo=True, es_kit=False).order_by("codigo")
    )
    saldos = (
        Saldo.objects.filter(sku__in=skus, estado=Saldo.UBICADO_VENDIBLE, cantidad__gt=0)
        .select_related("lote", "ubicacion")
        .order_by("sku__codigo", "ubicacion__codigo", "lote__codigo")
    )
    por_sku = {}
    for saldo in saldos:
        por_sku.setdefault(saldo.sku_id, []).append(saldo)
    filas = []
    for sku in skus:
        for saldo in por_sku.get(sku.pk, []):
            cad = saldo.lote.fecha_caducidad if saldo.lote else None
            filas.append({
                "codigo": sku.codigo,
                "descripcion": sku.descripcion,
                "lote": saldo.lote.codigo if saldo.lote else "",
                "caducidad": cad.isoformat() if cad else "",
                "ubicacion": saldo.ubicacion.codigo,
                "vendible_actual": saldo.cantidad,
                "contado": saldo.cantidad,
            })
        if not por_sku.get(sku.pk):
            filas.append({
                "codigo": sku.codigo, "descripcion": sku.descripcion,
                "lote": "", "caducidad": "", "ubicacion": "",
                "vendible_actual": 0, "contado": 0,
            })
    return filas


def leer_csv_conteo(texto):
    """Parsea el CSV de conteo. Regresa (filas, errores_de_archivo).

    Cada fila: dict con codigo, descripcion, lote, caducidad, ubicacion,
    vendible_actual (str), contado (str crudo) y `numero` (renglón en Excel).
    Renglones totalmente vacíos se ignoran. Un CSV sin las columnas mínimas es
    un error de archivo (no de fila).
    """
    import csv
    import io

    lector = csv.DictReader(io.StringIO(texto))
    lector.fieldnames = [(e or "").strip() for e in (lector.fieldnames or [])]
    faltan = [c for c in _COLUMNAS_CSV_CONTEO_MINIMAS if c not in lector.fieldnames]
    if faltan:
        return [], [
            "El CSV debe traer al menos las columnas codigo y contado "
            "(usa la exportación de conteo como plantilla)."
        ]
    filas = []
    for numero, cruda in enumerate(lector, start=2):
        fila = {c: (cruda.get(c) or "").strip() for c in COLUMNAS_CSV_CONTEO}
        if not any(fila.values()):
            continue
        fila["numero"] = numero
        filas.append(fila)
    return filas, []


def _parsear_fila_conteo(fila, cliente, skus, lotes, ubicaciones):
    """Valida una fila del CSV y regresa el renglón de la previa (dict).

    Cada renglón trae `error` (bloquea el aplicar) o `avisos` (no bloquean),
    `omitir` (contado vacío o delta 0), `delta`, y los objetos resueltos.
    """
    renglon = {
        "numero": fila["numero"], "codigo": fila["codigo"], "descripcion": fila["descripcion"],
        "lote": fila["lote"], "caducidad": fila["caducidad"], "ubicacion": fila["ubicacion"],
        "contado": None, "vendible_actual": 0, "delta": 0,
        "error": "", "avisos": [], "omitir": False,
        "sku": None, "lote_obj": None, "ubicacion_obj": None, "fecha_caducidad": None,
        "apartado": 0, "en_empaque": 0, "cuarentena": 0, "en_recepcion": 0,
    }
    sku = skus.get(fila["codigo"])
    if sku is None:
        renglon["error"] = f"SKU desconocido '{fila['codigo']}' para este cliente."
        return renglon
    renglon["sku"] = sku
    if sku.es_kit:
        renglon["error"] = f"{sku.codigo} es un kit: no tiene stock propio, no se cuenta."
        return renglon
    if fila["descripcion"] and fila["descripcion"] != sku.descripcion:
        renglon["avisos"].append(
            f"El nombre en el archivo ('{fila['descripcion']}') no coincide con el catálogo "
            f"('{sku.descripcion}'): revisa que el renglón sea el correcto."
        )

    try:
        renglon["fecha_caducidad"] = parsear_fecha_csv(fila["caducidad"])
    except ValueError:
        renglon["error"] = f"Caducidad '{fila['caducidad']}' inválida: usa {FORMATOS_LEGIBLES}."
        return renglon

    if fila["ubicacion"]:
        ubic = ubicaciones.get(fila["ubicacion"].upper())
        if ubic is None:
            renglon["error"] = f"La ubicación {fila['ubicacion']} no existe."
            return renglon
        if ubic.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA) or not ubic.activo:
            renglon["error"] = f"La ubicación {ubic.codigo} no es un anaquel de picking/reserva activo."
            return renglon
        renglon["ubicacion_obj"] = ubic

    lotes_del_sku = lotes.get(sku.pk, {})
    if fila["lote"]:
        renglon["lote_obj"] = lotes_del_sku.get(fila["lote"])
    elif lotes_del_sku:
        # Sin lote en un SKU que maneja lotes: la previa lo deja pasar solo si
        # el renglón no cambia nada (p. ej. la fila vacía que exporta un SKU
        # con lotes pero sin stock); con diferencia, exige el lote.
        renglon["lote_requerido"] = ", ".join(sorted(lotes_del_sku))

    if fila["contado"] == "":
        renglon["omitir"] = True
        renglon["avisos"].append("Sin captura: se deja como está.")
    else:
        try:
            contado = int(fila["contado"])
        except ValueError:
            renglon["error"] = f"'{fila['contado']}' no es un número entero."
            return renglon
        if contado < 0:
            renglon["error"] = "El conteo no puede ser negativo."
            return renglon
        renglon["contado"] = contado
    return renglon


def previa_reconciliacion(cliente, filas):
    """Compara las filas del CSV contra el stock vendible actual del cliente.

    Regresa {"renglones", "errores", "avisos", "aplicables", "omitidos",
    "no_contados"}: `errores` bloquea; `no_contados` son SKU/lote/anaquel con
    stock que el archivo no menciona (se dejan intactos, se avisa). El vendible
    se indexa por (sku, lote, ubicación) para poder comparar por anaquel cuando
    la fila trae ubicación y por SKU/lote cuando no; los demás estados del saldo
    (apartado, en empaque, cuarentena, en recepción) solo se muestran.
    """
    from apps.catalogo.models import SKU

    skus = {s.codigo: s for s in SKU.objects.filter(cliente=cliente)}
    lotes = {}
    for lote in Lote.objects.filter(sku__cliente=cliente):
        lotes.setdefault(lote.sku_id, {})[lote.codigo] = lote
    ubicaciones = {u.codigo.upper(): u for u in Ubicacion.objects.all()}

    saldos = list(Saldo.objects.filter(sku__cliente=cliente, cantidad__gt=0))
    vendible_por_clave = {}
    otros = {}
    for s in saldos:
        if s.estado == Saldo.UBICADO_VENDIBLE:
            clave = (s.sku_id, s.lote_id, s.ubicacion_id)
            vendible_por_clave[clave] = vendible_por_clave.get(clave, 0) + s.cantidad
        else:
            otros.setdefault(s.sku_id, {})
            otros[s.sku_id][s.estado] = otros[s.sku_id].get(s.estado, 0) + s.cantidad

    renglones = []
    vistos = set()
    errores, avisos = [], []
    for fila in filas:
        r = _parsear_fila_conteo(fila, cliente, skus, lotes, ubicaciones)
        if r["sku"] is not None:
            extra = otros.get(r["sku"].pk, {})
            r["apartado"] = extra.get(Saldo.RESERVADO, 0)
            r["en_empaque"] = extra.get(Saldo.EN_EMPAQUE, 0)
            r["cuarentena"] = extra.get(Saldo.CUARENTENA, 0)
            r["en_recepcion"] = extra.get(Saldo.EN_PUTAWAY, 0)
        if not r["error"] and r["sku"] is not None:
            lote_id = r["lote_obj"].pk if r["lote_obj"] else None
            ubic_id = r["ubicacion_obj"].pk if r["ubicacion_obj"] else None
            clave = (r["sku"].pk, lote_id, ubic_id)
            if clave in vistos:
                r["error"] = "Renglón repetido: el mismo SKU/lote/ubicación aparece más de una vez."
            vistos.add(clave)
            if ubic_id is not None:
                r["vendible_actual"] = vendible_por_clave.get(clave, 0)
            else:
                # Sin ubicación: se compara contra TODO el vendible de ese SKU/lote.
                r["vendible_actual"] = sum(
                    c for (sid, lid, _u), c in vendible_por_clave.items()
                    if sid == r["sku"].pk and lid == lote_id
                )
            if r.get("lote_requerido") and not r["omitir"]:
                # Sin lote: se compara contra el vendible de TODOS los lotes del SKU
                # (en ese anaquel, si lo trae). Solo pasa si no hay diferencia.
                r["vendible_actual"] = sum(
                    c for (sid, _l, u), c in vendible_por_clave.items()
                    if sid == r["sku"].pk and (ubic_id is None or u == ubic_id)
                )
                if r["contado"] == r["vendible_actual"]:
                    r["omitir"] = True
                    r["avisos"].append(f"Sin cambio. Maneja lotes ({r['lote_requerido']}): con diferencia, captura el lote.")
                else:
                    r["error"] = (
                        f"{r['sku'].codigo} maneja lotes ({r['lote_requerido']}): "
                        "captura el lote en cada renglón."
                    )
        if r["error"]:
            errores.append(f"fila {r['numero']}: {r['error']}")
        elif not r["omitir"]:
            r["delta"] = r["contado"] - r["vendible_actual"]
            if r["delta"] == 0:
                r["omitir"] = True
                r["avisos"].append("Sin cambio.")
            else:
                if fila["vendible_actual"] not in ("", str(r["vendible_actual"])):
                    r["avisos"].append(
                        f"El archivo decía {fila['vendible_actual']} vendibles y hoy hay "
                        f"{r['vendible_actual']}: el stock se movió desde la exportación."
                    )
                reservado = r["apartado"]
                if reservado and r["contado"] < reservado and r["ubicacion_obj"] is None:
                    r["avisos"].append(
                        f"Hay {reservado} piezas apartadas para pedidos abiertos y el conteo "
                        f"es {r['contado']}: esos pedidos quedarán sin respaldo."
                    )
        renglones.append(r)
        for aviso in r["avisos"]:
            if not r["error"]:
                avisos.append(f"fila {r['numero']}: {aviso}")

    no_contados = []
    skus_por_id = {s.pk: s for s in skus.values()}
    lotes_por_id = {l.pk: l for d in lotes.values() for l in d.values()}
    ubic_por_id = {u.pk: u for u in ubicaciones.values()}
    mencionados_sku_lote = {(r["sku"].pk, r["lote_obj"].pk if r["lote_obj"] else None)
                            for r in renglones if r["sku"] is not None and not r["error"]}
    for (sid, lid, uid), cantidad in sorted(vendible_por_clave.items()):
        if (sid, lid, uid) in vistos or ((sid, lid) in mencionados_sku_lote and not any(
            r["ubicacion_obj"] is not None for r in renglones
            if r["sku"] is not None and r["sku"].pk == sid
        )):
            continue
        lote = lotes_por_id.get(lid)
        no_contados.append({
            "codigo": skus_por_id[sid].codigo, "lote": lote.codigo if lote else "",
            "ubicacion": ubic_por_id[uid].codigo, "vendible_actual": cantidad,
        })

    aplicables = [r for r in renglones if not r["error"] and not r["omitir"]]
    return {
        "renglones": renglones, "errores": errores, "avisos": avisos,
        "aplicables": aplicables,
        "omitidos": sum(1 for r in renglones if r["omitir"] and not r["error"]),
        "no_contados": no_contados,
    }


def _validar_admin_mesa(actor):
    """La reconciliación por CSV la firma Mesa de Control como admin (sin doble
    PIN): el actor debe ser un usuario con rol mesa o superusuario."""
    perfil = getattr(actor, "perfil", None)
    es_mesa = perfil is not None and perfil.rol == "mesa"
    if not getattr(actor, "username", "") or not (es_mesa or getattr(actor, "is_superuser", False)):
        raise ValueError("La reconciliación por CSV la aplica Mesa de Control.")
    return actor


def reconciliar_conteo(cliente, filas, motivo, actor, nota="", archivo=""):
    """Aplica la reconciliación: un Ajuste (con Conteo ligado) por renglón con delta,
    todo o nada. La firma es el usuario de Mesa que aplica (`actor`), tras
    confirmar el resumen en pantalla; no pide doble PIN (Mesa opera como admin).

    Todo renglón contado deja Conteo, también los que coinciden con el sistema
    (sin ajuste): la reconciliación ES un conteo físico, así el "último conteo"
    del SKU se actualiza y la tarea de conteo cíclico del día queda completada.
    NO abre incidencias DES: una reconciliación es, por definición, una lista de
    descuadres ya asumidos. Regresa {"ajustes", "omitidos", "conteos", "folios"}.
    """
    if motivo not in dict(Ajuste.MOTIVOS):
        validos = ", ".join(clave for clave, _ in Ajuste.MOTIVOS)
        raise ValueError(f"Motivo '{motivo}' fuera del catálogo. Válidos: {validos}.")
    previa = previa_reconciliacion(cliente, filas)
    if previa["errores"]:
        raise ValueError(
            "El archivo trae renglones con error; corrígelos antes de aplicar: "
            + "; ".join(previa["errores"][:5])
            + (" …" if len(previa["errores"]) > 5 else "")
        )
    if not previa["aplicables"]:
        raise ValueError("Ningún renglón cambia el stock: no hay nada que aplicar.")
    firmante = _validar_admin_mesa(actor)

    folios = []
    with transaction.atomic():
        for r in previa["aplicables"]:
            sku = r["sku"]
            lote = r["lote_obj"]
            if lote is None and r["lote"]:
                lote, creado = Lote.objects.get_or_create(
                    sku=sku, codigo=r["lote"], defaults={"fecha_caducidad": r["fecha_caducidad"]},
                )
            if lote is not None and lote.fecha_caducidad is None and r["fecha_caducidad"]:
                lote.fecha_caducidad = r["fecha_caducidad"]
                lote.save(update_fields=["fecha_caducidad"])
            conteo = _conteo_reconciliacion(r, actor)
            ajuste = _aplicar_ajuste_firmado(
                sku, r["delta"], motivo, firmante, firmante,
                lote=lote, conteo=conteo, ubicacion=r["ubicacion_obj"],
            )
            folios.append(ajuste.folio)
        # Renglones contados que coinciden con el sistema: conteo sin ajuste.
        sin_cambio = [
            r for r in previa["renglones"]
            if not r["error"] and r["sku"] is not None and r["contado"] is not None and r["omitir"]
        ]
        for r in sin_cambio:
            _conteo_reconciliacion(r, actor)
        registrar_evento(
            "inventario", cliente.slug, "reconciliacion_csv", actor=actor, cliente=cliente,
            delta={
                "archivo": archivo, "renglones": len(previa["renglones"]),
                "ajustes": len(folios), "omitidos": previa["omitidos"],
                "conteos": len(folios) + len(sin_cambio),
                "no_contados": len(previa["no_contados"]), "folios": folios,
                "firma": firmante.username, "doble_firma": False,
                "avisos": previa["avisos"][:50],
            },
            motivo=(nota or f"Reconciliación de inventario por CSV ({archivo})")[:300],
        )
    return {
        "ajustes": len(folios), "omitidos": previa["omitidos"],
        "conteos": len(folios) + len(sin_cambio), "folios": folios,
    }


def _conteo_reconciliacion(renglon, actor):
    """Conteo de un renglón de la reconciliación (esperado = vendible que se
    comparó, contado = lo capturado) y cierre de la tarea cíclica del día del
    SKU si estaba pendiente. Sin umbral ni DES: eso lo decide la reconciliación."""
    conteo = Conteo.objects.create(
        sku=renglon["sku"], contador=_actor_str(actor) or "reconciliacion",
        esperado=renglon["vendible_actual"], contado=renglon["contado"],
    )
    TareaConteo.objects.filter(
        sku=renglon["sku"], fecha=timezone.localdate(), estado=TareaConteo.PENDIENTE,
    ).update(estado=TareaConteo.COMPLETADA, conteo=conteo)
    return conteo


# ── Reacomodo de racks ──

TIPO_POR_PREFIJO = {"PIC": Ubicacion.PICKING, "RES": Ubicacion.RESERVA}


def mover_ubicacion(codigo_origen, codigo_destino, actor=None):
    """Mueve TODO lo que hay en una ubicación a otra, conservando SKU, lote y
    estado de cada saldo (reacomodo de racks, sep-2026).

    - Destino inexistente → la ubicación se RENOMBRA: misma ficha, mismo
      inventario, apartados incluidos, cero movimientos. El tipo se infiere del
      prefijo del código nuevo (PIC → picking, RES → reserva) si lo tiene.
    - Destino existente → los saldos se FUSIONAN en el destino (misma llave
      sku/lote/estado suma cantidades) y la origen queda inactiva.
    Sin líneas de kardex: la cantidad y el estado del stock no cambian; queda
    el evento de auditoría `ubicacion_movida` con el detalle. Regresa
    {"modo": "renombrada" | "fusionada", "piezas", "saldos"}.
    """
    origen = Ubicacion.objects.get(codigo=codigo_origen)
    if codigo_origen == codigo_destino:
        raise ValueError(f"{codigo_origen}: origen y destino son la misma ubicación.")
    with transaction.atomic():
        destino = Ubicacion.objects.filter(codigo=codigo_destino).first()
        saldos = list(Saldo.objects.select_for_update().filter(ubicacion=origen).select_related("sku"))
        piezas = sum(s.cantidad for s in saldos)
        if destino is None:
            origen.codigo = codigo_destino
            tipo = TIPO_POR_PREFIJO.get(codigo_destino.split("-")[0].upper())
            if tipo:
                origen.tipo = tipo
            origen.save(update_fields=["codigo", "tipo"])
            modo = "renombrada"
        else:
            if not destino.activo:
                raise ValueError(f"{codigo_destino} está inactiva; actívala antes de mover ahí.")
            for saldo in saldos:
                if saldo.cantidad:
                    _incrementar(saldo.sku, destino.pk, saldo.lote_id, saldo.estado, saldo.cantidad)
                saldo.delete()
            origen.activo = False
            origen.save(update_fields=["activo"])
            modo = "fusionada"
        registrar_evento(
            "ubicacion", codigo_origen, "ubicacion_movida", actor=actor,
            delta={"a": codigo_destino, "modo": modo, "piezas": piezas, "saldos": len(saldos)},
            motivo=f"Reacomodo de racks: {codigo_origen} → {codigo_destino} ({modo}, {piezas} pieza(s)).",
        )
    return {"modo": modo, "piezas": piezas, "saldos": len(saldos)}


# ── Capacidad y ocupación de anaqueles (producto siempre parado) ──

def capacidad_sku_en(ubicacion, sku):
    """Piezas del SKU que caben PARADAS en la ubicación: huella (largo × ancho
    del anaquel entre las del SKU, o girado 90° sobre el piso, el mayor) por
    niveles (alto del anaquel entre alto del SKU, tope TORRE["APILADO_MAX"]).
    None = no se puede calcular (anaquel sin medidas, sin tope de alto como la
    reserva, o SKU sin medidas). 0 = no cabe ni una parada."""
    if not (ubicacion.largo_cm and ubicacion.ancho_cm and ubicacion.alto_cm):
        return None
    if not (sku.largo_cm and sku.ancho_cm and sku.alto_cm):
        return None
    if sku.alto_cm > ubicacion.alto_cm:
        return 0
    niveles = min(ubicacion.alto_cm // sku.alto_cm, int(settings.TORRE.get("APILADO_MAX", 6)))
    huella = max(
        (ubicacion.largo_cm // sku.largo_cm) * (ubicacion.ancho_cm // sku.ancho_cm),
        (ubicacion.largo_cm // sku.ancho_cm) * (ubicacion.ancho_cm // sku.largo_cm),
    )
    return huella * niveles


def ocupacion(ubicacion, saldos=None, extra=None):
    """Ocupación ESTIMADA del anaquel: cada SKU consume piezas / su capacidad
    sola; la suma es la fracción ocupada. `extra` = (sku, piezas) que se
    quieren agregar (para avisar antes de ubicar). Regresa {"pct", "estado"
    (libre|medio|lleno|ilimitado|sin_medidas), "sin_medidas": [códigos de SKU],
    "no_caben": [códigos], "por_sku": [{sku, piezas, capacidad}]}."""
    if saldos is None:
        saldos = Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0).select_related("sku")
    piezas_por_sku, lotes_por_sku = {}, {}
    for s in saldos:
        piezas_por_sku.setdefault(s.sku, 0)
        piezas_por_sku[s.sku] += s.cantidad
        if s.lote_id:
            lotes_por_sku.setdefault(s.sku, set()).add(s.lote.codigo)
    for item in (extra if isinstance(extra, list) else ([extra] if extra else [])):
        sku_extra, n = item[0], item[1]
        piezas_por_sku[sku_extra] = piezas_por_sku.get(sku_extra, 0) + n
        if len(item) > 2 and item[2]:
            lotes_por_sku.setdefault(sku_extra, set()).add(item[2])
    if not (ubicacion.largo_cm and ubicacion.ancho_cm):
        return {"pct": None, "estado": "sin_medidas", "sin_medidas": [], "no_caben": [], "por_sku": []}
    if not ubicacion.alto_cm:
        return {"pct": None, "estado": "ilimitado", "sin_medidas": [], "no_caben": [], "por_sku": []}
    fraccion, sin_medidas, no_caben, por_sku = 0.0, [], [], []
    for sku, piezas in piezas_por_sku.items():
        cap = capacidad_sku_en(ubicacion, sku)
        por_sku.append({"sku": sku, "piezas": piezas, "capacidad": cap, "lotes": sorted(lotes_por_sku.get(sku, ()))})
        if cap is None:
            sin_medidas.append(sku.codigo)
        elif cap == 0:
            no_caben.append(sku.codigo)
            fraccion += 1.0
        else:
            fraccion += piezas / cap
    pct = round(fraccion * 100)
    torre = settings.TORRE
    if pct >= int(torre.get("OCUPACION_LLENO_PCT", 90)):
        estado = "lleno"
    elif pct >= int(torre.get("OCUPACION_MEDIO_PCT", 60)):
        estado = "medio"
    else:
        estado = "libre"
    return {"pct": pct, "estado": estado, "sin_medidas": sin_medidas, "no_caben": no_caben, "por_sku": por_sku}


def ocupaciones(ubicaciones, reservas=None):
    """{código: ocupacion(...)} de varias ubicaciones con una sola consulta de
    saldos. `reservas` = {código: [(sku, piezas)]} apartadas de forma virtual
    (un plan en curso) que se suman a lo físico."""
    por_ubicacion = {u.pk: [] for u in ubicaciones}
    for s in Saldo.objects.filter(ubicacion__in=ubicaciones, cantidad__gt=0).select_related("sku"):
        por_ubicacion[s.ubicacion_id].append(s)
    reservas = reservas or {}
    return {u.codigo: ocupacion(u, por_ubicacion[u.pk], extra=reservas.get(u.codigo)) for u in ubicaciones}


def aviso_capacidad(ubicacion, sku, cantidad):
    """Texto de aviso si ubicar `cantidad` del SKU deja el anaquel lleno o el
    SKU no cabe parado; "" si no hay nada que avisar. Nunca bloquea."""
    cap = capacidad_sku_en(ubicacion, sku)
    if cap == 0:
        return (
            f"Ojo: {sku.codigo} no cabe parado en {ubicacion.codigo} "
            f"(alto {sku.alto_cm} cm contra {ubicacion.alto_cm} cm libres)."
        )
    if cap is None:
        return ""
    despues = ocupacion(ubicacion, extra=(sku, cantidad))
    if despues["estado"] == "lleno":
        return f"Ojo: {ubicacion.codigo} queda al {despues['pct']} % (estimado, producto parado)."
    return ""


# ── Acomodo sugerido (put-away) ──

def espacio_para(ubicacion, sku, ocup):
    """Piezas del SKU que todavía caben en el anaquel según su ocupación
    estimada `ocup` (de ocupacion()); 0 si no cabe o el anaquel ya está lleno."""
    cap = capacidad_sku_en(ubicacion, sku)
    if not cap:
        return 0
    fraccion = 0.0
    for fila in ocup.get("por_sku", []):
        if fila["capacidad"]:
            fraccion += fila["piezas"] / fila["capacidad"]
        elif fila["capacidad"] == 0:
            fraccion += 1.0
    return max(0, int(cap * (1 - fraccion)))


def sugerir_anaquel(sku, cantidad, reservas=None, lote=None):
    """Plan de put-away para `cantidad` piezas del SKU: [{ubicacion, cantidad,
    motivo}], en orden. Reglas (plan 2026-09-17): 1) los anaqueles donde ya
    vive el SKU y les cabe (no dispersar), por prioridad; 2) anaqueles vacíos
    según la clase de rotación del SKU: A los de mejor prioridad, C los de
    peor, B desde la mitad; solo picking activo con medidas y prioridad, nunca
    la reserva ni los marcados llenos a mano. Si no alcanza, el último renglón
    va con ubicacion=None y lo que quedó sin lugar. [] si el SKU no tiene
    medidas (no hay forma de saber cuánto cabe). `reservas` = {código:
    [(sku, piezas, lote)]} ya apartadas por un plan en curso (planear_acomodo).
    `lote` (código): los lotes de un mismo SKU no se mezclan en un anaquel —
    "ya vive el SKU" exige el mismo lote, y un anaquel con otro lote del SKU no
    es candidato (Chema 2026-09-17)."""
    from apps.catalogo.services import clase_rotacion  # lazy por contrato

    if cantidad <= 0 or not (sku.largo_cm and sku.ancho_cm and sku.alto_cm):
        return []
    anaqueles = list(
        Ubicacion.objects.filter(tipo=Ubicacion.PICKING, activo=True, prioridad__isnull=False, lleno_manual=False)
        .exclude(largo_cm=0).exclude(ancho_cm=0).exclude(alto_cm=0).order_by("prioridad", "codigo")
    )
    if not anaqueles:
        return []
    ocup = ocupaciones(anaqueles, reservas)
    plan, restante = [], cantidad

    def agrega(u, n, motivo):
        nonlocal restante
        plan.append({"ubicacion": u, "cantidad": n, "motivo": motivo})
        restante -= n

    def _mismo_lote(fila):
        return fila["sku"].pk == sku.pk and (not lote or not fila["lotes"] or lote in fila["lotes"])

    con_sku = [u for u in anaqueles if any(_mismo_lote(f) for f in ocup[u.codigo]["por_sku"])]
    for u in con_sku:
        if restante <= 0:
            break
        libre = espacio_para(u, sku, ocup[u.codigo])
        if libre > 0:
            agrega(u, min(libre, restante), f"ya tiene este SKU{' y lote' if lote else ''} · caben {libre} más")
    if restante > 0:
        vacios = [u for u in anaqueles if not ocup[u.codigo]["por_sku"] and capacidad_sku_en(u, sku)]
        clase = clase_rotacion(sku)
        if clase == "C":
            vacios = list(reversed(vacios))
        elif clase == "B":
            mitad = len(vacios) // 2
            vacios = vacios[mitad:] + vacios[:mitad]
        for u in vacios:
            if restante <= 0:
                break
            cap = capacidad_sku_en(u, sku)
            agrega(u, min(cap, restante), f"anaquel libre para clase {clase} · caben {cap}")
    if restante > 0:
        plan.append({"ubicacion": None, "cantidad": restante, "motivo": "sin anaquel con espacio"})
    return plan


def marcar_anaquel(ubicacion, lleno, actor=None):
    """El piso marca el anaquel lleno (deja de sugerirse) o con espacio; con auditoría."""
    if ubicacion.lleno_manual == bool(lleno):
        return False
    ubicacion.lleno_manual = bool(lleno)
    ubicacion.save(update_fields=["lleno_manual"])
    registrar_evento(
        "ubicacion", ubicacion.codigo, "anaquel_lleno" if lleno else "anaquel_con_espacio", actor=actor,
        motivo="Marcado por el piso al contar." if lleno else "Liberado por el piso al contar.",
    )
    return True


# ── Plan de acomodo por orden de entrada (recepción pieza por pieza) ──

def planear_acomodo(orden, actor=None):
    """Plan de put-away de TODA la orden con lo que falta por ubicar de cada
    línea: lo anunciado que no ha llegado más lo que ya está en recepción
    (en_putaway) de ese SKU. Las líneas se planean de clase A a C para que la
    alta rotación tome los mejores anaqueles, apartando virtualmente lo que
    cada SKU va ocupando (sin doble asignación). Lo que no cabe queda como
    paso sin anaquel: va a cuarentena (ilimitada, decisión de Chema
    2026-09-17). Se guarda en orden.plan_acomodo y se audita."""
    from apps.catalogo.services import clases_rotacion  # lazy por contrato

    clases = clases_rotacion(orden.cliente)
    lineas = list(orden.lineas.select_related("sku"))
    lineas.sort(key=lambda l: (clases.get(l.sku_id, "C"), l.sku.codigo, l.lote_codigo or ""))
    # Lo ya ubicado según el plan anterior, por (sku, lote): no se vuelve a
    # planear. Un plan viejo sin lote acredita sus ubicadas a las líneas del
    # SKU en orden. Y el total por SKU se limita a lo que FÍSICAMENTE falta
    # (anunciado no llegado + en recepción): lo ubicado a mano tampoco se
    # replanea aunque el plan no lo haya visto.
    previo = orden.plan_acomodo or {}
    ubicadas_previas = dict(previo.get("ubicadas", {}))
    for p in previo.get("pasos", []):
        clave = f"{p['sku_id']}|{p.get('lote') or ''}"
        ubicadas_previas[clave] = ubicadas_previas.get(clave, 0) + p.get("ubicadas", 0)
    sin_lote = {}
    for clave, n in list(ubicadas_previas.items()):
        sku_id, lote = clave.split("|", 1)
        if not lote and n:
            sin_lote[int(sku_id)] = sin_lote.get(int(sku_id), 0) + n
            ubicadas_previas.pop(clave)
    fisico = {}
    for linea in lineas:
        f = fisico.setdefault(linea.sku_id, {"anunciadas": 0, "recibidas": 0})
        f["anunciadas"] += linea.cantidad_anunciada
        f["recibidas"] += linea.cantidad_recibida
    tope = {
        sku_id: max(f["anunciadas"] - f["recibidas"], 0) + _suma(SKU_por_id(sku_id, lineas), Saldo.EN_PUTAWAY)
        for sku_id, f in fisico.items()
    }
    reservas, pasos, acreditadas = {}, [], {}
    for linea in lineas:
        sku = linea.sku
        lote = (linea.lote_codigo or "").strip()
        clave = f"{sku.pk}|{lote}"
        previas = ubicadas_previas.get(clave, 0)
        if sin_lote.get(sku.pk):  # plan viejo sin lote: se acredita en orden
            extra = min(sin_lote[sku.pk], max(linea.cantidad_anunciada - previas, 0))
            previas += extra
            sin_lote[sku.pk] -= extra
        pendientes = max(linea.cantidad_anunciada - previas, 0)
        pendientes = min(pendientes, max(tope.get(sku.pk, 0) - sum(
            p["cantidad"] for p in pasos if p["sku_id"] == sku.pk
        ), 0))
        acreditadas[clave] = previas
        if pendientes <= 0:
            continue
        plan = sugerir_anaquel(sku, pendientes, reservas, lote=lote or None)
        if not plan:  # sin medidas: todo a cuarentena hasta que Mesa las capture
            plan = [{"ubicacion": None, "cantidad": pendientes, "motivo": "sin medidas del producto: a cuarentena"}]
        for paso in plan:
            u = paso["ubicacion"]
            if u is not None:
                reservas.setdefault(u.codigo, []).append((sku, paso["cantidad"], lote or None))
            motivo = paso["motivo"] if u is not None else "sin anaquel con espacio: a cuarentena"
            pasos.append({
                "sku_id": sku.pk, "sku": sku.codigo, "lote": lote, "ubicacion": u.codigo if u else None,
                "cantidad": paso["cantidad"], "ubicadas": 0, "motivo": motivo,
            })
    orden.plan_acomodo = {
        "generado": timezone.now().isoformat(), "pasos": pasos,
        "ubicadas": {clave: n for clave, n in acreditadas.items() if n},  # ya ubicadas antes de este plan
    }
    orden.save(update_fields=["plan_acomodo"])
    registrar_evento(
        "asn", orden.folio, "plan_acomodo", actor=actor, cliente=orden.cliente,
        delta={"pasos": [(p["sku"], p["ubicacion"], p["cantidad"]) for p in pasos]},
        motivo=f"Plan de acomodo de {orden.folio}: {len(pasos)} paso(s).",
    )
    return orden.plan_acomodo


def SKU_por_id(sku_id, lineas):
    """El SKU de las líneas de la orden por id (evita otra consulta)."""
    return next(l.sku for l in lineas if l.sku_id == sku_id)


def siguiente_paso(orden, sku, lote=None):
    """Paso del plan que sigue para ese SKU y lote (el primero con piezas por
    ubicar); None si el plan no lo contempla (se rehace o se sugiere ad hoc)."""
    lote = (lote or "").strip()
    for paso in (orden.plan_acomodo or {}).get("pasos", []):
        if paso["sku_id"] == sku.pk and (paso.get("lote") or "") == lote and paso["ubicadas"] < paso["cantidad"]:
            return paso
    return None


def _avanzar_plan(orden, sku, codigo_ubicacion, lote=None):
    """Marca una pieza ubicada en el paso que corresponde (el del anaquel
    elegido si existe y le faltan; si no, el siguiente del SKU y lote)."""
    lote = (lote or "").strip()
    pasos = (orden.plan_acomodo or {}).get("pasos", [])
    candidatos = [
        p for p in pasos
        if p["sku_id"] == sku.pk and (p.get("lote") or "") == lote and p["ubicadas"] < p["cantidad"]
    ]
    elegido = next((p for p in candidatos if p["ubicacion"] == codigo_ubicacion), None) or (candidatos[0] if candidatos else None)
    if elegido is not None:
        elegido["ubicadas"] += 1
        orden.save(update_fields=["plan_acomodo"])


def a_cuarentena_desde_recepcion(sku, cantidad, referencia, actor, motivo=""):
    """Piezas recibidas (en_putaway) que no tienen anaquel: pasan a cuarentena
    en la zona de recepción, con kardex y evento."""
    cantidad = _validar_cantidad(cantidad, "cuarentena")
    with transaction.atomic():
        en_putaway = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY))
        if sum(s.cantidad for s in en_putaway) < cantidad:
            raise ValueError(f"No hay {cantidad} piezas de {sku.codigo} en recepción.")
        _restar(en_putaway, cantidad)
        ubic = _ubicacion_tipo(Ubicacion.RECEPCION) or en_putaway[0].ubicacion
        _incrementar(sku, ubic.pk, None, Saldo.CUARENTENA, cantidad)
        _mov(sku, Movimiento.PUTAWAY, 0, origen=Saldo.EN_PUTAWAY, destino=Saldo.CUARENTENA, referencia=referencia, actor=actor)
        registrar_evento(
            "sku", sku.codigo, "a_cuarentena", actor=actor, cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia)}, motivo=motivo[:300],
        )


def ubicar_pieza(orden, sku, lote, ubicacion, actor):
    """Una pieza recién escaneada: a su anaquel (ubicar) o, sin anaquel
    (ubicacion=None), a cuarentena por falta de espacio; avanza el plan."""
    codigo_lote = lote.codigo if lote is not None else None
    if ubicacion is None:
        a_cuarentena_desde_recepcion(sku, 1, orden.folio, actor, motivo="Sin anaquel con espacio en el plan de acomodo.")
        _avanzar_plan(orden, sku, None, codigo_lote)
        return None
    ubicar(sku, 1, ubicacion, lote, actor)
    _avanzar_plan(orden, sku, ubicacion.codigo, codigo_lote)
    return ubicacion


def marcar_danada(linea_asn, actor):
    """Una pieza ya contada como buena resulta dañada al ubicarla: sale de
    recepción, entra a cuarentena y la línea la pasa de recibida a dañada."""
    sku = linea_asn.sku
    with transaction.atomic():
        en_putaway = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY))
        if sum(s.cantidad for s in en_putaway) < 1:
            raise ValueError(f"No hay piezas de {sku.codigo} en recepción para marcar dañadas.")
        if linea_asn.cantidad_recibida < 1:
            raise ValueError(f"La línea de {sku.codigo} no tiene piezas recibidas que marcar.")
        _restar(en_putaway, 1)
        ubic = _ubicacion_tipo(Ubicacion.RECEPCION) or en_putaway[0].ubicacion
        _incrementar(sku, ubic.pk, None, Saldo.CUARENTENA, 1)
        _mov(sku, Movimiento.RECEPCION, 0, origen=Saldo.EN_PUTAWAY, destino=Saldo.CUARENTENA, referencia=linea_asn.orden.folio, actor=actor)
        linea_asn.cantidad_recibida -= 1
        linea_asn.cantidad_danada += 1
        linea_asn.save(update_fields=["cantidad_recibida", "cantidad_danada"])
        registrar_evento(
            "asn", linea_asn.orden.folio, "pieza_danada", actor=actor, cliente=linea_asn.orden.cliente,
            delta={"sku": sku.codigo}, motivo="Marcada dañada al ubicar: de recibida a dañada, va a cuarentena.",
        )
    return linea_asn
