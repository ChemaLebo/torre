"""Servicios transversales de Torre."""
from .models import EventoAuditoria


EMAIL_PLACEHOLDER_DOMINIO = "torre380e.mx"


def email_real(usuario):
    """True si el correo del usuario es de verdad (no el relleno usuario@torre380e.mx
    que Mesa pone cuando el alta viene sin correo)."""
    correo = (usuario.email or "").strip()
    return bool(correo) and not correo.lower().endswith("@" + EMAIL_PLACEHOLDER_DOMINIO)


def url_acceso(usuario):
    """URL absoluta de un solo uso para que el usuario defina su contraseña
    (token de restablecimiento de Django, vale PASSWORD_RESET_TIMEOUT)."""
    import os

    from django.contrib.auth.tokens import default_token_generator
    from django.urls import reverse
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    base = os.environ.get("BASE_URL_PUBLICA", "http://127.0.0.1:8380").rstrip("/")
    uid = urlsafe_base64_encode(force_bytes(usuario.pk))
    token = default_token_generator.make_token(usuario)
    return base + reverse("core:restablecer", kwargs={"uidb64": uid, "token": token})


BRANDING_CORREO_DEFAULT = {"nombre_publico": "Torre", "color_primario": "#1C1D1D", "logo_url": ""}


def branding_correo(cliente=None):
    """Colores, logo y nombre para los correos: los del cliente (Cliente.branding,
    los mismos del rastreo) o los de Torre si no hay cliente o no los capturó."""
    marca = dict(BRANDING_CORREO_DEFAULT)
    if cliente is not None:
        for clave in marca:
            valor = (cliente.branding or {}).get(clave)
            if valor:
                marca[clave] = valor
        if not (cliente.branding or {}).get("nombre_publico"):
            marca["nombre_publico"] = cliente.nombre
    return marca


def enviar_correo(destinatario, asunto, plantilla, contexto):
    """Correo multipart: texto plano desde `<plantilla>.txt` y HTML brandeado
    desde `<plantilla>.html`, con Reply-To de settings.EMAIL_REPLY_TO."""
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    correo = EmailMultiAlternatives(
        subject=asunto,
        body=render_to_string(f"{plantilla}.txt", contexto),
        to=[destinatario],
        reply_to=[settings.EMAIL_REPLY_TO] if settings.EMAIL_REPLY_TO else None,
    )
    correo.attach_alternative(render_to_string(f"{plantilla}.html", contexto), "text/html")
    correo.send()


def enviar_acceso(usuario, *, cliente=None, actor=None, motivo=""):
    """Manda al correo del usuario el enlace para definir su contraseña. Sin
    correo real no manda nada y regresa False. Queda en auditoría (sin el
    token). Un fallo del servidor de correo se propaga: quien lo llama decide
    cómo avisarlo."""
    from django.conf import settings

    if not email_real(usuario):
        return False
    contexto = {
        "usuario": usuario, "url": url_acceso(usuario), "cliente": cliente,
        "horas": settings.PASSWORD_RESET_TIMEOUT // 3600, "marca": branding_correo(cliente),
    }
    enviar_correo(usuario.email, "Tu acceso a Torre", "core/correo_acceso", contexto)
    registrar_evento(
        "usuario", usuario.username, "acceso_enviado", actor=actor, cliente=cliente,
        motivo=motivo or f"Enlace de acceso enviado a {usuario.email}",
    )
    return True


def registrar_evento(entidad, entidad_id, accion, *, actor=None, cliente=None, delta=None, motivo=""):
    """Único punto de escritura del event log. Todo movimiento pasa por aquí.

    actor: User, string, o None (sistema).
    """
    if actor is None:
        actor_tipo, actor_id = "sistema", ""
    elif hasattr(actor, "username"):
        actor_tipo, actor_id = "usuario", actor.username
    else:
        actor_tipo, actor_id = "webhook", str(actor)
    # Truncado defensivo al límite del campo: Postgres SÍ valida max_length
    # (SQLite no) y algunos ids son derivados (sha256 de webhook = 64 chars,
    # claves de idempotencia con teléfono). Un evento truncado > un DataError
    # que tira la operación que lo registraba.
    campo_max = EventoAuditoria._meta.get_field("entidad_id").max_length
    return EventoAuditoria.objects.create(
        actor_tipo=actor_tipo,
        actor_id=actor_id,
        cliente=cliente,
        entidad=entidad,
        entidad_id=str(entidad_id)[:campo_max],
        accion=accion,
        delta=delta or {},
        motivo=motivo,
    )
