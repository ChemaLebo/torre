"""Webhooks de rastreo de los carriers (Chema 2026-09-30): el evento llega al
instante y el poller (`poll_tracking`) queda de respaldo.

- envia.com, tipo 3 "tracking.simple": POST hooks/carriers/envia/<token>/ con
  {"type", "created_at", "data": {"tracking_number", "carrier_name", "status",
  "status_description", "location", "shipment_id"}} y, si envia firma,
  X-Webhook-Signature "v1=<hmac-sha256 hex>" de "<X-Webhook-Timestamp>.<X-Webhook-Event>.<cuerpo>".
- 99minutos: POST hooks/carriers/99minutos/<token>/ con el shipment en
  PascalCase ({"TrackingId", "InternalKey", "StatusName", "Events": [{"StatusCode",
  "StatusName", "CreatedAt", "Data": {"comment"}}]}, "User-Agent: 99notifications");
  se leen las claves sin distinguir mayúsculas por si cambian a camelCase.

Seguridad: el token de la URL es obligatorio y se compara en tiempo constante;
sin token configurado el endpoint está cerrado (403). Ambos responden 200 con
{"ok": false, "motivo"} cuando la guía no es nuestra, para que el carrier no
reintente. Todo entra por `services.procesar_evento_carrier`, el mismo camino
que el poller: misma normalización, mismos efectos, misma deduplicación.
"""
import hashlib
import hmac
import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.services import registrar_evento

from .adapters import CODIGOS_ESTADO_99MIN, _parsear_fecha, normalizar_estado_envia
from .services import PROVEEDOR_99MIN, PROVEEDOR_ENVIA, procesar_evento_carrier


def _token_valido(configurado, recibido):
    return bool(configurado) and hmac.compare_digest(str(configurado), str(recibido or ""))


def _firma_envia_valida(request, cuerpo):
    """X-Webhook-Signature de envia (tipos firmados): "v1=<hex>" sobre
    "<timestamp>.<evento>.<cuerpo>" con ENVIA_WEBHOOK_SECRET. Sin secreto
    configurado no se exige firma (el token de la URL sigue mandando)."""
    secreto = settings.ENVIA_WEBHOOK_SECRET
    if not secreto:
        return True
    firma = request.headers.get("X-Webhook-Signature", "")
    ts = request.headers.get("X-Webhook-Timestamp", "")
    evento = request.headers.get("X-Webhook-Event", "")
    recibida = firma.split("v1=", 1)[1].strip() if "v1=" in firma else firma.strip()
    base = f"{ts}.{evento}.".encode() + cuerpo
    esperada = hmac.new(secreto.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, recibida)


def _json(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _rechazo(motivo, status):
    return JsonResponse({"ok": False, "error": motivo}, status=status)


def _ignorado(proveedor, motivo, payload):
    registrar_evento(
        "webhook_carrier", proveedor, "webhook_ignorado",
        delta={"motivo": motivo, "payload": _recortar(payload)}, motivo=motivo[:300],
    )
    return JsonResponse({"ok": False, "motivo": motivo})


def _recortar(payload):
    """Copia del payload acotada para la auditoría (sin eventos enormes)."""
    try:
        texto = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {}
    return payload if len(texto) <= 4000 else {"truncado": texto[:4000]}


def info_desde_envia(payload):
    """(numero, info) a partir del evento tracking.simple de envia; (None, None)
    si no trae número de guía."""
    datos = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    numero = str(datos.get("tracking_number") or datos.get("trackingNumber") or "").strip()
    if not numero:
        return None, None
    crudo = str(datos.get("status") or "")
    descripcion = str(datos.get("status_description") or datos.get("description") or crudo)[:300]
    lugar = str(datos.get("location") or "").strip()
    if lugar and lugar.lower() not in descripcion.lower():
        descripcion = f"{descripcion} · {lugar}"[:300]
    ts = _parsear_fecha(datos.get("date") or datos.get("movementDate") or payload.get("created_at"))
    estado = normalizar_estado_envia(crudo) or normalizar_estado_envia(descripcion)
    return numero, {
        "estado": estado, "descripcion": descripcion, "ts_evento": ts, "raw": datos,
        "eventos": [{"estado": estado or "", "crudo": crudo[:80], "descripcion": descripcion, "ts": ts, "raw": datos}],
    }


def _campo(diccionario, *nombres):
    """Primer valor presente entre `nombres`, sin distinguir mayúsculas
    (99minutos documenta PascalCase y su API responde camelCase)."""
    if not isinstance(diccionario, dict):
        return None
    por_clave = {str(k).lower(): v for k, v in diccionario.items()}
    for nombre in nombres:
        valor = por_clave.get(nombre.lower())
        if valor not in (None, ""):
            return valor
    return None


def _evento_99(e):
    try:
        codigo = int(_campo(e, "StatusCode"))
    except (TypeError, ValueError):
        codigo = None
    nombre = str(_campo(e, "StatusName") or "")
    data = _campo(e, "Data")
    comentario = str(_campo(data, "comment") or "") if isinstance(data, dict) else ""
    return {
        "estado": CODIGOS_ESTADO_99MIN.get(codigo, "") if codigo is not None else "",
        "crudo": str(_campo(e, "StatusCode") or nombre)[:80],
        "descripcion": (f"{nombre} · {comentario}" if comentario else nombre)[:300],
        "ts": _parsear_fecha(_campo(e, "CreatedAt", "created_at")), "raw": e,
    }


def info_desde_99minutos(payload):
    """(numero, info) a partir del shipment que manda 99minutos: TrackingId y
    Events[] con todo el historial (mismo contenido que /shipments/tracking)."""
    datos = _campo(payload, "data")
    if isinstance(datos, list):
        datos = datos[0] if datos else {}
    if not isinstance(datos, dict):
        datos = payload
    numero = str(_campo(datos, "TrackingId", "tracking_id", "counter") or "").strip()
    if not numero:
        return None, None
    eventos = _campo(datos, "Events") or []
    historial = [_evento_99(e) for e in eventos if isinstance(e, dict)]
    historial.sort(key=lambda h: (h["ts"] is None, h["ts"] or 0))
    if historial:
        ultimo = historial[-1]
        estado = ultimo["estado"] or normalizar_estado_envia(ultimo["descripcion"])
        return numero, {"estado": estado, "descripcion": ultimo["descripcion"], "ts_evento": ultimo["ts"],
                        "raw": datos, "eventos": historial}
    # Sin historial: el estado del shipment, como en Adapter99Minutos.rastrear.
    crudo = _campo(datos, "StatusCode", "status")
    try:
        codigo = int(crudo)
    except (TypeError, ValueError):
        codigo = None
    nombre = str(_campo(datos, "StatusName", "statusDescription") or crudo or "")
    estado = CODIGOS_ESTADO_99MIN.get(codigo) if codigo is not None else None
    if estado is None:
        estado = normalizar_estado_envia(nombre)
    ts = _parsear_fecha(_campo(datos, "UpdatedAt", "updated_at") or _campo(payload, "created_at"))
    return numero, {"estado": estado, "descripcion": nombre[:300], "ts_evento": ts, "raw": datos, "eventos": []}


def _responder(proveedor, numero, info, payload):
    resultado = procesar_evento_carrier(proveedor, numero, info)
    if not resultado["ok"]:
        return _ignorado(proveedor, resultado["motivo"], payload)
    return JsonResponse({"ok": True, "guia": numero, "estado": resultado["estado"],
                         "actualizada": resultado["actualizada"], "incidencias": resultado["incidencias"]})


@csrf_exempt
@require_POST
def webhook_envia(request, token):
    """Evento de rastreo de envia.com (tipo 3, tracking.simple)."""
    if not _token_valido(settings.ENVIA_WEBHOOK_TOKEN, token):
        return _rechazo("Webhook de envia cerrado o token inválido.", 403)
    cuerpo = request.body
    if not _firma_envia_valida(request, cuerpo):
        return _rechazo("Firma X-Webhook-Signature inválida.", 401)
    payload = _json(request)
    if payload is None:
        return _rechazo("El cuerpo no es un objeto JSON.", 400)
    tipo = str(payload.get("type") or request.headers.get("X-Webhook-Event") or "")
    if tipo and "tracking" not in tipo.lower() and "status" not in tipo.lower():
        return _ignorado(PROVEEDOR_ENVIA, f"tipo de evento {tipo} no es de rastreo", payload)
    numero, info = info_desde_envia(payload)
    if numero is None:
        return _ignorado(PROVEEDOR_ENVIA, "evento sin tracking_number", payload)
    return _responder(PROVEEDOR_ENVIA, numero, info, payload)


@csrf_exempt
@require_POST
def webhook_99minutos(request, token):
    """Evento de rastreo de 99minutos (shipment con events[])."""
    if not _token_valido(settings.NOVENTA9_WEBHOOK_TOKEN, token):
        return _rechazo("Webhook de 99minutos cerrado o token inválido.", 403)
    payload = _json(request)
    if payload is None:
        return _rechazo("El cuerpo no es un objeto JSON.", 400)
    numero, info = info_desde_99minutos(payload)
    if numero is None:
        return _ignorado(PROVEEDOR_99MIN, "evento sin trackingId", payload)
    return _responder(PROVEEDOR_99MIN, numero, info, payload)
