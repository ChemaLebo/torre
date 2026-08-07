"""Web Push con VAPID self-hosted — las notificaciones de la PWA de Torre.

Los operadores instalan Torre en su Android ("Agregar a pantalla de inicio")
y les llegan push con la app cerrada: pedido nuevo y recepción anunciada al
piso; incidencia nueva y sugerencia de cliente a la Mesa.

Reglas duras:
- Best-effort SIEMPRE: enviar jamás levanta excepción hacia el flujo de
  negocio que disparó el aviso.
- Sin VAPID configurado (las 3 variables + el .pem) → no-op que regresa 0.
- Suscripción muerta (404/410 del push service) → se poda (activo=False).
- El endpoint de la suscripción SOLO puede ser un push service conocido
  (https + lista blanca de hosts): el servidor jamás hace POST a una URL
  arbitraria que mandó el navegador (anti-SSRF), y el envío va sin
  redirects por la misma razón.
- Cada webpush() sale con timeout corto y el lote completo tiene tope de
  tiempo: un push service colgado jamás detiene a Torre.
"""
import ipaddress
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction

from pywebpush import WebPushException, webpush

from .models import SuscripcionPush

# ── Límites y lista blanca ──
TIMEOUT_ENVIO_S = 5          # por webpush(): pywebpush pasa timeout=None si no se da
TOPE_LOTE_S = 10             # tope de tiempo del lote completo de envíos
MAX_SUSCRIPCIONES_ACTIVAS = 5  # por usuario; la más vieja se poda
MAX_ENDPOINT = 500
MAX_P256DH = 200
MAX_AUTH = 100

# Push services reales de los navegadores que Torre soporta. Nada más.
HOSTS_PUSH = {"fcm.googleapis.com", "web.push.apple.com"}
SUFIJOS_PUSH = (".push.services.mozilla.com", ".notify.windows.com")


class _SesionSinRedirects(requests.Session):
    """El push service jamás redirige un POST legítimo: sin redirects no hay
    SSRF por rebote a un host fuera de la lista blanca."""

    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


_sesion_push = _SesionSinRedirects()


def _ruta_pem():
    """Ruta absoluta al .pem de la llave privada VAPID (relativa a BASE_DIR)."""
    ruta = Path(settings.VAPID_PRIVATE_PEM or "")
    if not ruta.is_absolute():
        ruta = Path(settings.BASE_DIR) / ruta
    return ruta


def vapid_configurado():
    """True si las 3 variables VAPID están presentes y el .pem existe en disco."""
    completas = bool(
        settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_PEM and settings.VAPID_CLAIM_EMAIL
    )
    return completas and _ruta_pem().exists()


def endpoint_permitido(endpoint):
    """True solo para https hacia un push service de la lista blanca.

    IPs literales (privadas o no) y hosts desconocidos se rechazan: el
    endpoint lo manda el navegador del usuario y Torre le hace POST — sin
    esta guarda cualquier sesión del piso convierte al servidor en proxy
    (SSRF a metadata, red interna, etc.).
    """
    try:
        partes = urlparse(str(endpoint))
    except ValueError:
        return False
    if partes.scheme != "https" or not partes.hostname:
        return False
    host = partes.hostname.lower()
    try:
        ipaddress.ip_address(host)
        return False  # IP literal: jamás
    except ValueError:
        pass
    if host in HOSTS_PUSH:
        return True
    return any(host.endswith(s) or host == s.lstrip(".") for s in SUFIJOS_PUSH)


def suscribir(usuario, datos_subscription, user_agent=""):
    """Alta/refresco idempotente por endpoint (update_or_create).

    `datos_subscription` = el JSON estándar de PushSubscription.toJSON():
    {"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}.

    - Valida longitudes y que el endpoint sea un push service conocido.
    - Si el endpoint ya era de OTRO usuario (celular compartido), se reasigna
      EXPLÍCITAMENTE con evento de auditoría (rotación de dispositivo) y se
      podan las suscripciones del usuario anterior en ese mismo dispositivo.
    - Máximo MAX_SUSCRIPCIONES_ACTIVAS activas por usuario: la más vieja se poda.
    """
    datos = datos_subscription or {}
    endpoint = str(datos.get("endpoint") or "").strip()
    llaves = datos.get("keys") or {}
    p256dh = str(llaves.get("p256dh") or "").strip()
    auth = str(llaves.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise ValueError("La suscripción push viene incompleta: faltan endpoint o llaves.")
    if len(endpoint) > MAX_ENDPOINT:
        raise ValueError(f"El endpoint de la suscripción excede {MAX_ENDPOINT} caracteres.")
    if len(p256dh) > MAX_P256DH:
        raise ValueError(f"La llave p256dh excede {MAX_P256DH} caracteres.")
    if len(auth) > MAX_AUTH:
        raise ValueError(f"La llave auth excede {MAX_AUTH} caracteres.")
    if not endpoint_permitido(endpoint):
        raise ValueError(
            "El endpoint de la suscripción no es de un push service conocido; "
            "no se guarda."
        )
    user_agent = str(user_agent or "")[:200]
    with transaction.atomic():
        previa = (
            SuscripcionPush.objects.select_for_update()
            .filter(endpoint=endpoint)
            .select_related("usuario")
            .first()
        )
        if previa is not None and previa.usuario_id != usuario.pk:
            # Rotación de dispositivo legítima (celular compartido del piso):
            # se reasigna con rastro auditable, jamás en silencio.
            from apps.core.services import registrar_evento  # lazy por contrato
            registrar_evento(
                "suscripcion_push", previa.pk, "rotacion_dispositivo", actor=usuario,
                delta={
                    "usuario_anterior": previa.usuario.username,
                    "usuario_nuevo": getattr(usuario, "username", str(usuario)),
                },
                motivo="El dispositivo re-suscribió push con otra sesión; la fila se reasigna.",
            )
            # Variantes del usuario anterior en ese MISMO dispositivo: se podan
            # para que no le sigan llegando avisos de una sesión que ya no es suya.
            variantes = SuscripcionPush.objects.filter(
                usuario=previa.usuario, activo=True,
            ).exclude(pk=previa.pk)
            if user_agent:
                variantes = variantes.filter(user_agent=user_agent)
            for variante in variantes:
                variante.activo = False
                variante.save(update_fields=["activo"])
        suscripcion, _ = SuscripcionPush.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "usuario": usuario,
                "p256dh": p256dh,
                "auth": auth,
                "user_agent": user_agent,
                "activo": True,
            },
        )
        # Tope por usuario: nadie acumula suscripciones sin fin.
        activas = list(
            SuscripcionPush.objects.filter(usuario=usuario, activo=True)
            .order_by("-creado", "-pk")
        )
        for vieja in activas[MAX_SUSCRIPCIONES_ACTIVAS:]:
            vieja.activo = False
            vieja.save(update_fields=["activo"])
    return suscripcion


def desactivar(usuario, endpoint=None):
    """Poda (activo=False) las suscripciones del usuario.

    Con `endpoint` solo esa fila (botón "Desactivar notificaciones");
    sin él, todas (logout). Regresa cuántas se podaron.
    """
    filas = SuscripcionPush.objects.filter(usuario=usuario, activo=True)
    if endpoint:
        filas = filas.filter(endpoint=str(endpoint).strip())
    podadas = 0
    for fila in filas:
        fila.activo = False
        fila.save(update_fields=["activo"])
        podadas += 1
    return podadas


def _enviar_a_suscripciones(suscripciones, titulo, cuerpo, url):
    """Itera y envía. Poda 404/410; cualquier otro error se ignora y se sigue.

    Cada webpush sale con timeout corto y el lote completo respeta TOPE_LOTE_S:
    un push service colgado jamás cuelga el proceso que disparó el aviso.
    """
    payload = json.dumps({"title": titulo, "body": cuerpo, "url": url})
    ruta_pem = str(_ruta_pem())
    correo = settings.VAPID_CLAIM_EMAIL
    enviadas = 0
    inicio = time.monotonic()
    for suscripcion in suscripciones:
        if time.monotonic() - inicio > TOPE_LOTE_S:
            break  # tope del lote: lo que no salió, no salió — best-effort
        try:
            webpush(
                subscription_info={
                    "endpoint": suscripcion.endpoint,
                    "keys": {"p256dh": suscripcion.p256dh, "auth": suscripcion.auth},
                },
                data=payload,
                vapid_private_key=ruta_pem,
                # dict nuevo por envío: pywebpush le agrega aud/exp al vuelo.
                vapid_claims={"sub": f"mailto:{correo}"},
                timeout=TIMEOUT_ENVIO_S,
                requests_session=_sesion_push,
            )
        except WebPushException as exc:
            codigo = getattr(getattr(exc, "response", None), "status_code", None)
            if codigo in (404, 410):
                # El push service ya no conoce ese endpoint: suscripción muerta.
                suscripcion.activo = False
                suscripcion.save(update_fields=["activo"])
            continue
        except Exception:
            continue  # best-effort: un envío caído jamás tira a los demás
        enviadas += 1
    return enviadas


def enviar_push_a_usuario(usuario, titulo, cuerpo, url="/"):
    """Push a todas las suscripciones activas de UN usuario. Regresa cuántas salieron."""
    if not vapid_configurado():
        return 0
    return _enviar_a_suscripciones(
        SuscripcionPush.objects.filter(
            usuario=usuario, activo=True, usuario__is_active=True,
        ),
        titulo, cuerpo, url,
    )


def enviar_push_a_rol(rol, titulo, cuerpo, url="/"):
    """Push a las suscripciones activas de los usuarios con perfil de ese rol."""
    if not vapid_configurado():
        return 0
    return _enviar_a_suscripciones(
        SuscripcionPush.objects.filter(
            activo=True, usuario__perfil__rol=rol, usuario__is_active=True,
        ),
        titulo, cuerpo, url,
    )
