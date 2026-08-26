"""Servicios de envíos (contrato CONVENTIONS.md):

- `elegir_carrier(pedido) -> (carrier, servicio)`
- `generar_guia(pedido) -> Guia` (idempotente)
- `poll_tracking()` (job idempotente; command `poll_tracking`, cron cada 30 min)
- `get_adapter()`

El pedido avanza aquí solo hacia EN_TRANSITO / ENTREGADO / RETORNADO.
RECOLECTADO jamás lo pone el carrier: es el manifiesto físico (BLUEPRINT §1.4).
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.services import registrar_evento

from .adapters import EnviaAdapter, ErrorCarrier, MockAdapter
from .models import Guia, Paquete, ReglaEnvio

CARRIER_LOCAL = "local"
SERVICIO_LOCAL = "entrega_local"
SERVICIO_DEFAULT = "ground"  # Paquetexpress terrestre vía envia.com

PROVEEDOR_ENVIA = "envia"
PROVEEDOR_99MIN = "99minutos"
PROVEEDOR_MOCK = "mock"


def _proveedor_para(carrier):
    """Proveedor configurado para el carrier (TORRE['PROVEEDOR_POR_CARRIER']; default envia)."""
    mapa = settings.TORRE.get("PROVEEDOR_POR_CARRIER") or {}
    return mapa.get(carrier or "", PROVEEDOR_ENVIA)


def get_adapter(carrier=None, proveedor=None):
    """Adapter por proveedor: el de la guía manda (cancelar/rastrear van con
    quien la EMITIÓ); si no, el mapa por carrier decide. Cada proveedor gatea
    con su key+modo — generar guías reales cuesta dinero, así que solo "full"
    habla con la API real; sin configuración → Mock. El slug de 99minutos sin
    adapter directo configurado viaja por envia (fallback de configuración)."""
    elegido = proveedor or _proveedor_para(carrier)
    if elegido == PROVEEDOR_MOCK:
        return MockAdapter()
    if getattr(settings, "ENVIA_API_KEY", "") and getattr(settings, "ENVIA_MODO", "cotizar") == "full":
        return EnviaAdapter()
    return MockAdapter()


def _flota_propia():
    """TORRE["FLOTA_PROPIA"]: sin flota, el carrier "local" no es elegible."""
    return bool(settings.TORRE.get("FLOTA_PROPIA", False))


def elegir_carrier(pedido):
    """Regresa `(carrier, servicio)` para el pedido.

    Evalúa ReglaEnvio por prioridad (menor gana; en empate, la regla del
    cliente le gana a la global). Sin regla aplicable:
    - pedido local CON flota propia → ("local", "entrega_local") — sin guía externa;
    - lo demás → carrier preferente del cliente (Colima: paquetexpress) vía envia.

    Sin flota propia (TORRE["FLOTA_PROPIA"]=False) las reglas con carrier
    "local" se saltan y los pedidos es_local viajan con su carrier real; las
    guías "local" ya emitidas no se tocan (datos viejos).
    """
    flota = _flota_propia()
    reglas = ReglaEnvio.objects.filter(Q(cliente=pedido.cliente) | Q(cliente__isnull=True))
    for regla in sorted(reglas, key=lambda r: (r.prioridad, r.cliente_id is None, r.pk)):
        if regla.carrier == CARRIER_LOCAL and not flota:
            continue  # regla de flota propia sin flota: carril muerto, se salta
        if regla.aplica_a(pedido):
            return (regla.carrier, regla.servicio)
    if pedido.es_local and flota:
        return (CARRIER_LOCAL, SERVICIO_LOCAL)
    return (pedido.cliente.carrier_preferente or "paquetexpress", SERVICIO_DEFAULT)


def _crear_guia(pedido, carrier, servicio, paquete=None):
    """Crea UNA guía (del pedido completo o de un paquete específico)."""
    ahora = timezone.now()
    sufijo = f"-{paquete.numero}" if paquete is not None else ""

    if carrier == CARRIER_LOCAL:
        costo_local = (paquete.precio_cotizado if paquete is not None
                       else Decimal(str(settings.TORRE.get("TARIFA_LOCAL_MXN", 100))))
        guia = Guia.objects.create(
            pedido=pedido, paquete=paquete, carrier=carrier, servicio=servicio,
            numero=f"LOCAL-{pedido.folio}{sufijo}",
            costo_cotizado=costo_local, costo_preferencial=costo_local,
            etiqueta_url="", proveedor="local", estado=Guia.GUIA_CREADA,
            ultimo_evento="Entrega local propia: sin guía externa",
            ts_ultimo_movimiento=ahora,
        )
    else:
        adapter = get_adapter(carrier=carrier)
        costo_plan = paquete.precio_cotizado if paquete is not None else None
        try:
            datos = adapter.generar(pedido, carrier, servicio, paquete=paquete)
        except ErrorCarrier as exc:
            registrar_evento(
                "pedido", pedido.pk, "error_generacion_guia", cliente=pedido.cliente,
                delta={"carrier": carrier, "servicio": servicio,
                       "paquete": paquete.numero if paquete else None},
                motivo=str(exc)[:300],
            )
            raise
        costo = datos.get("costo") or costo_plan or Decimal("0.00")
        guia = Guia.objects.create(
            pedido=pedido, paquete=paquete, carrier=carrier, servicio=servicio,
            numero=datos["numero"],
            costo_cotizado=costo_plan or costo,
            costo_preferencial=costo,
            etiqueta_url=datos.get("etiqueta_url", ""),
            proveedor=getattr(adapter, "PROVEEDOR", PROVEEDOR_ENVIA),
            estado=Guia.GUIA_CREADA,
            ultimo_evento="Guía creada",
            ts_ultimo_movimiento=ahora,
            raw=datos.get("raw") or {},
        )

    registrar_evento(
        "guia", guia.pk, "guia_generada", cliente=pedido.cliente,
        delta={
            "pedido": pedido.folio, "carrier": carrier, "servicio": servicio,
            "numero": guia.numero, "paquete": paquete.numero if paquete else None,
            "costo_preferencial": str(guia.costo_preferencial),
        },
    )
    return guia


def _carrier_de_paquete(pedido, paquete):
    """(carrier, servicio) reales para la guía de un paquete.

    Un plan viejo pudo guardar carrier="local" cuando había flota propia;
    sin flota (TORRE["FLOTA_PROPIA"]=False) esa guía LOCAL-* jamás saldría
    del corral (POD escondido, poller la ignora) → se re-resuelve con
    elegir_carrier IGNORANDO el carrier guardado.
    """
    carrier = paquete.carrier or ""
    servicio = paquete.servicio or ""
    if not carrier or (carrier == CARRIER_LOCAL and not _flota_propia()):
        carrier, servicio = elegir_carrier(pedido)
    return carrier, servicio


def generar_guias(pedido):
    """Genera las guías del pedido: UNA POR PAQUETE del plan de envío.

    Si el pedido no tiene plan, se planifica aquí (división ≤20 kg optimizada
    por costo, ver cotizador.planificar_envio). Idempotente por paquete: un
    paquete con guía activa no genera otra. Solo un RETORNO libera reexpedición.

    El commit es POR PAQUETE (atomic propio por guía): una guía YA COMPRADA
    al carrier jamás se revierte porque otra caja falle — el error del
    paquete caído se acumula y se relanza al final; reintentar solo genera
    lo que falta (cero recompras).
    """
    from .cotizador import planificar_envio  # lazy: evita ciclo en carga

    with transaction.atomic():
        paquetes = list(pedido.paquetes.select_for_update().all())
        if not paquetes:
            try:
                paquetes = planificar_envio(pedido)
            except ValueError:
                paquetes = []

    guias = []
    error_pendiente = None
    if not paquetes:
        # Camino legacy (pedidos sin líneas planificables): una sola guía.
        with transaction.atomic():
            # Candado sobre el pedido: dos llamadas concurrentes se serializan.
            type(pedido).objects.select_for_update().get(pk=pedido.pk)
            existente = (
                Guia.objects.select_for_update()
                .filter(pedido=pedido)
                .exclude(estado__in=list(Guia.ESTADOS_INACTIVOS))
                .order_by("-id")
                .first()
            )
            if existente is not None:
                guias = [existente]
            else:
                carrier, servicio = elegir_carrier(pedido)
                guias = [_crear_guia(pedido, carrier, servicio)]
    else:
        for paquete in paquetes:
            try:
                with transaction.atomic():
                    # Candado por caja: quien gane el lock compra la guía; el
                    # otro la encuentra ya activa (cero guías dobles).
                    Paquete.objects.select_for_update().get(pk=paquete.pk)
                    activa = (
                        Guia.objects.select_for_update()
                        .filter(paquete=paquete)
                        .exclude(estado__in=list(Guia.ESTADOS_INACTIVOS))
                        .order_by("-id")
                        .first()
                    )
                    if activa is not None:
                        guias.append(activa)
                        continue
                    carrier, servicio = _carrier_de_paquete(pedido, paquete)
                    guias.append(_crear_guia(pedido, carrier, servicio, paquete=paquete))
            except ErrorCarrier as exc:
                # El atomic del paquete se revirtió (y con él su evento
                # interno): se re-registra aquí para que la falla quede
                # auditada aunque las demás guías sí hayan salido.
                error_pendiente = exc
                registrar_evento(
                    "pedido", pedido.pk, "error_generacion_guia", cliente=pedido.cliente,
                    delta={"paquete": paquete.numero, "carrier": paquete.carrier or ""},
                    motivo=str(exc)[:300],
                )

    if error_pendiente is not None:
        # Las guías que sí salieron quedan committeadas; el pedido sigue
        # EMPACADO y recuperable desde Salida (reintento = solo lo que falta).
        raise error_pendiente

    if pedido.estado == "EMPACADO":
        numeros = ", ".join(g.numero for g in guias)
        try:
            pedido.transicionar("GUIA_GENERADA", motivo=f"{len(guias)} guía(s): {numeros}")
        except ValueError:
            pass  # otro flujo ya movió el pedido; las guías quedan ligadas igual
    return guias


def generar_guia(pedido):
    """Compatibilidad: genera todas las guías del pedido y regresa la primera."""
    return generar_guias(pedido)[0]


def poll_tracking():
    """Job idempotente: rastrea toda guía no terminal y sincroniza pedido e incidencias.

    - Normaliza el estado del carrier y transiciona la guía.
    - ENTREGADO / EN_TRANSITO / EN_RUTA → avanza el pedido.
    - INTENTO_FALLIDO → incidencia RF prioridad P1.
    - RETORNO → pedido RETORNADO + incidencia RF.
    - Sin movimiento > umbral por ruta (settings.TORRE) → incidencia RET.
    - Si el rastreo falla, se cuenta como error de integración y NO se abren
      incidencias falsas (BLUEPRINT §1.4).
    """
    resumen = {"rastreadas": 0, "actualizadas": 0, "incidencias": 0, "errores": 0}
    ahora = timezone.now()
    guias = (
        Guia.objects.exclude(estado__in=list(Guia.ESTADOS_TERMINALES))
        .exclude(carrier=CARRIER_LOCAL)
        .select_related("pedido", "pedido__cliente")
    )
    adapters = {}  # una guía se rastrea con el proveedor que la emitió
    for guia in guias:
        resumen["rastreadas"] += 1
        proveedor = guia.proveedor or PROVEEDOR_ENVIA
        adapter = adapters.get(proveedor)
        if adapter is None:
            adapter = adapters[proveedor] = get_adapter(proveedor=proveedor)
        try:
            info = adapter.rastrear(guia.numero)
        except ErrorCarrier as exc:
            resumen["errores"] += 1
            registrar_evento(
                "guia", guia.pk, "error_rastreo", cliente=guia.pedido.cliente,
                delta={"numero": guia.numero}, motivo=str(exc)[:300],
            )
            continue
        parcial = _procesar_rastreo(guia, info, ahora)
        resumen["actualizadas"] += parcial["actualizada"]
        resumen["incidencias"] += parcial["incidencias"]
    return resumen


# ── Internos del poller ──

def _procesar_rastreo(guia, info, ahora):
    pedido = guia.pedido
    estado_nuevo = info.get("estado")
    descripcion = (info.get("descripcion") or "")[:300]
    resultado = {"actualizada": 0, "incidencias": 0}

    cambio = bool(estado_nuevo) and estado_nuevo != guia.estado
    if cambio:
        try:
            guia.transicionar(estado_nuevo, motivo=descripcion)
        except ValueError:
            registrar_evento(
                "guia", guia.pk, "tracking_fuera_de_secuencia", cliente=pedido.cliente,
                delta={"estado_guia": guia.estado, "estado_carrier": estado_nuevo},
                motivo=descripcion,
            )
            cambio = False

    hubo_movimiento = cambio or (bool(descripcion) and descripcion != guia.ultimo_evento)
    campos = []
    if descripcion and descripcion != guia.ultimo_evento:
        guia.ultimo_evento = descripcion
        campos.append("ultimo_evento")
    if info.get("raw"):
        guia.raw = info["raw"]
        campos.append("raw")
    if hubo_movimiento:
        guia.ts_ultimo_movimiento = info.get("ts_evento") or ahora
        campos.append("ts_ultimo_movimiento")
    if campos:
        guia.save(update_fields=sorted(set(campos)))

    if cambio:
        resultado["actualizada"] = 1
        resultado["incidencias"] += _aplicar_efectos(guia, estado_nuevo, descripcion)
    elif guia.estado in {Guia.EN_TRANSITO, Guia.EN_RUTA} and pedido.estado == "RECOLECTADO":
        # Resincroniza pedidos rezagados (p. ej. el manifiesto se marcó
        # después del primer escaneo del carrier).
        _transicionar_pedido(pedido, "EN_TRANSITO", motivo=descripcion)

    if guia.estado not in Guia.ESTADOS_TERMINALES and not hubo_movimiento:
        resultado["incidencias"] += _revisar_sin_movimiento(guia, ahora)
    return resultado


def _aplicar_efectos(guia, estado, descripcion):
    """Efectos del cambio de estado de la guía sobre el pedido/incidencias."""
    pedido = guia.pedido
    abiertas = 0
    if estado in {Guia.EN_TRANSITO, Guia.EN_RUTA}:
        _transicionar_pedido(pedido, "EN_TRANSITO", motivo=descripcion)
    elif estado == Guia.ENTREGADO:
        _transicionar_pedido(pedido, "ENTREGADO", motivo=descripcion)
    elif estado == Guia.INTENTO_FALLIDO:
        texto = (
            f"Intento de entrega fallido en la guía {guia.numero} ({guia.carrier}). "
            f"Último evento del carrier: {descripcion or 'sin detalle'}."
        )
        if _abrir_incidencia(pedido, "RF", texto, prioridad="P1"):
            abiertas += 1
    elif estado == Guia.RETORNO:
        _transicionar_pedido(pedido, "RETORNADO", motivo=descripcion)
        texto = (
            f"El carrier marcó retorno al remitente en la guía {guia.numero} ({guia.carrier}). "
            f"Último evento: {descripcion or 'sin detalle'}. Requiere reingreso y reexpedición."
        )
        if _abrir_incidencia(pedido, "RF", texto):
            abiertas += 1
    # RECOLECTADO del carrier NO mueve el pedido: el manifiesto es autoritativo.
    return abiertas


def _revisar_sin_movimiento(guia, ahora):
    """Guía viva sin movimiento por más del umbral de su ruta → incidencia RET."""
    pedido = guia.pedido
    torre = settings.TORRE
    horas = torre["SIN_MOVIMIENTO_LOCAL_HORAS"] if pedido.es_local else torre["SIN_MOVIMIENTO_FORANEO_HORAS"]
    referencia = guia.ts_ultimo_movimiento or guia.creado
    if referencia is None or ahora - referencia <= timedelta(hours=horas):
        return 0
    if pedido.incidencia_activa:
        return 0  # ya hay una incidencia con la pelota en juego: no duplicar cada poll
    texto = (
        f"Guía {guia.numero} ({guia.carrier}) sin movimiento por más de {horas} h "
        f"en ruta {'local' if pedido.es_local else 'foránea'}. "
        f"Último evento: {guia.ultimo_evento or 'sin eventos'}."
    )
    incidencia = _abrir_incidencia(pedido, "RET", texto)
    if incidencia is None:
        return 0
    registrar_evento(
        "guia", guia.pk, "alerta_sin_movimiento", cliente=pedido.cliente,
        delta={"numero": guia.numero, "horas_umbral": horas},
    )
    return 1


def _transicionar_pedido(pedido, destino, motivo=""):
    """Avanza el pedido según tracking, tolerando desfase con el piso.

    Si el carrier ya entregó pero el pedido apenas está RECOLECTADO, se
    encadena EN_TRANSITO → ENTREGADO. Una transición inválida no truena el
    poller: el pedido se queda donde el flujo autoritativo lo tenga.
    """
    if pedido.estado == destino:
        return False
    pasos = [destino]
    if destino == "ENTREGADO" and pedido.estado == "RECOLECTADO":
        pasos = ["EN_TRANSITO", "ENTREGADO"]
    avanzo = False
    for paso in pasos:
        try:
            pedido.transicionar(paso, motivo=motivo or "Actualización por tracking del carrier")
            avanzo = True
        except ValueError:
            break
    return avanzo


def _abrir_incidencia(pedido, tipo, texto, prioridad=None):
    """Abre incidencia vía el módulo incidencias (import lazy por contrato)."""
    try:
        from apps.incidencias.services import abrir_incidencia
    except ImportError:
        registrar_evento(
            "pedido", pedido.pk, "incidencia_no_abierta_modulo_ausente",
            cliente=pedido.cliente, delta={"tipo": tipo}, motivo=texto[:300],
        )
        return None
    return abrir_incidencia(
        pedido.cliente, tipo, "auto", pedido=pedido, texto=texto, prioridad=prioridad
    )
