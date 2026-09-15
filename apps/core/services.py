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


def enviar_acceso(usuario, *, cliente=None, actor=None, motivo=""):
    """Manda al correo del usuario el enlace para definir su contraseña. Sin
    correo real no manda nada y regresa False. Queda en auditoría (sin el
    token). Un fallo del servidor de correo se propaga: quien lo llama decide
    cómo avisarlo."""
    from django.conf import settings
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    if not email_real(usuario):
        return False
    contexto = {
        "usuario": usuario, "url": url_acceso(usuario), "cliente": cliente,
        "horas": settings.PASSWORD_RESET_TIMEOUT // 3600,
    }
    send_mail(
        subject="Tu acceso a Torre",
        message=render_to_string("core/correo_acceso.txt", contexto),
        from_email=None,
        recipient_list=[usuario.email],
    )
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
