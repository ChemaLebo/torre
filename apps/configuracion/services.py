"""Servicios de configuración de sistema (contrato):
- correos_incidencias(cliente) -> [correo]: la lista fija (sin cliente) más la
  del cliente, activos, sin repetidos.
- agregar_correo_incidencias(cliente, correo, nombre, actor) / quitar_correo_incidencias(fila, actor):
  la lista del cliente desde su ficha de Mesa (la fija solo se toca en el admin).
"""
from django.core.validators import validate_email
from django.db.models import Q

from apps.core.services import registrar_evento

from .models import CorreoIncidencias


def correos_incidencias(cliente):
    """Destinatarios del aviso de una incidencia del cliente: los correos sin
    cliente (reciben todo) y los de ese cliente, activos, sin duplicados."""
    filtro = Q(cliente__isnull=True)
    if cliente is not None:
        filtro |= Q(cliente=cliente)
    vistos, correos = set(), []
    for fila in CorreoIncidencias.objects.filter(activo=True).filter(filtro).order_by("cliente_id", "correo"):
        clave = fila.correo.strip().lower()
        if clave and clave not in vistos:
            vistos.add(clave)
            correos.append(fila.correo.strip())
    return correos


def agregar_correo_incidencias(cliente, correo, nombre="", actor=None):
    """Alta de un correo en la lista del cliente (ficha de Mesa). Valida el
    formato y que no esté repetido en esa lista; reactiva uno inactivo."""
    correo = (correo or "").strip()
    try:
        validate_email(correo)
    except Exception:
        raise ValueError("Captura un correo válido (nombre@dominio).") from None
    fila = CorreoIncidencias.objects.filter(cliente=cliente, correo__iexact=correo).first()
    if fila is not None and fila.activo:
        raise ValueError(f"{correo} ya está en la lista de {cliente.nombre}.")
    if fila is not None:
        fila.activo = True
        fila.nombre = (nombre or "").strip()[:120] or fila.nombre
        fila.save(update_fields=["activo", "nombre"])
    else:
        fila = CorreoIncidencias.objects.create(cliente=cliente, correo=correo, nombre=(nombre or "").strip()[:120])
    registrar_evento(
        "cliente", cliente.slug, "correo_incidencias_alta", actor=actor, cliente=cliente,
        delta={"correo": correo}, motivo="Correo agregado a la lista de incidencias del cliente desde Mesa.",
    )
    return fila


def quitar_correo_incidencias(fila, actor=None):
    """Baja (borra el renglón) de un correo de la lista de un cliente."""
    cliente = fila.cliente
    registrar_evento(
        "cliente", cliente.slug, "correo_incidencias_baja", actor=actor, cliente=cliente,
        delta={"correo": fila.correo}, motivo="Correo quitado de la lista de incidencias del cliente desde Mesa.",
    )
    fila.delete()
