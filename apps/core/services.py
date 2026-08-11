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
