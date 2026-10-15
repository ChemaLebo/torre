"""Servicios de inventario: la ÚNICA puerta de entrada y salida del stock.

Contrato (CONVENTIONS.md §inventario). Ningún módulo toca Saldo/Movimiento
directo: todo pasa por aquí para que kardex, auditoría y push a Shopify cuadren.

Semántica clave (ver models.py): `reservado` es una capa sobre
`ubicado_vendible` — la unidad reservada sigue contada en su saldo vendible.
    disponible = suma(ubicado_vendible) − suma(reservado) − buffer del cliente
"""
import re
from datetime import date

from django.conf import settings
from django.db import transaction
from django.db.models import F, Max, Q, Sum
from django.utils import timezone

from apps.catalogo.models import Lote, Ubicacion
from apps.core.fechas import FORMATOS_LEGIBLES, parsear_fecha_csv
from apps.core.services import registrar_evento

from .models import Restock, Ajuste, Conteo, EscaneoRecepcion, LineaASN, Movimiento, OrdenEntrada, Saldo, TareaConteo

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
    _abrir_restock_si_falta(sku)
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


def _restar_detallado(saldos_ordenados, cantidad):
    """Como _restar, pero regresa [(ubicacion_id, lote_id, tomo)] de dónde salió."""
    restante, origen = cantidad, []
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
        origen.append((saldo.ubicacion_id, saldo.lote_id, tomo))
        restante -= tomo
    return origen


def confirmar_pick(sku, cantidad, referencia):
    """Pick confirmado: consume la reserva y mueve el físico vendible → en_empaque.

    Desde 2026-10-13 (racks con producto fijo) el físico sale PRIMERO de la
    posición fija del producto (FEFO dentro de ella), luego de la posición y
    lote de la capa de reserva, y al final FEFO en toda la bodega; lo que
    pasa a en_empaque conserva la posición y el lote de donde salió."""
    from apps.catalogo.services import rack_de  # lazy por contrato

    cantidad = _validar_cantidad(cantidad, "pickear")
    rack = rack_de(sku)
    rack_id = rack.pk if rack is not None else None
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
            # 2) baja el físico vendible: primero el rack fijo del producto (FEFO
            #    dentro de él), luego misma ubicación/lote de la capa, luego FEFO
            en_rack = sorted(
                (v for v in vendibles if v.cantidad > 0 and rack_id is not None and v.ubicacion_id == rack_id),
                key=lambda s: _clave_fefo(s, caducidades),
            )
            mismos = [
                v for v in vendibles
                if v.cantidad > 0 and v not in en_rack and (v.ubicacion_id, v.lote_id) == (capa.ubicacion_id, capa.lote_id)
            ]
            otros = sorted(
                (v for v in vendibles
                 if v.cantidad > 0 and v not in en_rack and (v.ubicacion_id, v.lote_id) != (capa.ubicacion_id, capa.lote_id)),
                key=lambda s: _clave_fefo(s, caducidades),
            )
            # 3) sube en_empaque conservando la posición y el lote de donde salió (trazabilidad)
            for ubicacion_id, lote_id, salio in _restar_detallado(en_rack + mismos + otros, tomo):
                _incrementar(sku, ubicacion_id, lote_id, Saldo.EN_EMPAQUE, salio)
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


def regresar_de_salida(sku, cantidad, referencia, actor, ubicacion=None):
    """Reverso exacto de `despachar` para una caja que firmó manifiesto pero
    nunca se fue ("Quitar de salida", Chema 2026-10-05): despachado →
    en_empaque, delta positivo. La caja sigue cerrada con su contenido, así
    que vuelve a EN_EMPAQUE (no a vendible) y la siguiente salida la vuelve a
    despachar. Cae en `ubicacion` (el corral del que salió) o, sin ella, en
    la primera de salida / recepción activa; sin lote (la salida tampoco lo
    lleva). Sin reintento de reservas: no entra stock vendible."""
    cantidad = _validar_cantidad(cantidad, "regresar de salida")
    ubic = ubicacion or _ubicacion_tipo(Ubicacion.SALIDA, Ubicacion.RECEPCION)
    if ubic is None:
        raise ValueError("No hay ubicación de salida ni de recepción activa para regresar la caja.")
    with transaction.atomic():
        _incrementar(sku, ubic.pk, None, Saldo.EN_EMPAQUE, cantidad)
        _mov(
            sku, Movimiento.RETORNO, cantidad,
            origen="despachado", destino=Saldo.EN_EMPAQUE, referencia=referencia, actor=actor,
        )
        registrar_evento(
            "sku", sku.codigo, "regreso_de_salida", actor=actor, cliente=sku.cliente,
            delta={"cantidad": cantidad, "referencia": str(referencia), "ubicacion": ubic.codigo},
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
    if orden.tipo == OrdenEntrada.TIPO_REINGRESO and orden.estado == OrdenEntrada.RECIBIDA:
        # La cancelación en bodega ya contó y metió las piezas a recepción
        # (2026-09-30): recibir otra vez las duplica; solo se ubican.
        raise ValueError(f"{orden.folio} es un reingreso ya contado: solo se ubica, no se vuelve a recibir.")

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
        filas = [{
            "sku": linea.sku, "anunciada": linea.cantidad_anunciada,
            "recibida": linea.cantidad_recibida, "danada": linea.cantidad_danada,
            "lote": (linea.lote_codigo or "").strip(), "caducidad": linea.fecha_caducidad,
        } for linea in orden.lineas.all()]
        # Producto anunciado sin lote: la madre trae el total y los lotes cuelgan de ella.
        orden.detalle_lineas = _agrupar_sin_lote(filas, ("anunciada", "recibida", "danada"))
        for f in orden.detalle_lineas:
            llegaron = f["recibida"] + f["danada"]
            f["diferencia"] = None if f["es_hija"] else llegaron - f["anunciada"]
            f["con_diferencia"] = orden.evaluada and not f["es_hija"] and (llegaron != f["anunciada"] or f["danada"] > 0)
        orden.fotos_llegada = fotos.get(orden.folio, [])
        orden.incidencias_orden = list(orden.incidencias.all())
    # Dónde quedó cada cosa (Chema 2026-10-06, solo lectura en el portal): las
    # abiertas y las 5 cerradas más recientes; el resto, desde el editor de Mesa.
    cerradas = 0
    for orden in ordenes:
        if orden.estado == OrdenEntrada.CERRADA:
            cerradas += 1
            if cerradas > 5:
                orden.realidad = []
                continue
        orden.realidad = realidad_recepcion(orden) if orden.lineas.exists() else []
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


# ─────────────────────────────────────────────────────────────────────────────
# Recepción por escaneo (Chema 2026-10-13): contar ≠ recibir
# ─────────────────────────────────────────────────────────────────────────────
# Cada caja se escanea una vez y queda CONTADA en una tarima (EscaneoRecepcion,
# sin stock). Tras imprimir y pegar etiquetas, el piso confirma tarima por
# tarima: las cajas pasan a RECIBIDAS y nace el saldo en_putaway (no vendible)
# en esa tarima; las dañadas, a cuarentena en la zona de recepción. Cerrar la
# orden vuelve vendible todo lo recibido y avisa a Shopify una sola vez.

PATRON_QR_PRODUCTO = re.compile(r"^P\|(?P<barras>[^|]*)\|(?P<lote>[^|]*)$")
PATRON_QR_POSICION = re.compile(r"^U\|(?P<codigo>[^|]+)$")
PATRON_CODIGO_POSICION = re.compile(r"^[A-Z]{2,4}-[A-Z0-9][A-Z0-9-]*$")


def normalizar_escaneo(valor):
    """Lo que tecleó el escáner, corregido si la tablet tenía el teclado en
    español: un lector configurado como teclado de EE. UU. manda `]` donde
    quería `|` y `'` donde quería `-`. Solo se corrige cuando el resultado
    tiene la forma de un código de Torre (QR de producto `P|barras|lote`, QR
    de posición `U|código` o un código de posición con guiones); ningún
    código nuestro lleva esos dos caracteres, así que no hay ambigüedad."""
    valor = (valor or "").strip()
    if "]" not in valor and "'" not in valor:
        return valor
    candidato = valor.replace("]", "|").replace("'", "-")
    if (
        PATRON_QR_PRODUCTO.match(candidato)
        or PATRON_QR_POSICION.match(candidato)
        or PATRON_CODIGO_POSICION.match(candidato.upper())
    ):
        return candidato
    return valor


def qr_producto(sku, lote_codigo):
    """Contenido del QR de la etiqueta de caja: `P|<código de barras>|<lote>`
    (sin código de barras va el código del SKU; sin lote queda vacío)."""
    return f"P|{sku.codigo_barras or sku.codigo}|{(lote_codigo or '').strip()}"


def qr_posicion(ubicacion):
    """Contenido del QR de una tarima o posición: `U|<código>`."""
    return f"U|{ubicacion.codigo}"


def _sku_por_codigo(cliente, valor):
    """SKU activo del cliente con ese código de barras o código. Un producto de
    otro cliente o uno que no existe son errores distintos para que el piso
    sepa qué pasó; un kit no se recibe (se arma al empacar)."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    filtro = Q(codigo_barras=valor) | Q(codigo__iexact=valor)
    sku = SKU.objects.filter(cliente=cliente, activo=True).filter(filtro).order_by("pk").first()
    if sku is None:
        ajeno = SKU.objects.filter(filtro).exclude(cliente=cliente).select_related("cliente").first()
        if ajeno is not None:
            raise ValueError(f"{valor} es un producto de {ajeno.cliente.nombre}, no de {cliente.nombre}.")
        raise ValueError(f"El código {valor} no está registrado. Pide a Mesa que dé de alta el producto.")
    if sku.es_kit:
        raise ValueError(f"{sku.codigo} es un kit: se arma al empacar, no se recibe.")
    return sku


def resolver_escaneo(orden, valor):
    """Qué producto es el código escaneado en esta orden. Acepta el código de
    barras o el código del SKU y el QR de una etiqueta de Torre (que además
    trae el lote). Regresa {"sku", "lineas" (las de la orden para ese SKU, en
    orden), "lote_qr" (código del lote si vino en el QR, si no None),
    "en_orden"}. Un producto del cliente que no venía en la orden NO es error:
    se agrega con anunciado 0 al contar la primera caja (Chema 2026-10-13)."""
    valor = normalizar_escaneo(valor)
    if not valor:
        raise ValueError("Escanea un código.")
    if PATRON_QR_POSICION.match(valor):
        raise ValueError("Eso es una etiqueta de posición, no de producto.")
    lote_qr = None
    qr = PATRON_QR_PRODUCTO.match(valor)
    if qr:
        valor, lote_qr = qr["barras"].strip(), qr["lote"].strip()
        if not valor:
            raise ValueError("El QR no trae el código del producto.")
    sku = _sku_por_codigo(orden.cliente, valor)
    lineas = list(orden.lineas.filter(sku=sku).select_related("sku").order_by("pk"))
    return {"sku": sku, "lineas": lineas, "lote_qr": lote_qr, "en_orden": bool(lineas)}


def es_tarima(ubicacion):
    """True si la ubicación es una tarima (reserva TAR-nn)."""
    return ubicacion is not None and ubicacion.tipo == Ubicacion.RESERVA and ubicacion.codigo.startswith(PREFIJO_TARIMA)


def _orden_abierta(orden):
    if orden.estado == OrdenEntrada.CERRADA:
        raise ValueError(f"La orden {orden.folio} ya está cerrada; no acepta más cajas.")


def _linea_bloqueada(linea_o_pk):
    """La línea fresca y bloqueada (SELECT FOR UPDATE): los contadores de
    recibidas/dañadas nunca se leen de la instancia que trae el llamador,
    que puede estar vieja (p. ej. varias cajas de la misma línea en un bucle)."""
    pk = getattr(linea_o_pk, "pk", linea_o_pk)
    return LineaASN.objects.select_for_update().select_related("sku", "orden").get(pk=pk)


def _lote_de_linea(linea, caducidad=None, crear=True):
    """El Lote de la línea (por su código); None si la línea no lleva lote.
    Con `crear` lo da de alta con la caducidad de la línea si no existía."""
    from apps.catalogo.services import obtener_o_crear_lote  # lazy por contrato

    codigo = (linea.lote_codigo or "").strip()
    if not codigo:
        return None
    if crear:
        return obtener_o_crear_lote(linea.sku, codigo, caducidad or linea.fecha_caducidad)
    return Lote.objects.filter(sku=linea.sku, codigo=codigo).first()


def contar_caja(orden, sku, lote_codigo, fecha_caducidad, tarima, actor, danada=False, desde_reetiquetado=False):
    """Una caja escaneada queda CONTADA en esa tarima (sin stock): la línea
    del producto y lote (se crea con anunciado 0 si el lote, o el producto,
    no venían en la orden; eventos `lote_no_anunciado` / `sku_no_anunciado`),
    el Lote del catálogo con su caducidad y el escaneo con quién lo hizo. El
    primer escaneo pasa la orden a EN_RECEPCION. Con el conteo terminado solo
    se acepta desde el reetiquetado ("Faltaron X") o desde el editor de Mesa.
    Una caja de una línea SIN lote no lleva etiqueta (Chema 2026-10-14): nace
    con `impresa_ts` para que ni cuente como "sin etiqueta" ni se imprima.
    Regresa el EscaneoRecepcion."""
    from apps.catalogo.services import obtener_o_crear_lote  # lazy por contrato

    _orden_abierta(orden)
    if orden.ts_conteo_fin and not desde_reetiquetado:
        raise ValueError(f"El conteo de {orden.folio} ya terminó: las cajas que faltaron se agregan desde Reetiquetado, o Regresa a conteo.")
    if sku.cliente_id != orden.cliente_id:
        raise ValueError(f"{sku.codigo} no es de {orden.cliente.nombre}.")
    if not es_tarima(tarima) or not tarima.activo:
        raise ValueError("Elige en qué tarima estás contando.")
    lote_codigo = (lote_codigo or "").strip()
    if sku.requiere_lote and not lote_codigo:
        raise ValueError(f"{sku.codigo} requiere lote: elige uno o captura el nuevo.")
    with transaction.atomic():
        linea = orden.lineas.filter(sku=sku, lote_codigo=lote_codigo).order_by("pk").first()
        if linea is None:
            habia = orden.lineas.filter(sku=sku).exists()
            if fecha_caducidad is None and lote_codigo:  # el QR trae el lote, no la fecha: la del catálogo
                conocido = Lote.objects.filter(sku=sku, codigo=lote_codigo).first()
                fecha_caducidad = conocido.fecha_caducidad if conocido else None
            linea = LineaASN.objects.create(
                orden=orden, sku=sku, cantidad_anunciada=0, lote_codigo=lote_codigo, fecha_caducidad=fecha_caducidad,
            )
            registrar_evento(
                "asn", orden.folio, "lote_no_anunciado" if habia else "sku_no_anunciado",
                actor=actor, cliente=orden.cliente, delta={"sku": sku.codigo, "lote": lote_codigo or None},
                motivo=(
                    f"Llegó {sku.codigo} con el lote {lote_codigo or '—'}, que no venía en la orden."
                    if habia else f"Llegó {sku.codigo}, que no venía en la orden: se agrega con 0 anunciadas."
                ),
            )
        elif fecha_caducidad and linea.fecha_caducidad is None:
            linea.fecha_caducidad = fecha_caducidad
            linea.save(update_fields=["fecha_caducidad"])
        if lote_codigo:
            obtener_o_crear_lote(sku, lote_codigo, fecha_caducidad or linea.fecha_caducidad)
        if orden.estado == OrdenEntrada.ANUNCIADA:
            orden.transicionar(OrdenEntrada.EN_RECEPCION, actor=actor, motivo="Inicio de descarga")
        escaneo = EscaneoRecepcion.objects.create(
            orden=orden, linea=linea, tarima=tarima, danada=bool(danada), actor=_actor_str(actor),
            impresa_ts=None if lleva_etiqueta(linea) else timezone.now(),
        )
    return escaneo


def lleva_etiqueta(linea):
    """Solo las cajas con lote llevan etiqueta de recepción (Chema 2026-10-14)."""
    return bool((linea.lote_codigo or "").strip())


def escaneos_vivos(orden):
    """Escaneos de la orden que cuentan: contadas y recibidas (no anuladas)."""
    return orden.escaneos.exclude(estado=EscaneoRecepcion.ANULADA)


def terminar_conteo(orden, actor):
    """El piso terminó de escanear (Chema 2026-10-13): la orden pasa a
    reetiquetado (ts_conteo_fin), el escaneo se bloquea y el llamador imprime
    todas las etiquetas (piso.etiquetas.imprimir_etiquetas_orden). Exige al
    menos una caja viva. Evento `conteo_terminado`. Regresa la orden."""
    _orden_abierta(orden)
    with transaction.atomic():
        orden = OrdenEntrada.objects.select_for_update().get(pk=orden.pk)
        if orden.ts_conteo_fin:
            raise ValueError(f"El conteo de {orden.folio} ya estaba terminado.")
        cajas = escaneos_vivos(orden).count()
        if not cajas:
            raise ValueError("Todavía no hay cajas contadas: escanea al menos una antes de terminar el conteo.")
        orden.ts_conteo_fin = timezone.now()
        orden.save(update_fields=["ts_conteo_fin"])
        registrar_evento(
            "asn", orden.folio, "conteo_terminado", actor=actor, cliente=orden.cliente,
            delta={"cajas": cajas, "tarimas": [t["tarima"].codigo for t in tarimas_de_orden(orden)]},
            motivo=f"Conteo terminado con {cajas} caja(s): se imprimen las etiquetas y sigue el reetiquetado.",
        )
    return orden


def regresar_a_conteo(orden, actor):
    """Terminar conteo se pulsó sin querer: la orden vuelve a conteo (las
    etiquetas ya impresas siguen impresas; al terminar otra vez solo salen
    las cajas nuevas). Evento `conteo_reabierto`."""
    _orden_abierta(orden)
    with transaction.atomic():
        orden = OrdenEntrada.objects.select_for_update().get(pk=orden.pk)
        if not orden.ts_conteo_fin:
            raise ValueError(f"{orden.folio} sigue en conteo.")
        orden.ts_conteo_fin = None
        orden.save(update_fields=["ts_conteo_fin"])
        registrar_evento(
            "asn", orden.folio, "conteo_reabierto", actor=actor, cliente=orden.cliente,
            motivo="Regresar a conteo: el piso vuelve a escanear; lo ya impreso sigue impreso.",
        )
    return orden


def _agrupar_sin_lote(filas, totales):
    """Anunciado sin lote (Chema 2026-10-13): cuando el cliente anuncia un
    producto sin lote y el piso cuenta sus cajas con el lote que traen, la
    línea sin lote es el TOTAL del producto y los lotes que llegaron cuelgan
    de ella (`hijas`), sin compararse uno por uno contra lo anunciado. Las
    filas vienen por línea; regresa la lista con la madre (`agrupada`)
    seguida de sus hijas (`es_hija`), sumando en la madre los campos
    `totales`. Un producto anunciado con lote no se agrupa: sus lotes nuevos
    sí son diferencias."""
    por_sku = {}
    for f in filas:
        por_sku.setdefault(f["sku"].pk, []).append(f)
    resultado, vistos = [], set()
    for f in filas:
        if f["sku"].pk in vistos:
            continue
        vistos.add(f["sku"].pk)
        grupo = por_sku[f["sku"].pk]
        madre = next((g for g in grupo if not g["lote"]), None)
        if madre is None or len(grupo) == 1:
            for g in grupo:
                g["agrupada"], g["es_hija"] = False, False
            resultado.extend(grupo)
            continue
        hijas = [g for g in grupo if g is not madre]
        for campo in totales:
            madre[campo] += sum(h[campo] for h in hijas)
        madre["agrupada"], madre["es_hija"], madre["hijas"] = True, False, hijas
        for h in hijas:
            h["agrupada"], h["es_hija"] = False, True
        resultado.append(madre)
        resultado.extend(hijas)
    return resultado


def resumen_lineas(orden, tarima=None):
    """Por línea (producto · lote), la cuenta viva: `contadas` (cajas buenas
    escaneadas), `danadas`, `recibidas` (confirmadas en tarima, buenas),
    `por_confirmar`, `llegaron` (contadas + dañadas), `diferencia` contra lo
    anunciado, `faltante`, `sobrante` y `sin_etiqueta`. Un producto anunciado
    sin lote agrupa sus lotes (ver _agrupar_sin_lote): la madre trae los
    totales y la diferencia; las hijas, solo su cuenta. Con `tarima`, la
    cuenta es solo de las cajas escaneadas en esa tarima (Chema 2026-10-13:
    la pantalla de conteo muestra lo de SU tarima); la diferencia contra lo
    anunciado entonces no significa nada y solo la usa el cuadre sin tarima."""
    vivos = {}
    escaneos = escaneos_vivos(orden)
    if tarima is not None:
        escaneos = escaneos.filter(tarima=tarima)
    for e in escaneos.values("linea_id", "estado", "danada", "impresa_ts"):
        r = vivos.setdefault(e["linea_id"], {"contadas": 0, "danadas": 0, "por_confirmar": 0, "sin_etiqueta": 0})
        r["danadas" if e["danada"] else "contadas"] += 1
        if e["estado"] == EscaneoRecepcion.CONTADA:
            r["por_confirmar"] += 1
        if e["impresa_ts"] is None:
            r["sin_etiqueta"] += 1
    filas = []
    for linea in orden.lineas.select_related("sku").order_by("sku__codigo", "lote_codigo", "pk"):
        r = vivos.get(linea.pk, {"contadas": 0, "danadas": 0, "por_confirmar": 0, "sin_etiqueta": 0})
        llegaron = r["contadas"] + r["danadas"]
        filas.append({
            "linea": linea, "sku": linea.sku, "lote": (linea.lote_codigo or "").strip(), "caducidad": linea.fecha_caducidad,
            "anunciadas": linea.cantidad_anunciada, "contadas": r["contadas"], "danadas": r["danadas"],
            "recibidas": linea.cantidad_recibida, "por_confirmar": r["por_confirmar"], "sin_etiqueta": r["sin_etiqueta"],
            "llegaron": llegaron, "diferencia": llegaron - linea.cantidad_anunciada,
            "faltante": max(linea.cantidad_anunciada - llegaron, 0), "sobrante": max(llegaron - linea.cantidad_anunciada, 0),
        })
    filas = _agrupar_sin_lote(filas, ("anunciadas", "contadas", "danadas", "recibidas", "por_confirmar", "sin_etiqueta", "llegaron"))
    for f in filas:
        if f["agrupada"]:
            f["diferencia"] = f["llegaron"] - f["anunciadas"]
            f["faltante"], f["sobrante"] = max(f["anunciadas"] - f["llegaron"], 0), max(f["llegaron"] - f["anunciadas"], 0)
        elif f["es_hija"]:
            f["diferencia"], f["faltante"], f["sobrante"] = None, 0, 0
    return filas


def tarimas_de_orden(orden):
    """Tarimas (o posiciones) donde hay cajas de la orden, en orden de primer
    uso: {tarima, cajas (vivas), por_confirmar, recibidas, danadas,
    sin_etiqueta, confirmada (hay cajas y ninguna por confirmar)}."""
    filas, orden_uso = {}, []
    for e in escaneos_vivos(orden).select_related("tarima").order_by("ts", "id"):
        if e.tarima_id not in filas:
            filas[e.tarima_id] = {"tarima": e.tarima, "cajas": 0, "por_confirmar": 0, "recibidas": 0, "danadas": 0, "sin_etiqueta": 0}
            orden_uso.append(e.tarima_id)
        r = filas[e.tarima_id]
        r["cajas"] += 1
        if e.estado == EscaneoRecepcion.CONTADA:
            r["por_confirmar"] += 1
        else:
            r["recibidas"] += 1
        if e.danada:
            r["danadas"] += 1
        if e.impresa_ts is None:
            r["sin_etiqueta"] += 1
    resultado = []
    for pk in orden_uso:
        r = filas[pk]
        r["confirmada"] = r["cajas"] > 0 and r["por_confirmar"] == 0
        resultado.append(r)
    return resultado


def escaneos_lifo(orden, tarima):
    """Las cajas vivas de la tarima en orden inverso al escaneo (LIFO, Chema
    2026-10-13): al contar se pasan de la tarima llena a la vacía, así que
    la última escaneada queda arriba y su etiqueta debe salir primero."""
    return list(
        escaneos_vivos(orden).filter(tarima=tarima)
        .select_related("linea__sku__cliente", "tarima", "orden__cliente").order_by("-ts", "-id")
    )


def resumen_etiquetas(orden, tarima):
    """{total, impresas, siguiente (posición LIFO de la primera sin imprimir
    o None), tamano} de las etiquetas de la tarima."""
    from apps.configuracion.services import tanda_etiquetas as _tamano  # lazy por contrato

    lifo = escaneos_lifo(orden, tarima)
    siguiente = next((i + 1 for i, e in enumerate(lifo) if e.impresa_ts is None), None)
    return {"total": len(lifo), "impresas": sum(1 for e in lifo if e.impresa_ts), "siguiente": siguiente, "tamano": _tamano()}


def tanda_etiquetas(orden, tarima, desde=None, tamano=None):
    """La siguiente tanda de etiquetas de la tarima en orden LIFO: sin `desde`,
    desde la primera caja sin imprimir ("Continuar impresión"); con `desde`
    (posición 1..total), reimpresión desde esa etiqueta. La etiqueta de la
    tarima va solo cuando la tanda empieza en la primera caja. Regresa
    {escaneos, incluye_tarima, desde, hasta, total, tamano, reimpresion}."""
    from apps.configuracion.services import tanda_etiquetas as _tamano  # lazy por contrato

    lifo = escaneos_lifo(orden, tarima)
    if not lifo:
        raise ValueError(f"No hay cajas contadas en {tarima.codigo}.")
    tamano = int(tamano or _tamano())
    if desde is None:
        indice = next((i for i, e in enumerate(lifo) if e.impresa_ts is None), None)
        if indice is None:
            raise ValueError(
                f"Ya salieron las {len(lifo)} etiquetas de {tarima.codigo}. Usa «Reimprimir desde» si te falta alguna."
            )
    else:
        desde = int(desde)
        if desde < 1 or desde > len(lifo):
            raise ValueError(f"En {tarima.codigo} hay {len(lifo)} caja(s): pide desde 1 hasta {len(lifo)}.")
        indice = desde - 1
    seleccion = lifo[indice:indice + tamano]
    return {
        "escaneos": seleccion, "incluye_tarima": indice == 0, "desde": indice + 1, "hasta": indice + len(seleccion),
        "total": len(lifo), "tamano": tamano, "reimpresion": desde is not None,
    }


def marcar_impresas(escaneos):
    """Las cajas quedan con su etiqueta impresa (hora de la tanda)."""
    EscaneoRecepcion.objects.filter(pk__in=[e.pk for e in escaneos]).update(impresa_ts=timezone.now())


def _recibir_escaneos(orden, tarima, pendientes, actor):
    """Las cajas CONTADAS `pendientes` (de esa tarima) quedan RECIBIDAS: nace
    el saldo en_putaway en la tarima con el lote de cada línea (kardex
    RECEPCION) y las dañadas van a cuarentena en la zona de recepción; la
    línea suma recibidas/dañadas. Stock que ya estaba en la zona de recepción
    sin tarima (reingreso de una cancelación en bodega, recepciones viejas)
    se toma de ahí en vez de crearse. Corre dentro de la transacción del
    llamador. Regresa {"cajas", "danadas", "lineas": [...]}."""
    zona = _ubicacion_tipo(Ubicacion.RECEPCION)
    por_linea = {}
    for e in pendientes:
        r = por_linea.setdefault(e.linea_id, {"buenas": 0, "danadas": 0})
        r["danadas" if e.danada else "buenas"] += 1
    detalle = []
    for linea_id, r in por_linea.items():
        linea = _linea_bloqueada(linea_id)
        sku = linea.sku
        lote = _lote_de_linea(linea)
        lote_id = lote.pk if lote else None
        if r["buenas"]:
            movidas = _tomar_de_recepcion(sku, lote, r["buenas"], tarima, orden.folio, actor)
            nuevas = r["buenas"] - movidas
            if nuevas:
                _incrementar(sku, tarima.pk, lote_id, Saldo.EN_PUTAWAY, nuevas)
                _mov(sku, Movimiento.RECEPCION, nuevas, lote=lote, destino=Saldo.EN_PUTAWAY, referencia=orden.folio, actor=actor)
            linea.cantidad_recibida += r["buenas"]
        if r["danadas"]:
            if zona is None:
                raise ValueError("No hay ubicación de recepción activa para las cajas dañadas. Da de alta una (tipo recepción).")
            _incrementar(sku, zona.pk, lote_id, Saldo.CUARENTENA, r["danadas"])
            _mov(sku, Movimiento.RECEPCION, r["danadas"], lote=lote, destino=Saldo.CUARENTENA, referencia=orden.folio, actor=actor)
            linea.cantidad_danada += r["danadas"]
        linea.save(update_fields=["cantidad_recibida", "cantidad_danada"])
        detalle.append({"sku": sku.codigo, "lote": (linea.lote_codigo or "").strip() or None, "cajas": r["buenas"], "danadas": r["danadas"]})
    EscaneoRecepcion.objects.filter(pk__in=[e.pk for e in pendientes]).update(estado=EscaneoRecepcion.RECIBIDA)
    return {"cajas": sum(d["cajas"] for d in detalle), "danadas": sum(d["danadas"] for d in detalle), "lineas": detalle}


def _tomar_de_recepcion(sku, lote, cantidad, tarima, folio, actor):
    """Mueve a la tarima hasta `cantidad` piezas del SKU que ya estaban en
    put-away en la zona de recepción (mismo lote primero, luego sin lote):
    es el stock de un reingreso contado por la cancelación en bodega, o de
    una recepción anterior al escaneo. Sin cambio de estado: kardex con delta
    0. Regresa cuántas movió."""
    zonas = list(Ubicacion.objects.filter(tipo=Ubicacion.RECEPCION).values_list("pk", flat=True))
    if not zonas:
        return 0
    saldos = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY, ubicacion_id__in=zonas, cantidad__gt=0))
    lote_id = lote.pk if lote else None
    saldos.sort(key=lambda s: (s.lote_id != lote_id, s.lote_id is not None, s.pk))
    mover = min(sum(s.cantidad for s in saldos), cantidad)
    if mover <= 0:
        return 0
    _restar(saldos, mover)
    _incrementar(sku, tarima.pk, lote_id, Saldo.EN_PUTAWAY, mover)
    _mov(sku, Movimiento.PUTAWAY, 0, lote=lote, origen=Saldo.EN_PUTAWAY, destino=Saldo.EN_PUTAWAY, referencia=folio, actor=actor)
    return mover


def confirmar_tarima(orden, tarima, actor):
    """La tarima cuadró: sus cajas contadas quedan recibidas (ver
    _recibir_escaneos). Evento `tarima_confirmada`. Regresa el resumen."""
    _orden_abierta(orden)
    with transaction.atomic():
        orden = OrdenEntrada.objects.select_for_update().get(pk=orden.pk)
        pendientes = list(
            escaneos_vivos(orden).filter(tarima=tarima, estado=EscaneoRecepcion.CONTADA)
            .select_related("linea__sku").order_by("ts", "id")
        )
        if not pendientes:
            raise ValueError(f"No hay cajas por confirmar en {tarima.codigo}.")
        resumen = _recibir_escaneos(orden, tarima, pendientes, actor)
        registrar_evento(
            "asn", orden.folio, "tarima_confirmada", actor=actor, cliente=orden.cliente,
            delta={"tarima": tarima.codigo, **resumen},
            motivo=f"{tarima.codigo} reetiquetada: {resumen['cajas']} caja(s) recibidas"
            + (f" y {resumen['danadas']} dañada(s) a cuarentena" if resumen["danadas"] else "") + ".",
        )
    return resumen


def _revertir_recibida(escaneo, actor):
    """Deshace el stock de UNA caja recibida: sale de la tarima (o de la
    cuarentena de recepción si era dañada) con kardex RECEPCION −1 y la
    línea baja. ValueError si la caja ya no está donde se recibió."""
    linea = _linea_bloqueada(escaneo.linea_id)
    sku = linea.sku
    lote = _lote_de_linea(linea, crear=False)
    if escaneo.danada:
        zonas = list(Ubicacion.objects.filter(tipo=Ubicacion.RECEPCION).values_list("pk", flat=True))
        saldos = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.CUARENTENA, ubicacion_id__in=zonas, lote=lote, cantidad__gt=0))
        origen, donde = Saldo.CUARENTENA, "la cuarentena de recepción"
    else:
        saldos = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY, ubicacion=escaneo.tarima, lote=lote, cantidad__gt=0))
        origen, donde = Saldo.EN_PUTAWAY, escaneo.tarima.codigo
    if sum(s.cantidad for s in saldos) < 1:
        raise ValueError(f"Esa caja de {sku.codigo} ya no está en {donde}: Mesa la movió o la dictaminó. Corrige desde el editor.")
    _restar(saldos, 1)
    _mov(sku, Movimiento.RECEPCION, -1, lote=lote, origen=origen, referencia=escaneo.orden.folio, actor=actor)
    if escaneo.danada:
        linea.cantidad_danada = max(linea.cantidad_danada - 1, 0)
    else:
        linea.cantidad_recibida = max(linea.cantidad_recibida - 1, 0)
    linea.save(update_fields=["cantidad_recibida", "cantidad_danada"])


def anular_escaneo(orden, linea, tarima, actor, motivo=""):
    """Resta UNA caja de esa línea en esa tarima: el último escaneo vivo
    queda ANULADO (etiqueta sobrante, Chema 2026-10-13). Si ya estaba
    recibida, el stock se revierte con kardex. Evento `escaneo_anulado`."""
    _orden_abierta(orden)
    with transaction.atomic():
        escaneo = (
            escaneos_vivos(orden).filter(linea=linea, tarima=tarima).select_related("linea__sku", "tarima")
            .order_by("-ts", "-id").select_for_update().first()
        )
        if escaneo is None:
            raise ValueError(f"No hay cajas de {linea.sku.codigo}{' lote ' + linea.lote_codigo if linea.lote_codigo else ''} contadas en {tarima.codigo}.")
        estaba = escaneo.estado
        if estaba == EscaneoRecepcion.RECIBIDA:
            _revertir_recibida(escaneo, actor)
        escaneo.estado = EscaneoRecepcion.ANULADA
        escaneo.anulada_ts = timezone.now()
        escaneo.anulada_por = _actor_str(actor)
        escaneo.save(update_fields=["estado", "anulada_ts", "anulada_por"])
        registrar_evento(
            "asn", orden.folio, "escaneo_anulado", actor=actor, cliente=orden.cliente,
            delta={"sku": linea.sku.codigo, "lote": (linea.lote_codigo or "").strip() or None, "tarima": tarima.codigo,
                   "danada": escaneo.danada, "estaba": estaba},
            motivo=(motivo or "Caja contada de más (etiqueta sobrante).")[:300],
        )
    return escaneo


def cuadre_recepcion(orden):
    """Lo que el cierre muestra: `lineas` (resumen_lineas), `espejo` (a un
    producto le faltan las mismas cajas que a otro le sobran: cajas
    idénticas cambiadas, la falla de ASN-0007), `tarimas`, `por_confirmar`
    (tarimas con cajas contadas sin confirmar), `sin_etiqueta` (cajas sin
    etiqueta impresa) y `con_diferencias`."""
    lineas = resumen_lineas(orden)
    faltan = [l for l in lineas if l["faltante"]]
    sobran = [l for l in lineas if l["sobrante"]]
    espejo = [
        {"falta": f["sku"].codigo, "sobra": s["sku"].codigo, "cajas": f["faltante"]}
        for f in faltan for s in sobran
        if f["sku"].pk != s["sku"].pk and f["faltante"] == s["sobrante"]
    ]
    tarimas = tarimas_de_orden(orden)
    return {
        "lineas": lineas, "espejo": espejo, "tarimas": tarimas,
        "por_confirmar": [t for t in tarimas if t["por_confirmar"]],
        "sin_etiqueta": sum(l["sin_etiqueta"] for l in lineas),
        "con_diferencias": any(l["diferencia"] or l["danadas"] for l in lineas),
    }


def _cuentas_para_cierre(lineas):
    """[{sku, anunciada, recibida, danada}] con lo confirmado de cada línea,
    agrupando bajo la línea sin lote los lotes del mismo producto (ver
    _agrupar_sin_lote): el cliente anunció el total, no los lotes."""
    filas = [{
        "sku": l.sku, "lote": (l.lote_codigo or "").strip(),
        "anunciada": l.cantidad_anunciada, "recibida": l.cantidad_recibida, "danada": l.cantidad_danada,
    } for l in lineas]
    return [
        {"sku": f["sku"].codigo, "anunciada": f["anunciada"], "recibida": f["recibida"], "danada": f["danada"]}
        for f in _agrupar_sin_lote(filas, ("anunciada", "recibida", "danada")) if not f["es_hija"]
    ]


def cerrar_recepcion(orden, actor, tarimas_recibidas=None):
    """Cierra la recepción: exige que no quede caja contada sin confirmar ni
    stock de la orden en la zona de recepción sin tarima, guarda las tarimas
    recibidas, vuelve VENDIBLE todo lo recibido (en_putaway → ubicado_vendible
    en su tarima o posición, kardex PUTAWAY) y avisa a Shopify una vez por
    producto; transiciona a CERRADA y hace la confirmación honesta.

    Si hubo faltantes o sobrantes contra lo anunciado, registra el evento
    `cerrada_con_discrepancia` y abre UNA incidencia DES con el desglose por
    SKU. Siempre avisa al cliente (lazy a mensajeria.enviar_recepcion_cerrada).

    Con faltantes la orden nunca llega sola a RECIBIDA: cerrar desde
    EN_RECEPCION pasa primero por RECIBIDA (la decisión honesta de "ya no va
    a llegar más") estampando la descarga con el último movimiento.
    """
    skus_vendibles = set()
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
        sin_confirmar = sorted({
            t for t in escaneos_vivos(orden).filter(estado=EscaneoRecepcion.CONTADA).values_list("tarima__codigo", flat=True)
        })
        if sin_confirmar:
            raise ValueError(
                f"Hay cajas contadas sin confirmar en {', '.join(sin_confirmar)}. Confirma cada tarima antes de cerrar."
            )
        zonas = list(Ubicacion.objects.filter(tipo=Ubicacion.RECEPCION).values_list("pk", flat=True))
        en_zona = (
            Saldo.objects.filter(
                sku_id__in=[linea.sku_id for linea in lineas], estado=Saldo.EN_PUTAWAY, ubicacion_id__in=zonas,
            ).aggregate(t=Sum("cantidad"))["t"] or 0
        )
        if en_zona > 0:
            raise ValueError(
                f"Aún hay {en_zona} pieza(s) en la zona de recepción sin tarima. Mesa las acomoda desde el editor antes de cerrar."
            )
        if tarimas_recibidas is not None:
            tarimas_recibidas = int(tarimas_recibidas)
            if tarimas_recibidas < 0:
                raise ValueError("Las tarimas recibidas no pueden ser negativas.")

        # Validado todo: recién aquí se escribe (nada muta si el cierre no procede).
        if tarimas_recibidas is not None:
            orden.tarimas_recibidas = tarimas_recibidas
            orden.save(update_fields=["tarimas_recibidas"])
        if orden.ts_conteo_fin is None:  # cerrar desde Mesa sin pasar por Terminar conteo
            orden.ts_conteo_fin = timezone.now()
            orden.save(update_fields=["ts_conteo_fin"])

        discrepancias = []
        for cuenta in _cuentas_para_cierre(lineas):
            llegado = cuenta["recibida"] + cuenta["danada"]
            faltante = max(cuenta["anunciada"] - llegado, 0)
            sobrante = max(llegado - cuenta["anunciada"], 0)
            if faltante or sobrante:
                discrepancias.append({**cuenta, "faltante": faltante, "sobrante": sobrante})

        # Vendible: lo recibido de cada línea (producto + lote) en su tarima o
        # posición pasa a ubicado_vendible ahí mismo (Chema 2026-10-13: nada
        # es vendible hasta cerrar). Dos órdenes abiertas del mismo producto y
        # lote se vuelven vendibles juntas: caso raro, aceptado.
        for linea in lineas:
            lote = _lote_de_linea(linea, crear=False)
            saldos = list(
                Saldo.objects.select_for_update().filter(
                    sku=linea.sku, estado=Saldo.EN_PUTAWAY, lote=lote, cantidad__gt=0,
                    ubicacion__tipo__in=(Ubicacion.PICKING, Ubicacion.RESERVA),
                ).select_related("ubicacion")
            )
            for saldo in saldos:
                n = saldo.cantidad
                _restar([saldo], n)
                _incrementar(linea.sku, saldo.ubicacion_id, saldo.lote_id, Saldo.UBICADO_VENDIBLE, n)
                _mov(
                    linea.sku, Movimiento.PUTAWAY, n, lote=lote,
                    origen=Saldo.EN_PUTAWAY, destino=Saldo.UBICADO_VENDIBLE,
                    referencia=saldo.ubicacion.codigo, actor=actor,
                )
                skus_vendibles.add(linea.sku)

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
                motivo="Descarga terminada: cierre de la recepción",
            )
        orden.transicionar(OrdenEntrada.CERRADA, actor=actor, motivo="Recepción cerrada: todo lo recibido queda vendible")

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
    for sku in sorted(skus_vendibles, key=lambda s: s.codigo):
        _notificar_cambio_disponible(sku)
        _reintentar_pendientes(sku)
    return orden


def reiniciar_recepcion(orden, actor=None):
    """La recepción vuelve a cero para recontar todo escaneando: cada caja
    viva queda anulada (las recibidas revierten su stock con kardex; una
    caja que ya no está donde se recibió se anula igual y se reporta), las
    líneas vuelven a 0 recibidas/dañadas y una orden RECIBIDA regresa a
    EN_RECEPCION (deshacer explícito, auditado). La foto de llegada se
    queda. Regresa {"anuladas", "recibidas_revertidas", "no_encontradas"}."""
    _orden_abierta(orden)
    r = {"anuladas": 0, "recibidas_revertidas": 0, "no_encontradas": 0}
    with transaction.atomic():
        orden = OrdenEntrada.objects.select_for_update().get(pk=orden.pk)
        ahora = timezone.now()
        for escaneo in escaneos_vivos(orden).select_related("linea__sku", "tarima").order_by("-ts", "-id"):
            if escaneo.estado == EscaneoRecepcion.RECIBIDA:
                try:
                    with transaction.atomic():
                        _revertir_recibida(escaneo, actor)
                    r["recibidas_revertidas"] += 1
                except ValueError:
                    r["no_encontradas"] += 1
                    linea = _linea_bloqueada(escaneo.linea_id)
                    campo = "cantidad_danada" if escaneo.danada else "cantidad_recibida"
                    setattr(linea, campo, max(getattr(linea, campo) - 1, 0))
                    linea.save(update_fields=[campo])
            escaneo.estado = EscaneoRecepcion.ANULADA
            escaneo.anulada_ts = ahora
            escaneo.anulada_por = _actor_str(actor)
            escaneo.save(update_fields=["estado", "anulada_ts", "anulada_por"])
            r["anuladas"] += 1
        estado_anterior = orden.estado
        if orden.estado == OrdenEntrada.RECIBIDA:
            orden.estado = OrdenEntrada.EN_RECEPCION
            orden.ts_descarga_fin = None
        orden.ts_conteo_fin = None
        orden.save(update_fields=["estado", "ts_descarga_fin", "ts_conteo_fin"])
        registrar_evento(
            "asn", orden.folio, "recepcion_reiniciada", actor=actor, cliente=orden.cliente,
            delta={**r, "estado": [estado_anterior, orden.estado]},
            motivo="Reinicio de la recepción: las cajas contadas se anulan y el stock recibido se revierte para volver a escanear todo.",
        )
    return r


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


def _armar_resumen(sku, por_estado, ultimo_conteo, buffer_stock):
    """El dict de resumen_sku a partir de los saldos por estado ya agregados."""
    vendible = por_estado.get(Saldo.UBICADO_VENDIBLE, 0)
    reservado = por_estado.get(Saldo.RESERVADO, 0)
    en_putaway = por_estado.get(Saldo.EN_PUTAWAY, 0)
    en_empaque = por_estado.get(Saldo.EN_EMPAQUE, 0)
    cuarentena = por_estado.get(Saldo.CUARENTENA, 0)
    return {
        "sku": sku,
        "fisico": vendible + en_putaway + en_empaque + cuarentena,
        "en_recepcion": en_putaway,
        "vendible": vendible,
        "apartado": reservado,
        "en_empaque": en_empaque,
        "cuarentena": cuarentena,
        "disponible": vendible - reservado - (buffer_stock or 0),
        "ultimo_conteo": ultimo_conteo,
    }


def resumen_sku(sku):
    """Resumen por SKU para el portal: físico / en recepción / apartado /
    cuarentena / disponible + fecha del último conteo. Para listas enteras
    usa resumen_skus (dos consultas en total, no dos por SKU).
    """
    filas = Saldo.objects.filter(sku=sku).values("estado").annotate(t=Sum("cantidad"))
    por_estado = {f["estado"]: f["t"] or 0 for f in filas}
    ultimo = sku.conteos.order_by("-ts").first()
    return _armar_resumen(sku, por_estado, ultimo.ts if ultimo else None, sku.cliente.buffer_stock)


def resumen_skus(skus):
    """resumen_sku para MUCHOS SKUs en DOS consultas (saldos por SKU y estado;
    último conteo por SKU) en vez de dos por SKU (2026-10-06: la lista de
    SKUs de Colima hacía ~1,100 queries y tardaba más que el timeout del
    service worker, que la pintaba como "Sin conexión"). Regresa
    {sku_id: resumen} con el mismo shape que resumen_sku; los SKUs deben
    traer `cliente` cargado (select_related) para el buffer."""
    skus = list(skus)
    ids = [s.pk for s in skus]
    por_sku = {}
    for f in Saldo.objects.filter(sku_id__in=ids).values("sku_id", "estado").annotate(t=Sum("cantidad")):
        por_sku.setdefault(f["sku_id"], {})[f["estado"]] = f["t"] or 0
    ultimos = dict(
        Conteo.objects.filter(sku_id__in=ids).values("sku_id").annotate(ultimo=Max("ts")).values_list("sku_id", "ultimo")
    )
    return {
        s.pk: _armar_resumen(s, por_sku.get(s.pk, {}), ultimos.get(s.pk), s.cliente.buffer_stock)
        for s in skus
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
    "codigo", "descripcion", "codigo_barras", "lote", "caducidad", "ubicacion", "vendible_actual", "contado",
)
_COLUMNAS_CSV_CONTEO_MINIMAS = ("codigo", "contado")


def exportar_conteo(cliente):
    """Filas del CSV de conteo (columnas COLUMNAS_CSV_CONTEO): una por anaquel
    donde vive cada SKU/lote vendible, con `contado` prellenado con el vendible
    actual para editar solo lo que difiere. Los SKUs activos sin stock salen con
    una fila vacía para poder capturarlos; kits excluidos (jamás tienen stock).

    La importación lee SOLO codigo, lote, caducidad, ubicacion y contado; las
    demás columnas viajan para que el archivo sea legible (descripcion), para
    contar con el escáner en mano (codigo_barras; Chema 2026-10-07) y para
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
                "codigo_barras": sku.codigo_barras,
                "lote": saldo.lote.codigo if saldo.lote else "",
                "caducidad": cad.isoformat() if cad else "",
                "ubicacion": saldo.ubicacion.codigo,
                "vendible_actual": saldo.cantidad,
                "contado": saldo.cantidad,
            })
        if not por_sku.get(sku.pk):
            filas.append({
                "codigo": sku.codigo, "descripcion": sku.descripcion, "codigo_barras": sku.codigo_barras,
                "lote": "", "caducidad": "", "ubicacion": "",
                "vendible_actual": 0, "contado": 0,
            })
    return filas


def leer_csv_conteo(texto):
    """Parsea el CSV de conteo. Regresa (filas, errores_de_archivo).

    Cada fila: dict con codigo, descripcion, codigo_barras, lote, caducidad, ubicacion,
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


def ajustar_conteo_posicion(ubicacion, sku, renglones, motivo, actor, nota=""):
    """Conteo físico de UN producto en UNA posición, por lote (Mesa → Bodega →
    Posiciones → Editar → Ajustar conteo; Chema 2026-10-14, reacomodo de
    racks). `renglones` = [(lote, contado, caducidad)] con `lote` un Lote del
    producto, None (sin lote) o un código nuevo (str) que se crea con esa
    caducidad, como en la reconciliación por CSV. Por renglón: un Conteo
    (esperado = vendible de ese lote en esta posición) y, si hay diferencia,
    un Ajuste con lote y posición firmado por el usuario de Mesa (sin doble
    PIN, como la reconciliación; sin incidencia DES: el descuadre ya se está
    asumiendo). Contado 0 quita el lote de la posición. No agrega productos
    nuevos a la posición, solo lotes del producto. Si al final lo apartado del
    producto rebasa lo vendible en toda la bodega, libera la reserva sobrante
    (_liberar_reserva_sobrante). Regresa {"ajustes": [{lote, esperado,
    contado, delta, folio}], "conteos", "sin_cambio", "liberadas": [(folio,
    piezas)], "sin_respaldo"}."""
    if motivo not in dict(Ajuste.MOTIVOS):
        validos = ", ".join(clave for clave, _ in Ajuste.MOTIVOS)
        raise ValueError(f"Motivo '{motivo}' fuera del catálogo. Válidos: {validos}.")
    if ubicacion.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA) or not ubicacion.activo:
        raise ValueError(f"{ubicacion.codigo} no es una posición de almacén activa.")
    if getattr(sku, "es_kit", False):
        raise ValueError(f"{sku.codigo} es un kit: no tiene stock propio, no se cuenta.")
    if not renglones:
        raise ValueError("Captura al menos un lote con su conteo.")
    firmante = _validar_admin_mesa(actor)
    resultado = {"ajustes": [], "conteos": 0, "sin_cambio": 0, "liberadas": [], "sin_respaldo": 0}
    with transaction.atomic():
        vistos = set()
        for lote, contado, caducidad in renglones:
            try:
                contado = int(str(contado).strip())
            except (TypeError, ValueError):
                raise ValueError("El conteo debe ser un número entero de piezas.") from None
            if contado < 0:
                raise ValueError("El conteo no puede ser negativo.")
            if isinstance(lote, str):
                codigo = lote.strip().upper()
                if not codigo:
                    lote = None
                else:
                    lote, creado = Lote.objects.get_or_create(sku=sku, codigo=codigo, defaults={"fecha_caducidad": caducidad})
                    if not creado and lote.fecha_caducidad is None and caducidad:
                        lote.fecha_caducidad = caducidad
                        lote.save(update_fields=["fecha_caducidad"])
            if lote is not None and lote.sku_id != sku.pk:
                raise ValueError(f"El lote {lote.codigo} no es de {sku.codigo}.")
            clave = lote.pk if lote is not None else None
            if clave in vistos:
                raise ValueError(f"El lote {lote.codigo if lote else 'sin lote'} viene dos veces.")
            vistos.add(clave)
            esperado = (
                Saldo.objects.filter(sku=sku, ubicacion=ubicacion, lote=lote, estado=Saldo.UBICADO_VENDIBLE)
                .aggregate(t=Sum("cantidad"))["t"] or 0
            )
            conteo = _conteo_reconciliacion({"sku": sku, "vendible_actual": esperado, "contado": contado}, actor)
            resultado["conteos"] += 1
            delta = contado - esperado
            if delta == 0:
                resultado["sin_cambio"] += 1
                continue
            ajuste = _aplicar_ajuste_firmado(sku, delta, motivo, firmante, firmante, lote=lote, conteo=conteo, ubicacion=ubicacion)
            resultado["ajustes"].append({
                "lote": lote.codigo if lote else "", "esperado": esperado, "contado": contado, "delta": delta, "folio": ajuste.folio,
            })
        resultado["liberadas"], resultado["sin_respaldo"] = _liberar_reserva_sobrante(sku, actor)
        registrar_evento(
            "ubicacion", ubicacion.codigo, "conteo_posicion", actor=actor, cliente=sku.cliente,
            delta={"sku": sku.codigo, "motivo": motivo, "ajustes": resultado["ajustes"], "conteos": resultado["conteos"],
                   "liberadas": resultado["liberadas"], "sin_respaldo": resultado["sin_respaldo"]},
            motivo=(nota or f"Conteo de {sku.codigo} en {ubicacion.codigo} desde Posiciones.")[:300],
        )
    return resultado


def _liberar_reserva_sobrante(sku, actor):
    """Tras un conteo que deja menos físico que apartado (Chema 2026-10-14):
    libera la reserva sobrante quitándosela a los pedidos PENDIENTES más
    recientes del producto; cada línea queda sin inventario (como un faltante
    de ingesta) y se vuelve a apartar sola cuando entre stock. Lo que siga sin
    respaldo lo tienen pedidos ya en picking y se verá al pickear. Regresa
    ([(folio, piezas)], sin_respaldo)."""
    sobra = _suma(sku, Saldo.RESERVADO) - _suma(sku, Saldo.UBICADO_VENDIBLE)
    if sobra <= 0:
        return [], 0
    from apps.pedidos.models import LineaPedido, Pedido  # lazy: modelos de otra app
    from apps.pedidos.services import _planificar_best_effort  # lazy por contrato

    liberadas = []
    lineas = (
        LineaPedido.objects.select_related("pedido", "pedido__cliente")
        .filter(sku=sku, pedido__estado=Pedido.PENDIENTE)
        .filter(Q(reservada=True) | Q(cantidad_repuesta_reservada__gt=0))
        .order_by("-pedido__creado", "-pk")
    )
    for linea in lineas:
        if sobra <= 0:
            break
        piezas = (linea.cantidad if linea.reservada else 0) + linea.cantidad_repuesta_reservada
        if piezas <= 0:
            continue
        liberar_reserva(sku, piezas, linea.pedido.folio)
        linea.reservada = False
        linea.cantidad_repuesta_reservada = 0
        linea.save(update_fields=["reservada", "cantidad_repuesta_reservada"])
        registrar_evento(
            "pedido", linea.pedido.pk, "reserva_liberada_por_conteo", actor=actor, cliente=linea.pedido.cliente,
            delta={"sku": sku.codigo, "piezas": piezas},
            motivo="El conteo dejó menos físico que apartado: la línea queda sin inventario y se vuelve a apartar cuando entre stock.",
        )
        _planificar_best_effort(linea.pedido, force=True)
        liberadas.append((linea.pedido.folio, piezas))
        sobra -= piezas
    return liberadas, max(sobra, 0)


ESTADO_LEGIBLE = {
    Saldo.UBICADO_VENDIBLE: "vendible", Saldo.RESERVADO: "apartado (de las vendibles)", Saldo.EN_PUTAWAY: "en recepción",
    Saldo.EN_EMPAQUE: "en empaque (ya salió del anaquel)", Saldo.CUARENTENA: "cuarentena",
}


def _filas_buscador(saldos):
    """Renglones del buscador a partir de saldos (Chema 2026-10-14): uno por
    SKU · lote · posición · estado de lo que está físicamente en el anaquel
    (vendible, en recepción, cuarentena); la capa de apartados va DENTRO del
    renglón vendible (`apartado`), no como renglón aparte, y lo que ya se
    pickeó (en empaque) no aparece en ninguna posición: se regresa aparte,
    sumado por producto. Regresa (filas, en_empaque)."""
    filas, en_empaque = {}, {}
    orden_estado = {Saldo.UBICADO_VENDIBLE: 0, Saldo.EN_PUTAWAY: 1, Saldo.CUARENTENA: 2}
    for x in saldos:
        if x.estado == Saldo.EN_EMPAQUE:
            en_empaque[x.sku] = en_empaque.get(x.sku, 0) + x.cantidad
            continue
        estado = Saldo.UBICADO_VENDIBLE if x.estado == Saldo.RESERVADO else x.estado
        if estado not in orden_estado:
            continue
        clave = (x.ubicacion_id, x.sku_id, x.lote_id, estado)
        fila = filas.setdefault(clave, {
            "sku": x.sku, "lote": x.lote.codigo if x.lote_id else "", "caducidad": x.lote.fecha_caducidad if x.lote_id else None,
            "ubicacion": x.ubicacion.codigo, "estado": ESTADO_LEGIBLE.get(estado, estado), "cantidad": 0, "apartado": 0,
            "en_anaquel": True, "_orden": (x.ubicacion.codigo, x.sku.codigo, x.lote.codigo if x.lote_id else "", orden_estado[estado]),
        })
        fila["apartado" if x.estado == Saldo.RESERVADO else "cantidad"] += x.cantidad
    lista = sorted(filas.values(), key=lambda f: f["_orden"])
    for f in lista:
        del f["_orden"]
    return lista, [{"sku": sku, "cantidad": n} for sku, n in sorted(en_empaque.items(), key=lambda kv: kv[0].codigo)]


def buscar_en_bodega(q, cliente=None):
    """Buscador de Bodega (Chema 2026-10-06): "¿dónde está X?". `q` puede ser
    una POSICIÓN (código exacto de ubicación: qué hay ahí, con lotes y
    ocupación) o un PRODUCTO / LOTE (código, descripción o código de barras
    del SKU, o código de lote): una fila por SKU · lote · posición · estado con
    sus piezas, ordenada por posición, y los racks donde vive para resaltarlos
    en el plano. Las apartadas van dentro del renglón vendible y lo pickeado
    (en empaque) no se lista por posición: va en `en_empaque` por producto
    (Chema 2026-10-14). Regresa None sin consulta; {"modo":
    "posicion"|"producto", "q", "filas", "en_empaque", "racks", "ubicacion",
    "ocupacion", "skus", "piezas"}."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    q = (q or "").strip()
    if not q:
        return None
    ubicacion = Ubicacion.objects.filter(codigo__iexact=q).first()
    if ubicacion is not None:
        consulta = Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0).select_related("sku", "sku__cliente", "lote", "ubicacion")
        if cliente is not None:
            consulta = consulta.filter(sku__cliente=cliente)
        filas, en_empaque = _filas_buscador(consulta)
        return {"modo": "posicion", "q": q, "filas": filas, "en_empaque": en_empaque, "racks": {ubicacion.codigo},
                "ubicacion": ubicacion, "ocupacion": ocupacion(ubicacion), "skus": [],
                "piezas": sum(f["cantidad"] for f in filas if f["en_anaquel"])}
    skus = SKU.objects.filter(Q(codigo__icontains=q) | Q(descripcion__icontains=q) | Q(codigo_barras__iexact=q))
    if cliente is not None:
        skus = skus.filter(cliente=cliente)
    skus = list(skus.select_related("cliente").order_by("codigo")[:50])
    consulta = Saldo.objects.filter(cantidad__gt=0).filter(Q(sku__in=skus) | Q(lote__codigo__iexact=q))
    if cliente is not None:
        consulta = consulta.filter(sku__cliente=cliente)
    filas, en_empaque = _filas_buscador(consulta.select_related("sku", "sku__cliente", "lote", "ubicacion"))
    racks = {f["ubicacion"] for f in filas if f["en_anaquel"]}
    return {"modo": "producto", "q": q, "filas": filas, "en_empaque": en_empaque, "racks": racks, "ubicacion": None,
            "ocupacion": None, "skus": skus, "piezas": sum(f["cantidad"] for f in filas if f["en_anaquel"])}


# ── Capacidad y ocupación de anaqueles (producto siempre parado) ──

def capacidad_sku_en(ubicacion, sku, partes=1):
    """Piezas del SKU que caben PARADAS en la ubicación: huella (largo × ancho
    del anaquel entre las del SKU, o girado 90° sobre el piso, el mayor) por
    niveles (alto del anaquel entre alto del SKU, tope TORRE["APILADO_MAX"]).
    `partes` = entre cuántos productos se reparte el largo (posición con dos
    productos fijos: 2). None = no se puede calcular (anaquel sin medidas, sin
    tope de alto como la reserva, o SKU sin medidas). 0 = no cabe ni una parada."""
    if not (ubicacion.largo_cm and ubicacion.ancho_cm and ubicacion.alto_cm):
        return None
    if not (sku.largo_cm and sku.ancho_cm and sku.alto_cm):
        return None
    if sku.alto_cm > ubicacion.alto_cm:
        return 0
    largo = ubicacion.largo_cm // max(1, int(partes))
    niveles = min(ubicacion.alto_cm // sku.alto_cm, int(settings.TORRE.get("APILADO_MAX", 6)))
    huella = max(
        (largo // sku.largo_cm) * (ubicacion.ancho_cm // sku.ancho_cm),
        (largo // sku.ancho_cm) * (ubicacion.ancho_cm // sku.largo_cm),
    )
    return huella * niveles


def capacidad_asignada(ubicacion, sku):
    """Capacidad del producto en su posición fija: la cuenta por medidas con el
    largo entre el número de productos fijos de la posición (1 o 2)."""
    from apps.catalogo.models import AsignacionPosicion  # lazy: modelo de otra app

    partes = max(1, AsignacionPosicion.objects.filter(ubicacion=ubicacion).count())
    return capacidad_sku_en(ubicacion, sku, partes)


# Lo que FÍSICAMENTE está en un anaquel (Chema 2026-10-07): el vendible, lo que
# espera acomodo y la cuarentena. La capa "apartado" va encima del vendible
# (contarla era contar doble) y lo "en empaque" ya está en la mesa aunque
# conserve la posición de origen como trazabilidad.
ESTADOS_EN_ANAQUEL = (Saldo.UBICADO_VENDIBLE, Saldo.EN_PUTAWAY, Saldo.CUARENTENA)


def ocupacion(ubicacion, saldos=None, extra=None):
    """Ocupación ESTIMADA del anaquel: cada SKU consume piezas / su capacidad
    sola; la suma es la fracción ocupada. `extra` = (sku, piezas) que se
    quieren agregar (para avisar antes de ubicar). Regresa {"pct", "estado"
    (libre|medio|lleno|ilimitado|sin_medidas), "sin_medidas": [códigos de SKU],
    "no_caben": [códigos], "por_sku": [{sku, piezas, capacidad}]}."""
    if saldos is None:
        saldos = Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0, estado__in=ESTADOS_EN_ANAQUEL).select_related("sku")
    piezas_por_sku, lotes_por_sku = {}, {}
    for s in saldos:
        if s.estado not in ESTADOS_EN_ANAQUEL:
            continue  # apartado = capa sobre el vendible; en empaque = ya no está en el anaquel
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


def ocupaciones(ubicaciones, reservas=None, ignorar_cliente=None):
    """{código: ocupacion(...)} de varias ubicaciones con una sola consulta de
    saldos. `reservas` = {código: [(sku, piezas)]} apartadas de forma virtual
    (un plan en curso) que se suman a lo físico. `ignorar_cliente`: sus saldos
    no cuentan como ocupación (reacomodo total: se planea como si la bodega
    estuviera vacía de ese cliente; lo de otros clientes sí ocupa)."""
    por_ubicacion = {u.pk: [] for u in ubicaciones}
    saldos = Saldo.objects.filter(ubicacion__in=ubicaciones, cantidad__gt=0, estado__in=ESTADOS_EN_ANAQUEL).select_related("sku")
    if ignorar_cliente is not None:
        saldos = saldos.exclude(sku__cliente=ignorar_cliente)
    for s in saldos:
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


def sugerir_anaquel(sku, cantidad, reservas=None, lote=None, ignorar_cliente=None):
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
    es candidato (Chema 2026-09-17). 3) Si no alcanzó, un anaquel ocupado por
    OTROS productos con espacio también recibe (cada producto en su pila),
    hasta TORRE["SKUS_POR_CELDA_MAX"] productos distintos por celda: con
    muchas combinaciones producto-lote chicas, una celda por combinación
    dejaba la bodega "llena" de aire (Chema 2026-09-17)."""
    from apps.catalogo.services import clase_rotacion  # lazy por contrato

    if cantidad <= 0 or not (sku.largo_cm and sku.ancho_cm and sku.alto_cm):
        return []
    anaqueles = list(
        Ubicacion.objects.filter(tipo=Ubicacion.PICKING, activo=True, prioridad__isnull=False, lleno_manual=False)
        .exclude(largo_cm=0).exclude(ancho_cm=0).exclude(alto_cm=0).order_by("prioridad", "codigo")
    )
    if not anaqueles:  # todo lleno o sin medidas: hay piezas, no hay lugar
        return [{"ubicacion": None, "cantidad": cantidad, "motivo": "sin anaquel con espacio"}]
    ocup = ocupaciones(anaqueles, reservas, ignorar_cliente=ignorar_cliente)
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
        # 3) Compartir celda con otros productos (nunca con otro lote del mismo SKU).
        tope_skus = int(settings.TORRE.get("SKUS_POR_CELDA_MAX", 3))
        compartidos = []
        for u in anaqueles:
            filas = ocup[u.codigo]["por_sku"]
            if not filas or u in con_sku:
                continue
            if any(f["sku"].pk == sku.pk for f in filas):  # mismo SKU con otro lote: no
                continue
            if len(filas) >= tope_skus:
                continue
            libre = espacio_para(u, sku, ocup[u.codigo])
            if libre > 0:
                compartidos.append((u, libre))
        clase = clase_rotacion(sku)
        if clase == "C":
            compartidos.reverse()
        elif clase == "B":
            mitad = len(compartidos) // 2
            compartidos = compartidos[mitad:] + compartidos[:mitad]
        for u, libre in compartidos:
            if restante <= 0:
                break
            agrega(u, min(libre, restante), f"comparte anaquel con otro producto · caben {libre}")
    if restante > 0:
        plan.append({"ubicacion": None, "cantidad": restante, "motivo": "sin anaquel con espacio"})
    return plan


def replanear_bodega(cliente, actor=None, aplicar=False):
    """Plan de reacomodo TOTAL de lo vendible de un cliente como si la bodega
    estuviera vacía de sus productos (Chema 2026-09-21: el acomodo de ASN-0004
    se hizo sin separar lotes y hay que rehacerlo con la hoja correcta). Junta
    lo vendible del cliente en anaqueles de picking y reserva por SKU y lote,
    lo planea de clase A a C con sugerir_anaquel ignorando la ocupación del
    propio cliente (lo de otros clientes y los anaqueles marcados llenos a
    mano sí cuentan) y regresa los pasos [{sku, lote, ubicacion|None,
    cantidad, motivo, desde: [[anaquel, piezas]]}]. Con aplicar=True mueve el
    inventario en el sistema a los anaqueles del plan para que quede igual a
    lo físico cuando el piso termine: lo vendible y sus capas de reserva
    cambian de anaquel conservando lote y estado, sin líneas de kardex (la
    cantidad no cambia; mismo criterio que mover_ubicacion), con evento
    reacomodo_total. Los pasos sin anaquel se quedan donde están. Regresa
    {"pasos", "sin_espacio", "movidas"}."""
    from apps.catalogo.services import clases_rotacion  # lazy por contrato

    saldos = list(
        Saldo.objects.filter(
            sku__cliente=cliente, cantidad__gt=0,
            estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO],
            ubicacion__tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA],
        ).select_related("sku", "lote", "ubicacion")
    )
    grupos = {}
    for s in saldos:
        if s.estado != Saldo.UBICADO_VENDIBLE:
            continue
        clave = (s.sku_id, s.lote.codigo if s.lote_id else "")
        g = grupos.setdefault(clave, {"sku": s.sku, "lote": clave[1], "total": 0, "desde": {}})
        g["total"] += s.cantidad
        g["desde"][s.ubicacion.codigo] = g["desde"].get(s.ubicacion.codigo, 0) + s.cantidad
    clases = clases_rotacion(cliente)
    orden = sorted(grupos.values(), key=lambda g: (clases.get(g["sku"].pk, "C"), g["sku"].codigo, g["lote"]))
    reservas, pasos = {}, []
    for g in orden:
        sku, lote = g["sku"], g["lote"]
        plan = sugerir_anaquel(sku, g["total"], reservas, lote=lote or None, ignorar_cliente=cliente)
        if not plan:
            plan = [{"ubicacion": None, "cantidad": g["total"], "motivo": "sin medidas del producto"}]
        for paso in plan:
            u = paso["ubicacion"]
            if u is not None:
                reservas.setdefault(u.codigo, []).append((sku, paso["cantidad"], lote or None))
            pasos.append({
                "sku_id": sku.pk, "sku": sku.codigo, "lote": lote,
                "ubicacion": u.codigo if u else None, "cantidad": paso["cantidad"],
                "motivo": paso["motivo"], "desde": sorted(g["desde"].items()),
            })
    sin_espacio = sum(p["cantidad"] for p in pasos if p["ubicacion"] is None)
    movidas = 0
    if aplicar:
        codigos = {p["ubicacion"] for p in pasos if p["ubicacion"]}
        destinos = {u.codigo: u for u in Ubicacion.objects.filter(codigo__in=codigos)}
        with transaction.atomic():
            for g in orden:
                sku, lote = g["sku"], g["lote"]
                colocados = [p for p in pasos if p["sku_id"] == sku.pk and p["lote"] == lote and p["ubicacion"]]
                a_mover = sum(p["cantidad"] for p in colocados)
                if a_mover <= 0:
                    continue
                filtro = {"sku": sku, "ubicacion__tipo__in": [Ubicacion.PICKING, Ubicacion.RESERVA]}
                filtro["lote__codigo"] = lote if lote else None
                if not lote:
                    filtro.pop("lote__codigo")
                    filtro["lote__isnull"] = True
                vendibles = list(Saldo.objects.select_for_update(of=("self",)).filter(estado=Saldo.UBICADO_VENDIBLE, **filtro))
                lote_id = vendibles[0].lote_id if vendibles else None
                _restar(sorted(vendibles, key=lambda s: s.pk), a_mover)
                for p in colocados:
                    _incrementar(sku, destinos[p["ubicacion"]].pk, lote_id, Saldo.UBICADO_VENDIBLE, p["cantidad"])
                # Las capas de reserva viajan con el producto al primer anaquel del plan.
                capas = list(Saldo.objects.select_for_update(of=("self",)).filter(estado=Saldo.RESERVADO, **filtro))
                primero = destinos[colocados[0]["ubicacion"]]
                for capa in capas:
                    if capa.ubicacion_id != primero.pk:
                        _incrementar(sku, primero.pk, capa.lote_id, Saldo.RESERVADO, capa.cantidad)
                        capa.delete()
                movidas += a_mover
            registrar_evento(
                "cliente", cliente.slug, "reacomodo_total", actor=actor, cliente=cliente,
                delta={"pasos": len(pasos), "movidas": movidas, "sin_espacio": sin_espacio},
                motivo="Reacomodo total: el inventario del sistema se movió a los anaqueles del plan nuevo.",
            )
    return {"pasos": pasos, "sin_espacio": sin_espacio, "movidas": movidas}


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


# ── Tarimas (Chema 2026-09-24; nombre editable 2026-10-13) ────────────────────
# Una tarima es una ubicación de reserva TAR-nn: en recepción cada caja se
# cuenta en una tarima y ahí nace su saldo al confirmarla. Sin medidas ni
# prioridad. El piso la abre desde "¿Dónde estás escaneando?" con el nombre
# propuesto (el siguiente número) o uno propio con el mismo prefijo.
PREFIJO_TARIMA = "TAR-"


def _numero_tarima(codigo):
    sufijo = str(codigo or "")[len(PREFIJO_TARIMA):]
    return int(sufijo) if sufijo.isdigit() else 0


def tarimas_activas():
    """Tarimas activas en orden numérico (TAR-01, TAR-02, …)."""
    tarimas = Ubicacion.objects.filter(tipo=Ubicacion.RESERVA, activo=True, codigo__startswith=PREFIJO_TARIMA)
    return sorted(tarimas, key=lambda u: (_numero_tarima(u.codigo), u.codigo))


def nombre_tarima_propuesto():
    """El siguiente TAR-nn libre (no la crea)."""
    existentes = Ubicacion.objects.filter(codigo__startswith=PREFIJO_TARIMA).values_list("codigo", flat=True)
    return f"{PREFIJO_TARIMA}{max((_numero_tarima(c) for c in existentes), default=0) + 1:02d}"


def contenido_ubicacion(ubicacion):
    """Qué vive en una ubicación sin medidas (tarima, zona de desborde):
    [{"sku", "piezas", "lotes"}] por SKU, de mayor a menor. `ocupacion` no lo
    da para ubicaciones sin medidas (no hay capacidad que estimar)."""
    piezas, lotes = {}, {}
    for s in Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0, estado__in=ESTADOS_EN_ANAQUEL).select_related("sku", "lote"):
        piezas[s.sku] = piezas.get(s.sku, 0) + s.cantidad
        if s.lote_id:
            lotes.setdefault(s.sku, set()).add(s.lote.codigo)
    return sorted(
        ({"sku": sku, "piezas": n, "capacidad": None, "lotes": sorted(lotes.get(sku, ()))} for sku, n in piezas.items()),
        key=lambda f: (-f["piezas"], f["sku"].codigo),
    )


def crear_tarima(actor=None, codigo=None):
    """Tarima nueva (reserva, activa): la siguiente TAR-nn o el nombre que
    diga el piso, que debe empezar con TAR- y no repetirse. Auditada."""
    with transaction.atomic():
        list(Ubicacion.objects.select_for_update().filter(codigo__startswith=PREFIJO_TARIMA))  # candado: dos altas a la vez no chocan
        codigo = (codigo or "").strip().upper() or nombre_tarima_propuesto()
        if not codigo.startswith(PREFIJO_TARIMA) or len(codigo) <= len(PREFIJO_TARIMA):
            raise ValueError(f"El nombre de una tarima empieza con {PREFIJO_TARIMA} (ej. {nombre_tarima_propuesto()}).")
        if len(codigo) > 20:
            raise ValueError("El nombre de la tarima es muy largo (máximo 20 caracteres).")
        if Ubicacion.objects.filter(codigo__iexact=codigo).exists():
            raise ValueError(f"Ya existe {codigo}: elige otro nombre.")
        tarima = Ubicacion.objects.create(codigo=codigo, tipo=Ubicacion.RESERVA, activo=True)
    registrar_evento(
        "ubicacion", tarima.codigo, "tarima_creada", actor=actor,
        motivo="Tarima creada desde recepción; lo que se cuente ahí es vendible al cerrar la orden.",
    )
    return tarima


# ── Editor de recepción (Mesa, Chema 2026-10-06; por escaneo desde 2026-10-13) ──
# La tabla "Realidad": por línea (producto · lote) qué hay de verdad y en qué
# tarima, y correcciones con kardex donde cambia el stock y evento
# `recepcion_corregida` siempre (motivo obligatorio). Es el único lugar donde
# se corrige una recepción: el piso solo escanea.


def realidad_recepcion(orden):
    """Tabla "Realidad" de la orden: por línea (producto · lote) lo anunciado,
    contado, dañado y recibido, y por tarima cuántas cajas hay (contadas sin
    confirmar y recibidas). `posiciones` y `en_recepcion` alimentan la tabla
    "Dónde quedó" del portal (solo lectura)."""
    por_linea = {}
    for e in escaneos_vivos(orden).select_related("tarima").order_by("ts", "id"):
        tarimas = por_linea.setdefault(e.linea_id, {})
        r = tarimas.setdefault(e.tarima_id, {"tarima": e.tarima, "contadas": 0, "danadas": 0, "por_confirmar": 0, "recibidas": 0, "sin_etiqueta": 0})
        r["danadas" if e.danada else "contadas"] += 1
        if e.estado == EscaneoRecepcion.CONTADA:
            r["por_confirmar"] += 1
        elif not e.danada:
            r["recibidas"] += 1
        if e.impresa_ts is None:
            r["sin_etiqueta"] += 1
    filas = []
    for f in resumen_lineas(orden):
        tarimas = list(por_linea.get(f["linea"].pk, {}).values())
        f["tarimas"] = tarimas
        f["posiciones"] = [{"ubicacion": t["tarima"], "cantidad": t["recibidas"]} for t in tarimas if t["recibidas"]]
        f["en_recepcion"] = f["por_confirmar"]
        filas.append(f)
    return filas


def _orden_editable(orden):
    if orden.estado == OrdenEntrada.CERRADA:
        raise ValueError(f"{orden.folio} ya está cerrada: se consulta, no se edita.")


def _validar_motivo(motivo):
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Di por qué se corrige (el motivo queda en el expediente).")
    return motivo[:300]


def _evento_correccion(orden, tipo, actor, motivo, **delta):
    registrar_evento(
        "asn", orden.folio, "recepcion_corregida", actor=actor, cliente=orden.cliente,
        delta={"tipo": tipo, **delta}, motivo=motivo,
    )


def _validar_tarima_editor(tarima):
    if tarima is None or not tarima.activo or tarima.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA):
        raise ValueError("Elige una tarima o posición de almacén activa.")


def _cajas_vivas(orden, linea, tarima, danada):
    return escaneos_vivos(orden).filter(linea=linea, tarima=tarima, danada=danada)


def ajustar_conteo(orden, linea, tarima, contadas, danadas, actor, motivo):
    """Mesa deja la línea con `contadas` cajas buenas y `danadas` dañadas en
    esa tarima. Subir crea escaneos ya RECIBIDOS (stock a la tarima, dañadas a
    cuarentena); bajar anula las últimas (con su stock). Regresa (Δbuenas, Δdañadas)."""
    _orden_editable(orden)
    motivo = _validar_motivo(motivo)
    _validar_tarima_editor(tarima)
    contadas, danadas = int(contadas), int(danadas)
    if contadas < 0 or danadas < 0:
        raise ValueError("Las cantidades no pueden ser negativas.")
    with transaction.atomic():
        d_c = contadas - _cajas_vivas(orden, linea, tarima, False).count()
        d_d = danadas - _cajas_vivas(orden, linea, tarima, True).count()
        if not d_c and not d_d:
            return 0, 0
        nuevas = []
        for danada, n in ((False, d_c), (True, d_d)):
            for _ in range(max(n, 0)):
                nuevas.append(EscaneoRecepcion.objects.create(
                    orden=orden, linea=linea, tarima=tarima, danada=danada, actor=_actor_str(actor),
                    impresa_ts=None if lleva_etiqueta(linea) else timezone.now(),
                ))
        if nuevas:
            if orden.estado == OrdenEntrada.ANUNCIADA:
                orden.transicionar(OrdenEntrada.EN_RECEPCION, actor=actor, motivo="Conteo capturado desde el editor")
            _lote_de_linea(linea)
            _recibir_escaneos(orden, tarima, nuevas, actor)
        for danada, n in ((False, d_c), (True, d_d)):
            for _ in range(max(-n, 0)):
                escaneo = _cajas_vivas(orden, linea, tarima, danada).order_by("-ts", "-id").select_for_update().first()
                if escaneo.estado == EscaneoRecepcion.RECIBIDA:
                    _revertir_recibida(escaneo, actor)
                escaneo.estado = EscaneoRecepcion.ANULADA
                escaneo.anulada_ts = timezone.now()
                escaneo.anulada_por = _actor_str(actor)
                escaneo.save(update_fields=["estado", "anulada_ts", "anulada_por"])
        _evento_correccion(
            orden, "conteo", actor, motivo, sku=linea.sku.codigo, lote=(linea.lote_codigo or "").strip() or None,
            tarima=tarima.codigo, contadas=[contadas - d_c, contadas], danadas=[danadas - d_d, danadas],
        )
    return d_c, d_d


def mover_cajas(orden, linea, origen, destino, cantidad, actor, motivo):
    """Mueve `cantidad` cajas recibidas (buenas) de la línea de una tarima o
    posición a otra: el saldo en_putaway cambia de posición (mismo estado,
    sin kardex, como mover_ubicacion) y los últimos escaneos de esa tarima
    se reapuntan para que la realidad siga diciendo dónde está cada caja."""
    _orden_editable(orden)
    motivo = _validar_motivo(motivo)
    cantidad = _validar_cantidad(cantidad, "mover")
    _validar_tarima_editor(destino)
    if origen.pk == destino.pk:
        raise ValueError("Elige una posición distinta.")
    sku = linea.sku
    with transaction.atomic():
        recibidas = list(
            _cajas_vivas(orden, linea, origen, False).filter(estado=EscaneoRecepcion.RECIBIDA)
            .order_by("-ts", "-id").select_for_update()[:cantidad]
        )
        if len(recibidas) < cantidad:
            raise ValueError(f"En {origen.codigo} solo hay {len(recibidas)} caja(s) recibidas de {sku.codigo}{' lote ' + linea.lote_codigo if linea.lote_codigo else ''}.")
        lote = _lote_de_linea(linea, crear=False)
        saldos = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY, ubicacion=origen, lote=lote, cantidad__gt=0))
        if sum(s.cantidad for s in saldos) < cantidad:
            raise ValueError(f"En {origen.codigo} solo hay {sum(s.cantidad for s in saldos)} pieza(s) de {sku.codigo} sin vender. Nada se cambió.")
        _restar(saldos, cantidad)
        _incrementar(sku, destino.pk, lote.pk if lote else None, Saldo.EN_PUTAWAY, cantidad)
        EscaneoRecepcion.objects.filter(pk__in=[e.pk for e in recibidas]).update(tarima=destino)
        _evento_correccion(orden, "mover", actor, motivo, sku=sku.codigo, lote=(linea.lote_codigo or "").strip() or None,
                           de=origen.codigo, a=destino.codigo, cantidad=cantidad)
    return cantidad


def marcar_danadas(orden, linea, tarima, cantidad, actor, motivo):
    """`cantidad` cajas recibidas de la línea en esa tarima resultan dañadas:
    salen de la tarima a la cuarentena de recepción (kardex) y la línea las
    pasa de recibidas a dañadas; sus escaneos quedan marcados."""
    _orden_editable(orden)
    motivo = _validar_motivo(motivo)
    cantidad = _validar_cantidad(cantidad, "dañar")
    zona = _ubicacion_tipo(Ubicacion.RECEPCION)
    if zona is None:
        raise ValueError("No hay ubicación de recepción activa para la cuarentena.")
    sku = linea.sku
    with transaction.atomic():
        linea = _linea_bloqueada(linea)
        recibidas = list(
            _cajas_vivas(orden, linea, tarima, False).filter(estado=EscaneoRecepcion.RECIBIDA)
            .order_by("-ts", "-id").select_for_update()[:cantidad]
        )
        if len(recibidas) < cantidad:
            raise ValueError(f"En {tarima.codigo} solo hay {len(recibidas)} caja(s) recibidas de {sku.codigo}{' lote ' + linea.lote_codigo if linea.lote_codigo else ''}.")
        lote = _lote_de_linea(linea, crear=False)
        lote_id = lote.pk if lote else None
        saldos = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_PUTAWAY, ubicacion=tarima, lote=lote, cantidad__gt=0))
        if sum(s.cantidad for s in saldos) < cantidad:
            raise ValueError(f"En {tarima.codigo} solo hay {sum(s.cantidad for s in saldos)} pieza(s) de {sku.codigo}. Nada se cambió.")
        _restar(saldos, cantidad)
        _incrementar(sku, zona.pk, lote_id, Saldo.CUARENTENA, cantidad)
        _mov(sku, Movimiento.RECEPCION, 0, lote=lote, origen=Saldo.EN_PUTAWAY, destino=Saldo.CUARENTENA, referencia=orden.folio, actor=actor)
        EscaneoRecepcion.objects.filter(pk__in=[e.pk for e in recibidas]).update(danada=True)
        linea.cantidad_recibida -= cantidad
        linea.cantidad_danada += cantidad
        linea.save(update_fields=["cantidad_recibida", "cantidad_danada"])
        _evento_correccion(orden, "danar", actor, motivo, sku=sku.codigo, lote=(linea.lote_codigo or "").strip() or None,
                           de=tarima.codigo, cantidad=cantidad)
    return cantidad


def editar_linea_recepcion(orden, linea, codigo_lote, caducidad, anunciadas, actor, motivo):
    """Corrige lote, caducidad y/o anunciadas de una línea (sin mover stock;
    el lote del catálogo toma la caducidad nueva y, si ya había stock con el
    lote viejo, se renombra para que siga siendo el mismo stock)."""
    _orden_editable(orden)
    motivo = _validar_motivo(motivo)
    codigo_lote = (codigo_lote or "").strip()
    anunciadas = int(anunciadas)
    if anunciadas < 0:
        raise ValueError("Lo anunciado no puede ser negativo.")
    if not codigo_lote and linea.sku.requiere_lote:
        raise ValueError(f"{linea.sku.codigo} requiere lote.")
    if codigo_lote != (linea.lote_codigo or "").strip() and orden.lineas.filter(sku=linea.sku, lote_codigo=codigo_lote).exclude(pk=linea.pk).exists():
        raise ValueError(f"{linea.sku.codigo} ya tiene una línea con el lote {codigo_lote or '(sin lote)'} en esta orden.")
    antes = {"lote": linea.lote_codigo, "caducidad": str(linea.fecha_caducidad or ""), "anunciadas": linea.cantidad_anunciada}
    with transaction.atomic():
        linea = _linea_bloqueada(linea)
        if (linea.cantidad_recibida or linea.cantidad_danada) and codigo_lote != (linea.lote_codigo or "").strip():
            viejo = Lote.objects.filter(sku=linea.sku, codigo=linea.lote_codigo).first() if linea.lote_codigo else None
            if viejo is not None and codigo_lote and not Lote.objects.filter(sku=linea.sku, codigo=codigo_lote).exists():
                viejo.codigo = codigo_lote
                viejo.save(update_fields=["codigo"])
        linea.lote_codigo, linea.fecha_caducidad, linea.cantidad_anunciada = codigo_lote, caducidad, anunciadas
        linea.save(update_fields=["lote_codigo", "fecha_caducidad", "cantidad_anunciada"])
        if codigo_lote:
            lote = Lote.objects.filter(sku=linea.sku, codigo=codigo_lote).first()
            if lote is not None and caducidad and lote.fecha_caducidad != caducidad:
                lote.fecha_caducidad = caducidad
                lote.save(update_fields=["fecha_caducidad"])
        _evento_correccion(orden, "linea", actor, motivo, sku=linea.sku.codigo, antes=antes,
                           despues={"lote": codigo_lote, "caducidad": str(caducidad or ""), "anunciadas": anunciadas})
    return linea


def agregar_linea_recepcion(orden, sku, codigo_lote, caducidad, anunciadas, actor, motivo, contadas=0, danadas=0, tarima=None):
    """Agrega a la orden un producto que llegó sin venir anunciado (o un lote
    nuevo de uno anunciado): línea con `anunciadas` (default 0) y, en el
    mismo paso, cajas ya recibidas en una tarima (ajustar_conteo)."""
    _orden_editable(orden)
    motivo = _validar_motivo(motivo)
    codigo_lote = (codigo_lote or "").strip()
    if sku.cliente_id != orden.cliente_id:
        raise ValueError(f"{sku.codigo} no es de {orden.cliente.nombre}.")
    if not codigo_lote and sku.requiere_lote:
        raise ValueError(f"{sku.codigo} requiere lote.")
    if orden.lineas.filter(sku=sku, lote_codigo=codigo_lote).exists():
        raise ValueError(f"{sku.codigo} ya tiene una línea con ese lote en {orden.folio}: edítala.")
    anunciadas, contadas, danadas = int(anunciadas or 0), int(contadas or 0), int(danadas or 0)
    if min(anunciadas, contadas, danadas) < 0:
        raise ValueError("Las cantidades no pueden ser negativas.")
    if (contadas or danadas) and tarima is None:
        raise ValueError("Di en qué tarima están las cajas.")
    with transaction.atomic():
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=anunciadas, lote_codigo=codigo_lote, fecha_caducidad=caducidad)
        _evento_correccion(orden, "agregar", actor, motivo, sku=sku.codigo, lote=codigo_lote or None,
                           anunciadas=anunciadas, contadas=contadas, danadas=danadas, tarima=tarima.codigo if tarima else None)
        if contadas or danadas:
            ajustar_conteo(orden, linea, tarima, contadas, danadas, actor, motivo)
            linea.refresh_from_db()
    return linea


def correcciones_recepcion(orden):
    """Historial de correcciones del editor (eventos recepcion_corregida), más reciente primero."""
    from apps.core.models import EventoAuditoria  # lazy por contrato

    return list(EventoAuditoria.objects.filter(entidad="asn", entidad_id=orden.folio, accion="recepcion_corregida").order_by("-ts"))


def realidad_csv_filas(orden):
    """Filas (sku, nombre, codigo_barras, lote, caducidad, posicion, estado, piezas) de la realidad de la orden."""
    filas = []
    for f in realidad_recepcion(orden):
        base = [f["sku"].codigo, f["sku"].descripcion, f["sku"].codigo_barras or "", f["lote"], f["caducidad"].isoformat() if f["caducidad"] else ""]
        for t in f["tarimas"]:
            if t["recibidas"]:
                filas.append([*base, t["tarima"].codigo, "recibida", t["recibidas"]])
            if t["por_confirmar"]:
                filas.append([*base, t["tarima"].codigo, "contada sin confirmar", t["por_confirmar"]])
            if t["danadas"]:
                filas.append([*base, t["tarima"].codigo, "dañada (cuarentena)", t["danadas"]])
    return filas


# ─────────────────────────────────────────────────────────────────────────────
# Posiciones desde Bodega (Chema 2026-10-13): renombrar y mover existencias
# ─────────────────────────────────────────────────────────────────────────────

NOMBRE_POSICION_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,19}$")


def validar_nombre_posicion(nuevo):
    """Nombre de posición: mayúsculas, dígitos y guiones, máximo 20 (A-3,
    B-12, TAR-07). Regresa el nombre normalizado o levanta ValueError."""
    nuevo = (nuevo or "").strip().upper()
    if not NOMBRE_POSICION_RE.match(nuevo):
        raise ValueError("El nombre lleva solo letras, números y guiones (máximo 20): por ejemplo A-3 o TAR-07.")
    return nuevo


def renombrar_posicion(ubicacion, nuevo, actor=None):
    """Cambia el nombre de una posición conservando su ficha, su inventario y
    sus apartados (mover_ubicacion en modo renombrar). El nombre nuevo no
    puede existir, ni siquiera inactivo. Regresa el nombre anterior."""
    nuevo = validar_nombre_posicion(nuevo)
    anterior = ubicacion.codigo
    if nuevo == anterior:
        raise ValueError(f"{anterior} ya se llama así.")
    if Ubicacion.objects.filter(codigo=nuevo).exists():
        raise ValueError(f"Ya existe una posición {nuevo}; elige otro nombre.")
    mover_ubicacion(anterior, nuevo, actor=actor)
    ubicacion.refresh_from_db()
    return anterior


def existencias_de_posicion(ubicacion):
    """Lo que hay en una posición por producto y lote: [{sku, lote, vendible,
    apartado}]. Vendible es lo físico; apartado, la capa de reservas encima."""
    filas = {}
    saldos = (
        Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0, estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO])
        .select_related("sku", "sku__cliente", "lote").order_by("sku__codigo", "lote__codigo")
    )
    for s in saldos:
        fila = filas.setdefault((s.sku_id, s.lote_id), {"sku": s.sku, "lote": s.lote, "vendible": 0, "apartado": 0})
        fila["vendible" if s.estado == Saldo.UBICADO_VENDIBLE else "apartado"] += s.cantidad
    return list(filas.values())


def mover_existencias(sku, lote, origen, destino, cantidad, actor=None, motivo=""):
    """Mueve `cantidad` piezas vendibles de un producto y lote de una posición
    a otra (Mesa → Bodega → Posiciones). El estado no cambia; la capa de
    apartados que ya no cabe sobre lo que queda en el origen viaja con las
    piezas. Kardex `traslado` con referencia origen>destino y evento
    `existencias_movidas`. Regresa {cantidad, apartadas}."""
    cantidad = _validar_cantidad(cantidad, "mover")
    if origen.pk == destino.pk:
        raise ValueError("Elige una posición destino distinta.")
    if not destino.activo or destino.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA):
        raise ValueError(f"{destino.codigo} no es una posición de almacén activa.")
    lote_id = lote.pk if lote is not None else None
    with transaction.atomic():
        saldos = list(Saldo.objects.select_for_update().filter(
            sku=sku, ubicacion=origen, lote_id=lote_id, estado__in=[Saldo.UBICADO_VENDIBLE, Saldo.RESERVADO],
        ))
        vendibles = [s for s in saldos if s.estado == Saldo.UBICADO_VENDIBLE]
        apartadas = [s for s in saldos if s.estado == Saldo.RESERVADO]
        hay = sum(s.cantidad for s in vendibles)
        if hay < cantidad:
            raise ValueError(
                f"En {origen.codigo} hay {hay} pieza(s) vendible(s) de {sku.codigo}"
                f"{' lote ' + lote.codigo if lote else ''}; no se pueden mover {cantidad}. Nada se cambió."
            )
        _restar(vendibles, cantidad)
        _incrementar(sku, destino.pk, lote_id, Saldo.UBICADO_VENDIBLE, cantidad)
        exceso = max(0, sum(s.cantidad for s in apartadas) - (hay - cantidad))
        if exceso:
            _restar(apartadas, exceso)
            _incrementar(sku, destino.pk, lote_id, Saldo.RESERVADO, exceso)
        _mov(
            sku, Movimiento.TRASLADO, cantidad, lote=lote, origen=Saldo.UBICADO_VENDIBLE, destino=Saldo.UBICADO_VENDIBLE,
            referencia=f"{origen.codigo}>{destino.codigo}", actor=actor,
        )
        registrar_evento(
            "ubicacion", origen.codigo, "existencias_movidas", actor=actor, cliente=sku.cliente,
            delta={"sku": sku.codigo, "lote": lote.codigo if lote else None, "cantidad": cantidad,
                   "a": destino.codigo, "apartadas": exceso},
            motivo=(motivo or "").strip()[:300] or f"{cantidad} × {sku.codigo} de {origen.codigo} a {destino.codigo}.",
        )
    return {"cantidad": cantidad, "apartadas": exceso}


# ─────────────────────────────────────────────────────────────────────────────
# Restock (Chema 2026-10-13): de la tarima al rack fijo, caja por caja
# ─────────────────────────────────────────────────────────────────────────────


def _vendibles_fuera_del_rack(sku, rack):
    """Saldos vendibles del producto fuera de su rack, el lote que caduca primero al frente."""
    saldos = list(
        Saldo.objects.filter(sku=sku, estado=Saldo.UBICADO_VENDIBLE, cantidad__gt=0).exclude(ubicacion=rack)
        .select_related("ubicacion", "lote")
    )
    caducidades = _caducidades(saldos)
    return sorted(saldos, key=lambda s: (_clave_fefo(s, caducidades), s.ubicacion.codigo))


def lote_para_restock(sku, rack):
    """El lote que caduca primero entre lo vendible fuera del rack (FEFO global
    lo hace valer el restock) y el saldo donde está: (lote | None, saldo) o None."""
    fuera = _vendibles_fuera_del_rack(sku, rack)
    if not fuera:
        return None
    primero = fuera[0]
    return primero.lote, primero


def sugerir_restock(sku):
    """Qué llevaría un restock del producto ahora: {rack, en_rack, capacidad,
    lote, origen, cantidad}. None si el producto no tiene rack fijo o no hay
    nada fuera del rack. `cantidad` = lo que falta para la capacidad (toda la
    existencia fuera si la capacidad no se puede calcular), tope lo que hay
    del lote en su origen."""
    from apps.catalogo.services import rack_de  # lazy por contrato

    rack = rack_de(sku)
    if rack is None:
        return None
    eleccion = lote_para_restock(sku, rack)
    if eleccion is None:
        return None
    lote, origen = eleccion
    en_rack = Saldo.objects.filter(sku=sku, ubicacion=rack, estado=Saldo.UBICADO_VENDIBLE).aggregate(t=Sum("cantidad"))["t"] or 0
    capacidad = capacidad_asignada(rack, sku)
    falta = origen.cantidad if capacidad is None else max(0, capacidad - en_rack)
    return {"rack": rack, "en_rack": en_rack, "capacidad": capacidad, "lote": lote, "origen": origen.ubicacion,
            "cantidad": min(falta, origen.cantidad)}


def restock_abierto(sku):
    return Restock.objects.filter(sku=sku, estado__in=Restock.ABIERTOS).select_related("rack", "lote", "origen").first()


def restock_necesario(sku):
    """Regla de Chema: lo apartado del producto rebasa lo que hay en su rack."""
    from apps.catalogo.services import rack_de  # lazy por contrato

    rack = rack_de(sku)
    if rack is None:
        return False
    apartado = _suma(sku, Saldo.RESERVADO)
    en_rack = Saldo.objects.filter(sku=sku, ubicacion=rack, estado=Saldo.UBICADO_VENDIBLE).aggregate(t=Sum("cantidad"))["t"] or 0
    return apartado > en_rack


def abrir_restock(sku, actor=None, pedido=None):
    """La tarea abierta del producto, o una nueva con la sugerencia de ahora.
    None si no hay rack fijo o nada que traer."""
    abierto = restock_abierto(sku)
    if abierto is not None:
        if pedido is not None and abierto.pedido_id is None:
            abierto.pedido = pedido
            abierto.save(update_fields=["pedido"])
        return abierto
    sugerencia = sugerir_restock(sku)
    if sugerencia is None or sugerencia["cantidad"] <= 0:
        return None
    restock = Restock.objects.create(
        sku=sku, rack=sugerencia["rack"], lote=sugerencia["lote"], origen=sugerencia["origen"],
        cantidad_sugerida=sugerencia["cantidad"], pedido=pedido,
    )
    registrar_evento(
        "restock", restock.folio, "restock_abierto", actor=actor, cliente=sku.cliente,
        delta={"sku": sku.codigo, "rack": restock.rack.codigo, "lote": restock.lote.codigo if restock.lote else None,
               "origen": restock.origen.codigo if restock.origen else None, "cantidad": restock.cantidad_sugerida,
               "pedido": pedido.folio if pedido else None},
        motivo=f"Restock de {sku.codigo} a {restock.rack.codigo}: trae hasta {restock.cantidad_sugerida}.",
    )
    return restock


def _abrir_restock_si_falta(sku):
    """Tras una reserva: si lo apartado rebasa el rack, nace la tarea (best-effort)."""
    try:
        if restock_necesario(sku):
            abrir_restock(sku)
    except Exception:  # noqa: BLE001 — jamás frena una reserva
        pass


def restocks_en_cola(user=None):
    """Los restocks que van ANTES de los pedidos en Mi turno: los míos en curso y los pendientes."""
    qs = Restock.objects.filter(estado__in=Restock.ABIERTOS).select_related("sku", "sku__cliente", "rack", "lote", "origen", "asignado_a")
    if user is not None:
        qs = qs.filter(Q(estado=Restock.PENDIENTE) | Q(asignado_a=user))
    return list(qs)


def tomar_restock(restock, user):
    """EMPEZAR sobre la tarea: pasa a EN_CURSO a nombre del operador (o sigue si ya era suyo)."""
    if restock.estado not in Restock.ABIERTOS:
        raise ValueError(f"{restock.folio} ya está {restock.get_estado_display().lower()}.")
    if restock.estado == Restock.EN_CURSO and restock.asignado_a_id not in (None, user.pk):
        raise ValueError(f"{restock.folio} lo tiene {restock.asignado_a.username}.")
    restock.estado, restock.asignado_a = Restock.EN_CURSO, user
    restock.save(update_fields=["estado", "asignado_a"])
    return restock


def restock_escanear_caja(restock, valor):
    """La caja escaneada (QR `P|barras|lote` o código de barras): debe ser del
    producto y del lote que caduca primero fuera del rack. Regresa (lote, origen)."""
    valor = normalizar_escaneo(valor)
    sku = restock.sku
    m = PATRON_QR_PRODUCTO.match(valor)
    barras, lote_codigo = (m.group(1), (m.group(2) or "").strip()) if m else (valor, None)
    if barras not in {c for c in (sku.codigo_barras, sku.codigo) if c}:
        raise ValueError(f"Esa caja no es de {sku.codigo} ({sku.descripcion}). Este restock es de {sku.codigo}.")
    eleccion = lote_para_restock(sku, restock.rack)
    if eleccion is None:
        raise ValueError(f"Ya no hay {sku.codigo} fuera de {restock.rack.codigo}: termina el restock.")
    lote, origen = eleccion
    esperado = lote.codigo if lote else ""
    if lote_codigo is not None and lote_codigo != esperado:
        raise ValueError(
            f"Esa caja es del lote {lote_codigo or 'sin lote'}; primero sale el lote {esperado or 'sin lote'}"
            f" (caduca antes), que está en {origen.ubicacion.codigo}."
        )
    return lote, origen.ubicacion


def restock_escanear_rack(restock, valor):
    """El rack escaneado (QR `U|nombre` o el nombre) debe ser el del restock."""
    valor = normalizar_escaneo(valor)
    m = PATRON_QR_POSICION.match(valor)
    codigo = (m.group(1) if m else valor).strip().upper()
    if codigo != restock.rack.codigo:
        raise ValueError(f"Ese es {codigo or 'otro rack'}; esta caja va a {restock.rack.codigo}.")
    return restock.rack


def restock_mover_caja(restock, lote, origen, actor):
    """Confirmación: una caja de la tarima al rack (kardex traslado) y la cuenta de movidas."""
    mover_existencias(restock.sku, lote, origen, restock.rack, 1, actor=actor, motivo=f"Restock {restock.folio}")
    Restock.objects.filter(pk=restock.pk).update(cantidad_movida=F("cantidad_movida") + 1)
    restock.refresh_from_db(fields=["cantidad_movida"])
    return restock


def terminar_restock(restock, actor, cancelar=False):
    """Cierra la tarea: las cajas movidas son las que cupieron (o se cancela sin mover)."""
    if restock.estado not in Restock.ABIERTOS:
        raise ValueError(f"{restock.folio} ya está {restock.get_estado_display().lower()}.")
    restock.estado = Restock.CANCELADO if cancelar else Restock.HECHO
    restock.cerrado = timezone.now()
    restock.save(update_fields=["estado", "cerrado"])
    registrar_evento(
        "restock", restock.folio, "restock_cancelado" if cancelar else "restock_hecho", actor=actor, cliente=restock.sku.cliente,
        delta={"sku": restock.sku.codigo, "rack": restock.rack.codigo, "movidas": restock.cantidad_movida, "sugeridas": restock.cantidad_sugerida},
        motivo=f"{restock.folio}: {restock.cantidad_movida} de {restock.cantidad_sugerida} caja(s) al rack {restock.rack.codigo}.",
    )
    return restock


def lote_que_sale_primero(sku, rack=None):
    """FEFO para el picking por QR (Chema 2026-10-13): entre lo vendible del
    rack fijo del producto (si lo tiene y hay algo ahí); si no, entre todo lo
    vendible. Regresa (lote | None, saldo) o None si no hay nada vendible."""
    base = Saldo.objects.filter(sku=sku, estado=Saldo.UBICADO_VENDIBLE, cantidad__gt=0).select_related("ubicacion", "lote")
    saldos = list(base.filter(ubicacion=rack)) if rack is not None else []
    if not saldos:
        saldos = list(base)
    if not saldos:
        return None
    caducidades = _caducidades(saldos)
    primero = sorted(saldos, key=lambda s: (_clave_fefo(s, caducidades), s.ubicacion.codigo))[0]
    return primero.lote, primero
