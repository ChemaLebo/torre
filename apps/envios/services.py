"""Servicios de envíos (contrato CONVENTIONS.md):

- `elegir_carrier(pedido) -> (carrier, servicio)`
- `carrier_preferido(pedido) -> carrier` (la ReglaEnvio prefiere, el precio respalda)
- `generar_guia(pedido) -> Guia` (idempotente)
- `poll_tracking()` (job idempotente; command `poll_tracking`, cron cada 30 min)
- `get_adapter()`

El pedido avanza aquí solo hacia EN_TRANSITO / ENTREGADO / RETORNADO.
RECOLECTADO jamás lo pone el carrier: es el manifiesto físico (BLUEPRINT §1.4).
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.services import registrar_evento

from .adapters import Adapter99Minutos, EnviaAdapter, ErrorCarrier, MockAdapter
from .models import EventoGuia, Guia, LineaManifiesto, Manifiesto, Paquete, Recoleccion, ReglaEnvio

CARRIER_LOCAL = "local"
SERVICIO_LOCAL = "entrega_local"
SERVICIO_DEFAULT = "ground"  # Paquetexpress terrestre vía envia.com

PROVEEDOR_ENVIA = "envia"
PROVEEDOR_99MIN = "99minutos"
PROVEEDOR_MOCK = "mock"
# Valor de Pedido.carrier_forzado que significa "la lista de envia.com por precio".
CARRIER_POOL_ENVIA = "envia"


class SinPaqueteria(ErrorCarrier):
    """Ningún carrier cotiza el pedido: no hay plan de cajas y NO se compra
    guía (Chema 2026-09-24: "si no cotiza o no hay plan, que salga un aviso").
    Mesa elige paquetería en la incidencia interna que nace con esto."""


def _carrier_forzado(pedido):
    return (getattr(pedido, "carrier_forzado", "") or "").strip()


def opciones_paqueteria():
    """[(código, etiqueta)] para el selector de Mesa: 99minutos directo, iMile,
    la lista de envia por precio y cada carrier de esa lista por separado."""
    base = [
        ("noventa9Minutos", "99minutos directo"),
        ("imile", "iMile (vía envia.com)"),
        (CARRIER_POOL_ENVIA, "envia.com: el más barato de su lista"),
    ]
    vistos = {c for c, _ in base}
    return base + [
        (c, f"{c} (vía envia.com)") for c in settings.TORRE["CARRIERS_COTIZAR"] if c not in vistos
    ]


def etiqueta_paqueteria(codigo):
    return dict(opciones_paqueteria()).get(codigo, codigo)


def url_rastreo_carrier(carrier, numero):
    """URL pública de rastreo del carrier para una guía, según
    TORRE["RASTREO_CARRIER_URL"]; "" si el carrier no tiene patrón (entrega
    local, mock) o la guía no trae número."""
    from urllib.parse import quote

    patron = settings.TORRE.get("RASTREO_CARRIER_URL", {}).get(carrier or "", "")
    if not patron or not numero:
        return ""
    return patron.format(numero=quote(str(numero), safe=""))


def _proveedor_para(carrier, cliente=None):
    """Proveedor para el carrier. La integración del CLIENTE manda primero
    (flip por cliente, sep-2026); sin cliente aplica el mapa global
    TORRE['PROVEEDOR_POR_CARRIER'] (default envia)."""
    if (cliente is not None and carrier == "noventa9Minutos"
            and getattr(cliente, "integracion_envios", "") == "99minutos"):
        return PROVEEDOR_99MIN
    mapa = settings.TORRE.get("PROVEEDOR_POR_CARRIER") or {}
    return mapa.get(carrier or "", PROVEEDOR_ENVIA)


def get_adapter(carrier=None, proveedor=None, cliente=None):
    """Adapter por proveedor: el de la guía manda (cancelar/rastrear van con
    quien la EMITIÓ); si no, el mapa por carrier decide. Cada proveedor gatea
    con su key+modo — generar guías reales cuesta dinero, así que solo "full"
    habla con la API real; sin configuración → Mock. El slug de 99minutos sin
    adapter directo configurado viaja por envia (fallback de configuración)."""
    elegido = proveedor or _proveedor_para(carrier, cliente)
    if elegido == PROVEEDOR_MOCK:
        return MockAdapter()
    if elegido == PROVEEDOR_99MIN and _99min_habilitado("full"):
        return Adapter99Minutos()
    return _adapter_envia_generar()


def _99min_habilitado(modo_requerido):
    modo = getattr(settings, "NOVENTA9_MODO", "off")
    habil = modo == "full" if modo_requerido == "full" else modo != "off"
    return bool(getattr(settings, "NOVENTA9_API_KEY", "")) and habil


def _adapter_envia_generar():
    if getattr(settings, "ENVIA_API_KEY", "") and getattr(settings, "ENVIA_MODO", "cotizar") == "full":
        return EnviaAdapter()
    return MockAdapter()


def _adapter_envia_cotizacion():
    if getattr(settings, "ENVIA_API_KEY", "") and getattr(settings, "ENVIA_MODO", "cotizar") != "off":
        return EnviaAdapter()
    return MockAdapter()


def get_adapter_cotizacion(carrier, cliente=None):
    """Adapter para COTIZAR ese carrier. Mismo routing que get_adapter pero el
    gating es modo != "off": cotizar no cuesta dinero, generar sí exige "full"."""
    if _proveedor_para(carrier, cliente) == PROVEEDOR_99MIN and _99min_habilitado("cotizar"):
        return Adapter99Minutos()
    return _adapter_envia_cotizacion()


def cotizar_lane_carrier(carrier, cp_destino, peso_kg, dims=None, cliente=None):
    """Fila de UN carrier vía su proveedor. Con NOVENTA9_FALLBACK_ENVIA, un
    fallo del directo de 99minutos re-cotiza ese carrier por envia."""
    adapter = get_adapter_cotizacion(carrier, cliente)
    fila = adapter.cotizar_lane(carrier, cp_destino, peso_kg, dims)
    if fila.get("ok") or getattr(adapter, "PROVEEDOR", "") != PROVEEDOR_99MIN:
        return fila
    if not getattr(settings, "NOVENTA9_FALLBACK_ENVIA", False):
        return fila
    return _adapter_envia_cotizacion().cotizar_lane(carrier, cp_destino, peso_kg, dims)


def _flota_propia():
    """TORRE["FLOTA_PROPIA"]: sin flota, el carrier "local" no es elegible."""
    return bool(settings.TORRE.get("FLOTA_PROPIA", False))


def elegir_carrier(pedido):
    """Regresa `(carrier, servicio)` para el pedido.

    Evalúa ReglaEnvio por prioridad (menor gana; en empate, la regla del
    cliente le gana a la global). Sin regla aplicable:
    - pedido local CON flota propia → ("local", "entrega_local") — sin guía externa;
    - cliente en "reparto" → la carta del pedido (envios.reparto, una sola
      vez por pedido; sin pesos configurados cae al default, fail-safe);
    - cliente en "99minutos" → noventa9Minutos directo;
    - lo demás → carrier preferente del cliente (Colima: paquetexpress) vía envia.

    Sin flota propia (TORRE["FLOTA_PROPIA"]=False) las reglas con carrier
    "local" se saltan y los pedidos es_local viajan con su carrier real; las
    guías "local" ya emitidas no se tocan (datos viejos).
    """
    forzado = _carrier_forzado(pedido)
    if forzado and forzado != CARRIER_POOL_ENVIA:
        return (forzado, SERVICIO_DEFAULT)  # Mesa lo decidió para este pedido: gana a todo
    flota = _flota_propia()
    regla = _regla_aplicable(pedido, flota)
    if regla is not None:
        return regla
    if pedido.es_local and flota:
        return (CARRIER_LOCAL, SERVICIO_LOCAL)
    carta = _carta_reparto(pedido)
    if carta:
        return (carta, SERVICIO_DEFAULT)
    if getattr(pedido.cliente, "integracion_envios", "") == "99minutos":
        # Flip por cliente: sus envíos viajan por 99minutos directo. Las
        # ReglaEnvio siguen mandando arriba (excepciones explícitas ganan).
        return ("noventa9Minutos", SERVICIO_DEFAULT)
    return (pedido.cliente.carrier_preferente or "paquetexpress", SERVICIO_DEFAULT)


def _regla_aplicable(pedido, flota):
    """(carrier, servicio) de la primera ReglaEnvio que aplica al pedido
    (prioridad menor gana; en empate la del cliente le gana a la global), o
    None. Sin flota, las reglas con carrier "local" son carril muerto."""
    reglas = ReglaEnvio.objects.filter(Q(cliente=pedido.cliente) | Q(cliente__isnull=True))
    for regla in sorted(reglas, key=lambda r: (r.prioridad, r.cliente_id is None, r.pk)):
        if regla.carrier == CARRIER_LOCAL and not flota:
            continue  # regla de flota propia sin flota: carril muerto, se salta
        if regla.aplica_a(pedido):
            return (regla.carrier, regla.servicio)
    return None


def _cliente_reparte(cliente):
    return getattr(cliente, "integracion_envios", "") == "reparto" and bool(cliente.reparto_pesos)


def _carta_reparto(pedido):
    """Carta del reparto por porcentajes si el cliente está en "reparto" con
    pesos; "" si no aplica. Se saca una sola vez por pedido (queda en
    Pedido.reparto_carrier). No revisa reglas ni flota: quien llama ya decidió."""
    if not _cliente_reparte(pedido.cliente):
        return ""
    if pedido.reparto_carrier:
        return pedido.reparto_carrier
    from .reparto import sacar_carta  # lazy: evita ciclo en carga
    return sacar_carta(pedido)


def carriers_del_pedido(pedido):
    """Carriers que la config VIGENTE permite cotizar/comprar para este pedido.
    Cliente en reparto: el carrier de la ReglaEnvio que aplique (decidió antes,
    no consume carta) o la carta del pedido; cliente 99minutos directo:
    noventa9Minutos; los demás: la lista blanca CARRIERS_COTIZAR (las reglas
    no acotan el plan de esos clientes: PREFIEREN, ver carrier_preferido). Lo
    usan el planificador y el replan. La paquetería forzada por Mesa
    (Pedido.carrier_forzado) manda sobre todo lo demás."""
    forzado = _carrier_forzado(pedido)
    if forzado == CARRIER_POOL_ENVIA:
        return list(settings.TORRE["CARRIERS_COTIZAR"])
    if forzado:
        return [forzado]
    if _cliente_reparte(pedido.cliente):
        flota = _flota_propia()
        regla = _regla_aplicable(pedido, flota)
        if regla is not None and regla[0] != CARRIER_LOCAL:
            return [regla[0]]
        if regla is None and not (pedido.es_local and flota):
            return [_carta_reparto(pedido)]
    elif getattr(pedido.cliente, "integracion_envios", "") == "99minutos":
        return ["noventa9Minutos"]
    return list(settings.TORRE["CARRIERS_COTIZAR"])


def carrier_preferido(pedido):
    """Carrier que el cotizador PREFIERE para el pedido (Chema 2026-09-22: las
    reglas primero, el precio después): el de la ReglaEnvio que le aplique
    (Colima: locales → estafeta, foráneos → imile) o, sin regla, el prioritario
    global TORRE["CARRIER_PRIORITARIO"]; "" = manda el precio. La regla
    prefiere, no acota: si el preferido no cotiza el lane (o no cotiza TODAS
    las cajas del plan) el resto de carriers_del_pedido compite por precio.
    Una regla con carrier "local" sin flota propia no prefiere nada (carril
    muerto, igual que en elegir_carrier); con flota, el atajo local del
    planificador decide antes de cotizar. Forzada por Mesa: esa (o "" si
    forzó la lista de envia: manda el precio)."""
    forzado = _carrier_forzado(pedido)
    if forzado:
        return "" if forzado == CARRIER_POOL_ENVIA else forzado
    regla = _regla_aplicable(pedido, _flota_propia())
    if regla is not None:
        return "" if regla[0] == CARRIER_LOCAL else regla[0]
    return settings.TORRE.get("CARRIER_PRIORITARIO") or ""


def _respaldo_envia(adapter, pedido, carrier, exc):
    """Con NOVENTA9_FALLBACK_ENVIA: un fallo del directo de 99minutos reintenta
    UNA vez por envia (tarifa de envia, auditado). Sin flag → None (surface)."""
    if getattr(adapter, "PROVEEDOR", "") != PROVEEDOR_99MIN:
        return None
    if not getattr(settings, "NOVENTA9_FALLBACK_ENVIA", False):
        return None
    registrar_evento(
        "pedido", pedido.pk, "fallback_envia", cliente=pedido.cliente,
        delta={"carrier": carrier},
        motivo=f"99minutos directo falló ({str(exc)[:200]}); se reintenta por envia.",
    )
    return _adapter_envia_generar()


def _guardar_etiqueta_pdf(guia, pdf):
    """Proveedores que entregan la etiqueta en línea (base64): se persiste y la
    URL interna del archivo alimenta el link de Salida."""
    if not pdf:
        return
    from django.core.files.base import ContentFile
    guia.etiqueta_pdf.save(f"{guia.numero}.pdf", ContentFile(pdf), save=True)
    if not guia.etiqueta_url:
        guia.etiqueta_url = guia.etiqueta_pdf.url
        guia.save(update_fields=["etiqueta_url"])


# El carrier rechazó la CIUDAD del destino contra su catálogo (iMile:
# "consignee city [Ciudad del Carmen] not exist", "Zip Code [72830] does not
# match city [Puebla]"). Es texto del conector, no un código: patrón.
_RE_ERROR_CIUDAD = re.compile(r"city\s*\[[^\]]*\]\s*not exist|does not match city", re.IGNORECASE)


def _reintentar_con_municipio(adapter, pedido, carrier, servicio, paquete, exc):
    """Reintento dirigido (Chema 2026-09-23): el carrier rechazó la ciudad al
    comprar y el catálogo del CP trae un municipio distinto de la localidad →
    se compra UNA sola vez más con el municipio, mismo carrier y misma
    cotización (iMile suele conocer el nombre corto: Carmen, Puebla,
    Querétaro; la localidad trae el formal: Ciudad del Carmen, Heroica Puebla
    de Zaragoza). Vale para cualquier carrier: el disparador es el texto del
    error, no el carrier. Regresa los datos de la guía; None si no aplica
    (otro error, sin catálogo, municipio vacío o igual a la localidad); si el
    municipio también falla, ErrorCarrier con los dos mensajes. Auditado en
    ambos casos (guia_reintento_municipio)."""
    if not _RE_ERROR_CIUDAD.search(str(exc)):
        return None
    from .localidades import localidad_por_cp  # lazy: modelo + HTTP
    localidad = localidad_por_cp(pedido.cp)
    if localidad is None or not (localidad.municipio or "").strip():
        return None
    rechazada = (localidad.localidad or (pedido.direccion or {}).get("city") or "").strip()
    municipio = localidad.municipio.strip()
    if municipio.lower() == rechazada.lower():
        return None
    delta = {"carrier": carrier, "paquete": paquete.numero if paquete else None, "cp": pedido.cp,
             "ciudad_rechazada": rechazada, "municipio": municipio}
    try:
        datos = adapter.generar(pedido, carrier, servicio, paquete=paquete, ciudad=municipio)
    except ErrorCarrier as exc2:
        registrar_evento(
            "pedido", pedido.pk, "guia_reintento_municipio", cliente=pedido.cliente,
            delta={**delta, "ok": False}, motivo=f"Tampoco con el municipio: {str(exc2)[:200]}",
        )
        raise ErrorCarrier(
            f"{exc} · Se reintentó con el municipio «{municipio}» y también falló: {exc2}"
        ) from exc2
    registrar_evento(
        "pedido", pedido.pk, "guia_reintento_municipio", cliente=pedido.cliente,
        delta={**delta, "ok": True},
        motivo=f"{carrier} rechazó la ciudad «{rechazada}»; la guía salió con el municipio «{municipio}».",
    )
    return datos


def _crear_guia(pedido, carrier, servicio, paquete=None):
    """Crea UNA guía (del pedido completo o de un paquete específico). Si el
    carrier rechaza la ciudad, un reintento con el municipio del catálogo
    (_reintentar_con_municipio) antes de darse por vencido."""
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
        adapter = get_adapter(carrier=carrier, cliente=pedido.cliente)
        costo_plan = paquete.precio_cotizado if paquete is not None else None
        try:
            datos = adapter.generar(pedido, carrier, servicio, paquete=paquete)
        except ErrorCarrier as exc:
            datos = _reintentar_con_municipio(adapter, pedido, carrier, servicio, paquete, exc)
            if datos is None:
                adapter = _respaldo_envia(adapter, pedido, carrier, exc)
                if adapter is None:
                    registrar_evento(
                        "pedido", pedido.pk, "error_generacion_guia", cliente=pedido.cliente,
                        delta={"carrier": carrier, "servicio": servicio,
                               "paquete": paquete.numero if paquete else None},
                        motivo=str(exc)[:300],
                    )
                    raise
                try:
                    datos = adapter.generar(pedido, carrier, servicio, paquete=paquete)
                except ErrorCarrier as exc2:
                    registrar_evento(
                        "pedido", pedido.pk, "error_generacion_guia", cliente=pedido.cliente,
                        delta={"carrier": carrier, "servicio": servicio, "respaldo": "envia",
                               "paquete": paquete.numero if paquete else None},
                        motivo=str(exc2)[:300],
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
        _guardar_etiqueta_pdf(guia, datos.get("etiqueta_pdf"))

    registrar_evento(
        "guia", guia.pk, "guia_generada", cliente=pedido.cliente,
        delta={
            "pedido": pedido.folio, "carrier": carrier, "servicio": servicio,
            "numero": guia.numero, "paquete": paquete.numero if paquete else None,
            "costo_preferencial": str(guia.costo_preferencial),
        },
    )
    return guia


def _replan_paquete(pedido, paquete, carrier_viejo):
    """El plan guardó un carrier que la config vigente ya no permite (lista
    recortada o cliente flipeado de integración): se re-cotiza el lane AL
    GENERAR y el paquete se reasigna a la mejor opción permitida (el carrier
    preferido del pedido si cotiza, si no el más barato). El plan se conserva
    como plan (división, corral, precio de referencia); la config manda a la
    hora de comprar la guía."""
    from .cotizador import cotizar_lane, elegir_entre  # lazy: evita ciclo en carga

    dims = (paquete.largo_cm, paquete.ancho_cm, paquete.alto_cm)
    filas = [
        f for f in cotizar_lane(pedido.cp, paquete.peso_kg, dims, cliente=pedido.cliente,
                                carriers=carriers_del_pedido(pedido))
        if f["ok"] and f["precio"] is not None
    ]
    if not filas:
        raise ErrorCarrier(
            f"El plan del paquete {paquete.numero} traía {carrier_viejo} (ya no "
            f"permitido) y ningún carrier vigente cotiza CP {pedido.cp}. "
            "Revisar con Mesa de Control."
        )
    mejor = elegir_entre(filas, carrier_preferido(pedido))
    paquete.carrier = mejor["carrier"]
    paquete.servicio = mejor["servicio"]
    paquete.precio_cotizado = mejor["precio"]
    paquete.save(update_fields=["carrier", "servicio", "precio_cotizado"])
    registrar_evento(
        "pedido", pedido.pk, "replan_paquete", cliente=pedido.cliente,
        delta={"paquete": paquete.numero, "antes": carrier_viejo,
               "ahora": mejor["carrier"], "precio": float(mejor["precio"])},
        motivo="El carrier del plan ya no está permitido por la config vigente; "
               "el paquete se re-cotizó al generar la guía.",
    )
    return mejor["carrier"], mejor["servicio"]


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
    elif carrier != CARRIER_LOCAL and carrier not in carriers_del_pedido(pedido):
        # Plan viejo vs config nueva (#10): la config vigente manda al generar.
        carrier, servicio = _replan_paquete(pedido, paquete, carrier)
    return carrier, servicio


def generar_guias(pedido):
    """Genera las guías del pedido: UNA POR PAQUETE del plan de envío.

    Si el pedido no tiene plan, se planifica aquí (división ≤20 kg optimizada
    por costo, ver cotizador.planificar_envio). Idempotente por paquete: un
    paquete con guía activa no genera otra. Solo un RETORNO libera reexpedición.
    Un carrier que cotizó el lane y falla al COMPRAR no se sustituye solo
    (Chema 2026-09-22): el error queda auditado (error_generacion_guia) y la
    caja se reintenta desde el empaque; el respaldo por precio vive solo al
    cotizar (cotizador.elegir_entre).

    El commit es POR PAQUETE (atomic propio por guía): una guía YA COMPRADA
    al carrier jamás se revierte porque otra caja falle — el error del
    paquete caído se acumula y se relanza al final; reintentar solo genera
    lo que falta (cero recompras).
    """
    from .cotizador import planificar_envio  # lazy: evita ciclo en carga

    # La carta del reparto se saca ANTES de cualquier atomic de aquí abajo:
    # si el carrier falla, la carta se queda con el pedido (es el dato del
    # fallo que se quiere medir), no se revierte con la guía.
    carriers_del_pedido(pedido)
    sin_plan = None
    with transaction.atomic():
        paquetes = list(pedido.paquetes.select_for_update().all())
        if not paquetes:
            try:
                paquetes = planificar_envio(pedido)
            except ValueError as exc:
                sin_plan = exc
    if sin_plan is not None:
        # Ningún carrier cotiza: antes se caía al camino legacy y se compraba
        # UNA guía con el pedido entero (cajas de 30 kg, PED-00103..109).
        # Ahora no se compra nada: incidencia interna y aviso al piso.
        incidencia = _avisar_sin_paqueteria(pedido, str(sin_plan))
        folio_inc = f" Mesa elige paquetería en la incidencia {incidencia.folio}." if incidencia else ""
        raise SinPaqueteria(f"{sin_plan} No hay plan de cajas y no se compra guía.{folio_inc}")

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
                # Sin reintento automático con otro carrier: se registra y el
                # pedido se frena, para ver cada fallo mientras se depura. El
                # atomic del paquete se revirtió (y con él su evento interno):
                # se re-registra aquí para que la falla quede auditada aunque
                # las demás guías sí hayan salido.
                error_pendiente = exc
                registrar_evento(
                    "pedido", pedido.pk, "error_generacion_guia", cliente=pedido.cliente,
                    delta={"paquete": paquete.numero, "carrier": paquete.carrier or "",
                           "servicio": paquete.servicio or ""},
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


def agendar_recoleccion(carrier, fecha, hora_desde, hora_hasta, guias, actor,
                        instrucciones=""):
    """Agenda UNA recolección del carrier para todas las guías dadas.

    Dedup duro: jamás doble booking del mismo carrier el mismo día (el fee se
    cobra al balance). Solo carriers en TORRE["CARRIERS_PICKUP"] — 99minutos
    no entra: su pickup es nativo (pickUpAfter en el create).
    """
    if not (settings.TORRE.get("CARRIERS_PICKUP") or {}).get(carrier):
        raise ValueError(
            f"{carrier} no acepta recolección programada por este medio: "
            "entrega en sucursal (drop-off) o revisa la config."
        )
    if not guias:
        raise ValueError("No hay guías listas de ese carrier para recolectar.")
    if int(hora_hasta) <= int(hora_desde):
        raise ValueError("La ventana de recolección está volteada: revisa las horas.")
    existente = Recoleccion.objects.filter(carrier=carrier, fecha=fecha).first()
    if existente is not None:
        raise ValueError(
            f"Ya hay recolección de {carrier} para el {fecha} "
            f"(folio {existente.folio_carrier or 's/n'}); jamás doble booking — el fee se cobra."
        )
    adapter = _adapter_envia_generar()
    resultado = adapter.agendar_recoleccion(
        carrier, fecha, hora_desde, hora_hasta, guias, instrucciones,
    )
    recoleccion = Recoleccion.objects.create(
        carrier=carrier, fecha=fecha,
        hora_desde=int(hora_desde), hora_hasta=int(hora_hasta),
        instrucciones=instrucciones or "",
        folio_carrier=resultado.get("folio") or "",
        costo=resultado.get("costo"),
    )
    recoleccion.guias.set(guias)
    registrar_evento(
        "recoleccion", recoleccion.pk, "recoleccion_agendada", actor=actor,
        delta={
            "carrier": carrier, "fecha": str(fecha),
            "ventana": [int(hora_desde), int(hora_hasta)],
            "folio": recoleccion.folio_carrier,
            "costo": float(resultado["costo"]) if resultado.get("costo") else None,
            "guias": [g.numero for g in guias],
        },
        motivo=f"Recolección {carrier} agendada con {len(guias)} guía(s).",
    )
    return recoleccion


def guias_del_pedido(pedido):
    """Todas las guías del pedido para un expediente (Mesa y portal; Chema
    2026-09-23): una por caja cuando el envío va dividido, con carrier, número,
    caja, estado legible, si sigue viva y el rastreo público del carrier. Orden
    estable por pk (caja 1 primero); las canceladas o retornadas van igual,
    marcadas como inactivas."""
    return [
        {
            "carrier": g.carrier,
            "numero": g.numero,
            "caja": g.paquete.numero if g.paquete_id else None,
            "estado": g.get_estado_display(),
            "activa": g.es_activa,
            "url": url_rastreo_carrier(g.carrier, g.numero),
        }
        for g in pedido.guias.select_related("paquete").order_by("pk")
    ]


def cancelar_guia(guia, actor, motivo=""):
    """Cancela UNA guía comprada que aún no sale (cambio de dirección, Chema
    2026-09-23): avisa al carrier por API (envia: POST /ship/cancel/;
    99minutos: su cancel) best-effort y la deja CANCELADA en Torre pase lo que
    pase con el carrier — auditado para que Mesa persiga la falla, pero Torre
    ya no cuenta esa guía y puede comprar otra. Solo desde GUIA_CREADA: una
    guía ya recolectada no se cancela, se resuelve como incidencia."""
    if guia.estado != Guia.GUIA_CREADA:
        raise ValueError(
            f"La guía {guia.numero} está {guia.get_estado_display().lower()}: "
            "solo se cancela una guía que aún no sale."
        )
    pedido = guia.pedido
    ok, detalle = True, ""
    if guia.carrier != CARRIER_LOCAL:
        try:
            adapter = get_adapter(guia.carrier, proveedor=guia.proveedor, cliente=pedido.cliente)
            ok = bool(adapter.cancelar(guia))
        except Exception as exc:  # noqa: BLE001 — best-effort: el carrier no bloquea la cancelación
            ok, detalle = False, str(exc)[:200]
    guia.transicionar(Guia.CANCELADA, actor=actor, motivo=motivo or "Guía cancelada antes de salir")
    registrar_evento(
        "guia", guia.pk, "cancelada_carrier" if ok else "cancelacion_carrier_fallida",
        actor=actor, cliente=pedido.cliente,
        delta={"numero": guia.numero, "carrier": guia.carrier, "proveedor": guia.proveedor, "detalle": detalle},
        motivo=(motivo or f"Guía {guia.numero} cancelada.")[:300],
    )
    return ok


def recotizar_paquete(pedido, paquete):
    """Re-cotiza UNA caja contra la config vigente y la dirección actual del
    pedido (cambio de dirección: puede cambiar carrier y precio). Envuelve
    _replan_paquete; ErrorCarrier si nadie cotiza."""
    return _replan_paquete(pedido, paquete, paquete.carrier)


def registrar_manifiesto(carrier, corral, operador, salidas, chofer="", sin_escaneo=None):
    """La hoja que firma el chofer: un Manifiesto con folio por lo que subió a
    SU camión (Chema 2026-09-22). `salidas` = [(pedido, cajas)] tal como lo
    despachó pedidos.marcar_recolectado: cajas = las que salieron, o None si
    el pedido salió entero (una línea por guía activa; sin guía, una línea
    del pedido). Guarda el número de guía como quedó impreso. `sin_escaneo` =
    {"pedidos": {pk}, "cajas": {pk}} marcados "no estaba en salida · ya
    salió": sus líneas van aparte en la hoja. Regresa el Manifiesto, o None
    si no salió nada."""
    sin_escaneo = sin_escaneo or {}
    ya_pedidos = set(sin_escaneo.get("pedidos") or ())
    ya_cajas = set(sin_escaneo.get("cajas") or ())
    lineas = []
    for pedido, cajas in salidas:
        if cajas:
            for caja in cajas:
                guia = caja.guia_activa
                lineas.append(LineaManifiesto(
                    pedido=pedido, paquete=caja, guia=guia,
                    numero_guia=guia.numero if guia else "", caja=caja.numero,
                ))
            continue
        guias = [g for g in pedido.guias.all() if g.es_activa]
        if not guias:
            lineas.append(LineaManifiesto(pedido=pedido))
        for guia in guias:
            lineas.append(LineaManifiesto(
                pedido=pedido, paquete=guia.paquete, guia=guia, numero_guia=guia.numero,
                caja=guia.paquete.numero if guia.paquete_id else None,
            ))
    if not lineas:
        return None
    for linea in lineas:
        linea.sin_escaneo = linea.pedido_id in ya_pedidos or (linea.paquete_id in ya_cajas)
    with transaction.atomic():
        manifiesto = Manifiesto.objects.create(
            carrier=carrier, corral=corral or "",
            operador=operador if hasattr(operador, "pk") else None,
            chofer=(chofer or "")[:120],
        )
        for linea in lineas:
            linea.manifiesto = manifiesto
        LineaManifiesto.objects.bulk_create(lineas)
    registrar_evento(
        "manifiesto", manifiesto.folio, "manifiesto_creado", actor=operador,
        delta={"carrier": carrier, "corral": corral, "cajas": len(lineas),
               "sin_escaneo": sum(1 for l in lineas if l.sin_escaneo),
               "pedidos": sorted({p.folio for p, _ in salidas})},
        motivo=f"Manifiesto {manifiesto.folio} de {carrier}: {len(lineas)} caja(s) al camión.",
    )
    return manifiesto


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

    _guardar_eventos(guia, info, estado_nuevo if cambio else "", hubo_movimiento, ahora)

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


def _estado_por_guias(pedido):
    """Estado que le toca al pedido según TODAS sus guías: "ENTREGADO" solo
    cuando cada guía activa está entregada; "RETORNADO" cuando ninguna sigue
    activa (todas regresaron); None mientras haya cajas en juego. Un pedido
    de una sola guía se comporta como siempre. Fulfillment parcial: mientras
    el pedido espere inventario o esté en su segunda ola
    (Pedido.pendiente_de_completar) no se entrega — se entregan todos los
    line items o nada (Chema 2026-09-22)."""
    guias = list(pedido.guias.all())
    activas = [g for g in guias if g.es_activa]
    if guias and not activas:
        return "RETORNADO"
    if activas and all(g.estado == Guia.ENTREGADO for g in activas):
        return None if pedido.pendiente_de_completar else "ENTREGADO"
    return None


def _guardar_eventos(guia, info, estado_nuevo, hubo_movimiento, ahora):
    """EventoGuia con lo nuevo del rastreo: el historial completo del carrier
    (`info["eventos"]`, con su hora) deduplicado contra lo ya guardado; sin
    historial, un evento por movimiento visto (hora del carrier si la mandó)."""
    vistos = {
        (e.ts_carrier, e.crudo, e.descripcion)
        for e in EventoGuia.objects.filter(guia=guia).only("ts_carrier", "crudo", "descripcion")
    }
    nuevos = []
    historial = info.get("eventos") or []
    if historial:
        for e in historial:
            clave = (e.get("ts"), (e.get("crudo") or "")[:80], (e.get("descripcion") or "")[:300])
            if clave in vistos:
                continue
            vistos.add(clave)
            nuevos.append(EventoGuia(
                guia=guia, estado=e.get("estado") or "", crudo=clave[1], descripcion=clave[2],
                ts_carrier=e.get("ts"), raw=e.get("raw") or {},
            ))
    elif hubo_movimiento:
        descripcion = (info.get("descripcion") or "")[:300]
        crudo = str(info.get("estado") or "")[:80]
        clave = (info.get("ts_evento"), crudo, descripcion)
        if clave not in vistos:
            nuevos.append(EventoGuia(
                guia=guia, estado=estado_nuevo or guia.estado, crudo=crudo, descripcion=descripcion,
                ts_carrier=info.get("ts_evento"), raw=info.get("raw") or {},
            ))
    if nuevos:
        EventoGuia.objects.bulk_create(nuevos)
    return len(nuevos)


def _aplicar_efectos(guia, estado, descripcion):
    """Efectos del cambio de estado de la guía sobre el pedido/incidencias.

    El pedido se mueve por el conjunto de sus guías (una caja entregada no
    entrega el pedido) y jamás mientras queden cajas en bodega
    (PARCIALMENTE_DESPACHADO): ahí manda el manifiesto de las que faltan."""
    pedido = guia.pedido
    abiertas = 0
    # Cajas de esta ola en bodega, o una segunda ola en curso (fulfillment
    # parcial: algo ya salió y algo sigue adentro): el tracking de lo que
    # salió no mueve el pedido; mandan el manifiesto y la máquina del piso.
    en_bodega = pedido.estado == "PARCIALMENTE_DESPACHADO" or (
        pedido.tiene_despachadas and bool(pedido.lineas_por_surtir)
    )
    if estado in {Guia.EN_TRANSITO, Guia.EN_RUTA}:
        if not en_bodega:
            _transicionar_pedido(pedido, "EN_TRANSITO", motivo=descripcion)
    elif estado == Guia.ENTREGADO:
        if _estado_por_guias(pedido) == "ENTREGADO":
            _transicionar_pedido(pedido, "ENTREGADO", motivo=descripcion)
        elif not en_bodega:
            caja = f"caja {guia.paquete.numero}" if guia.paquete_id else f"guía {guia.numero}"
            _transicionar_pedido(
                pedido, "EN_TRANSITO",
                motivo=f"{caja} entregada; faltan otras cajas del pedido. {descripcion}"[:300],
            )
    elif estado == Guia.INTENTO_FALLIDO:
        texto = (
            f"Intento de entrega fallido en la guía {guia.numero} ({guia.carrier}). "
            f"Último evento del carrier: {descripcion or 'sin detalle'}."
        )
        if _abrir_incidencia(pedido, "RF", texto, prioridad="P1"):
            abiertas += 1
    elif estado == Guia.RETORNO:
        if _estado_por_guias(pedido) == "RETORNADO":
            _transicionar_pedido(pedido, "RETORNADO", motivo=descripcion)
        texto = (
            f"El carrier marcó retorno al remitente en la guía {guia.numero} ({guia.carrier}). "
            f"Último evento: {descripcion or 'sin detalle'}. Requiere reingreso y reexpedición."
        )
        if _abrir_incidencia(pedido, "RF", texto):
            abiertas += 1
    # RECOLECTADO del carrier NO mueve el pedido: el manifiesto es autoritativo.
    _evento_fulfillment_shopify(guia, estado, descripcion)
    return abiertas


def _evento_fulfillment_shopify(guia, estado, descripcion):
    """El avance de la guía también se escribe en Shopify como FulfillmentEvent
    ("Delivery status" del admin): lazy por contrato y best-effort — Shopify
    caído no detiene el rastreo."""
    try:
        from apps.integraciones.services import registrar_evento_fulfillment  # lazy
        registrar_evento_fulfillment(
            guia.pedido, guia, estado, descripcion=descripcion, ts=guia.ts_ultimo_movimiento,
        )
    except Exception:  # noqa: BLE001, S110 — best-effort
        pass


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


def _avisar_sin_paqueteria(pedido, detalle):
    """Incidencia interna "Sin paquetería que cotice" (lazy por contrato; None
    si el módulo no existe)."""
    try:
        from apps.incidencias.services import abrir_sin_paqueteria
        return abrir_sin_paqueteria(pedido, detalle)
    except Exception:  # noqa: BLE001 — la incidencia es aviso; el error de guía se levanta igual
        registrar_evento("pedido", pedido.pk, "sin_paqueteria", cliente=pedido.cliente, motivo=detalle[:300])
        return None


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
