"""Servicios transversales de Torre."""
from .models import EventoAuditoria


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
    return EventoAuditoria.objects.create(
        actor_tipo=actor_tipo,
        actor_id=actor_id,
        cliente=cliente,
        entidad=entidad,
        entidad_id=str(entidad_id),
        accion=accion,
        delta=delta or {},
        motivo=motivo,
    )
