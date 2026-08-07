"""API JSON del relay de impresión — la consume deploy/agente_impresion.py.

Sin DRF: JSON plano sobre vistas función. Autenticación por token estático
(`TORRE_TOKEN_IMPRESION`) en `Authorization: Bearer <token>`, comparado en
tiempo constante con hmac.compare_digest; sin token configurado la API queda
cerrada (403 siempre). El PDF se sirve por aquí mismo con el mismo token:
MEDIA en dev no tiene auth y las etiquetas llevan datos del comprador.

Los POST van csrf_exempt: el agente no es un navegador, el token manda.
"""
import hmac
import json
from functools import wraps

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import TrabajoImpresion

# Cuántos trabajos regresa cada poll: suficiente para vaciar una ola de
# empaque en 2-3 vueltas sin respuestas gigantes.
LOTE_PENDIENTES = 10


def _token_valido(request):
    """True solo con token configurado Y header Bearer idéntico (constant-time)."""
    esperado = getattr(settings, "TORRE_TOKEN_IMPRESION", "")
    if not esperado:
        return False
    encabezado = request.headers.get("Authorization", "")
    esquema, _, recibido = encabezado.partition(" ")
    if esquema != "Bearer" or not recibido.strip():
        return False
    return hmac.compare_digest(recibido.strip(), esperado)


def _requiere_token(vista):
    """Decorator: sin token válido → 403 JSON, sin pistas de qué falló."""

    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not _token_valido(request):
            return JsonResponse({"error": "No autorizado."}, status=403)
        return vista(request, *args, **kwargs)

    return envoltura


@require_GET
@_requiere_token
def pendientes(request):
    """Trabajos PENDIENTES con reintentos disponibles, más viejos primero, máx 10."""
    maximo = settings.TORRE["IMPRESION_MAX_INTENTOS"]
    trabajos = (
        TrabajoImpresion.objects.filter(
            estado=TrabajoImpresion.PENDIENTE, intentos__lt=maximo
        )
        .select_related("guia__pedido")
        .order_by("creado")[:LOTE_PENDIENTES]
    )
    return JsonResponse(
        [
            {
                "id": trabajo.pk,
                "folio": trabajo.guia.pedido.folio,
                "guia": trabajo.guia.numero,
                "url_pdf": request.build_absolute_uri(
                    reverse("envios:impresion_pdf", args=[trabajo.pk])
                ),
            }
            for trabajo in trabajos
        ],
        safe=False,
    )


@csrf_exempt
@require_POST
@_requiere_token
def resultado(request, trabajo_id):
    """El agente reporta: {"ok": true} → IMPRESO; {"ok": false, "error": "..."} → reintento.

    Al agotar TORRE["IMPRESION_MAX_INTENTOS"] el trabajo pasa a ERROR (con
    evento de auditoría para que Mesa lo vea). Un trabajo ya cerrado responde
    su estado actual sin tocar nada: los reintentos del agente son inofensivos.
    """
    trabajo = get_object_or_404(TrabajoImpresion, pk=trabajo_id)
    try:
        datos = json.loads(request.body or b"")
    except json.JSONDecodeError:
        return JsonResponse({"error": "El cuerpo no es JSON válido."}, status=400)
    if not isinstance(datos, dict) or "ok" not in datos:
        return JsonResponse({"error": 'Falta el campo "ok" (true/false).'}, status=400)

    if trabajo.estado == TrabajoImpresion.PENDIENTE:
        if datos["ok"]:
            trabajo.transicionar(TrabajoImpresion.IMPRESO)
        else:
            trabajo.intentos += 1
            trabajo.error = str(datos.get("error") or "sin detalle del agente")[:300]
            trabajo.save(update_fields=["intentos", "error"])
            if trabajo.intentos >= settings.TORRE["IMPRESION_MAX_INTENTOS"]:
                trabajo.transicionar(TrabajoImpresion.ERROR, motivo=trabajo.error)
    return JsonResponse(
        {"id": trabajo.pk, "estado": trabajo.estado, "intentos": trabajo.intentos}
    )


@require_GET
@_requiere_token
def pdf(request, trabajo_id):
    """Sirve el PDF de la etiqueta autenticado por el mismo token del agente."""
    trabajo = get_object_or_404(TrabajoImpresion, pk=trabajo_id)
    try:
        archivo = trabajo.pdf.open("rb")
    except (FileNotFoundError, ValueError):
        raise Http404("El PDF de este trabajo ya no está disponible.")
    return FileResponse(
        archivo,
        content_type="application/pdf",
        filename=f"etiqueta-{trabajo.guia.pedido.folio}.pdf",
    )
