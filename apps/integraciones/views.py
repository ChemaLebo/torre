"""Endpoint de webhooks de Shopify.

Contrato: POST hooks/shopify/<tienda_id>/ — valida HMAC si la tienda tiene token,
responde 200 rápido, guarda WebhookEvento (idempotencia por webhook_id) y en dev
procesa síncrono. Un reintento de Shopify con el mismo webhook_id es no-op.
"""
import base64
import hashlib
import hmac
import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Tienda
from .services import procesar_webhook, registrar_webhook


def _firma_valida(token, cuerpo, firma_recibida):
    """HMAC-SHA256 del cuerpo crudo, base64, comparación en tiempo constante."""
    esperada = base64.b64encode(
        hmac.new(token.encode("utf-8"), cuerpo, hashlib.sha256).digest()
    ).decode("ascii")
    try:
        return hmac.compare_digest(esperada, firma_recibida or "")
    except TypeError:  # header con caracteres no-ASCII: firma inválida, no 500
        return False


@csrf_exempt
@require_POST
def webhook_shopify(request, tienda_id):
    """Recibe webhooks de Shopify para una tienda registrada."""
    tienda = get_object_or_404(Tienda, pk=tienda_id, activo=True)
    cuerpo = request.body

    # HMAC solo si la tienda tiene secreto configurado (dev sin token = abierto).
    if tienda.token and not _firma_valida(
        tienda.token, cuerpo, request.headers.get("X-Shopify-Hmac-Sha256")
    ):
        return JsonResponse({"ok": False, "error": "Firma HMAC inválida."}, status=401)

    try:
        payload = json.loads(cuerpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "El cuerpo no es JSON válido."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Se espera un objeto JSON."}, status=400)

    topic = request.headers.get("X-Shopify-Topic", "orders/create")
    # Sin header (pruebas manuales): id determinista por contenido → reenviar
    # el mismo cuerpo sigue siendo idempotente.
    webhook_id = request.headers.get("X-Shopify-Webhook-Id") or hashlib.sha256(
        topic.encode("utf-8") + b":" + cuerpo
    ).hexdigest()

    evento, creado = registrar_webhook(tienda, webhook_id, topic, payload)
    if not creado:
        # Ya lo teníamos: 200 para que Shopify no reintente; cero efectos nuevos.
        return JsonResponse({"ok": True, "duplicado": True})

    # Síncrono en dev; los errores quedan en SyncLog y el evento queda para replay.
    procesar_webhook(evento)
    return JsonResponse({"ok": True, "webhook_id": webhook_id})
