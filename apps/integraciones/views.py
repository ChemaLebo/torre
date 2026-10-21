"""Endpoint de webhooks de Shopify.

Contrato: POST hooks/shopify/<tienda_id>/ — valida HMAC si la tienda tiene token,
responde 200 rápido, guarda WebhookEvento (idempotencia por webhook_id) y en dev
procesa síncrono. Un reintento de Shopify con el mismo webhook_id es no-op.
"""
import base64
import hashlib
import hmac
import json

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.services import registrar_evento

from .models import Tienda
from .services import procesar_webhook, registrar_webhook
from .shopify import ShopifyError


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

    # La firma se valida contra webhook_secret — NUNCA contra el access token:
    # Shopify firma con el signing secret de Notificaciones (webhooks creados en
    # el admin) o con el API secret key de la app (creados por API); el shpat_
    # jamás firma nada. Fail-closed en producción: sin secreto NO se aceptan
    # webhooks (endpoint público, tienda_id secuencial). Vacío solo en dev/demo.
    if not tienda.webhook_secret:
        if not settings.DEBUG:
            return JsonResponse(
                {"ok": False, "error": "Tienda sin webhook secret configurado."}, status=403,
            )
    elif not _firma_valida(
        tienda.webhook_secret, cuerpo, request.headers.get("X-Shopify-Hmac-Sha256")
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


# ── OAuth: Conectar con Shopify desde Torre (Chema 2026-10-21) ───────────────

def _es_mesa(request):
    return request.user.is_authenticated and (getattr(request, "rol", None) == "mesa" or request.user.is_superuser)


def _resumen_conexion(tienda, resumen):
    partes = [f"{tienda.dominio} conectada."]
    if resumen.get("error"):
        partes.append(f"Pasos siguientes no corrieron: {resumen['error']}")
        return " ".join(partes)
    partes.append(f"Webhooks: {resumen.get('webhooks')}.")
    partes.append(f"Metafield: {resumen.get('metafield')}.")
    if resumen.get("location_elegida"):
        partes.append(f"Location: {resumen['location_elegida']}.")
    elif not tienda.location_id:
        partes.append("Elige la location de WOP en editar.")
    if resumen.get("permisos_faltantes"):
        partes.append("Faltan permisos: " + ", ".join(resumen["permisos_faltantes"]) + ".")
    return " ".join(partes)


def oauth_callback(request):
    """GET /oauth/callback: regreso de Shopify tras aprobar los permisos. La
    tienda se identifica por `shop`; se valida la firma con su client_secret y
    el `state` que Torre generó al iniciar; el código se cambia por el token y
    corren los pasos post-conexión. Con sesión de Mesa regresa a la ficha del
    cliente; sin sesión (el cliente desde su admin) muestra una página simple."""
    from . import oauth  # lazy por contrato

    params = {clave: valor for clave, valor in request.GET.items()}
    shop = (params.get("shop") or "").strip().lower()
    tienda = Tienda.objects.select_related("cliente").filter(dominio=shop).first() if shop else None
    if tienda is None or not tienda.client_secret:
        return HttpResponseNotFound("Tienda desconocida o sin app configurada en Torre.")
    if not oauth.firma_valida(params, tienda.client_secret):
        return HttpResponseForbidden("Firma inválida: la respuesta no viene de Shopify o el client secret no es el de esta app.")
    if not tienda.oauth_state or params.get("state") != tienda.oauth_state:
        return HttpResponseForbidden("Conexión no iniciada desde Torre o ya usada: vuelve a Conectar con Shopify.")
    code = params.get("code") or ""
    if not code:
        return HttpResponseForbidden("Shopify no mandó el código de autorización.")
    actor = request.user if request.user.is_authenticated else None
    try:
        oauth.conectar_tienda(tienda, code, actor=actor)
    except ShopifyError as exc:
        registrar_evento(
            "tienda", tienda.dominio, "conexion_fallida", actor=actor, cliente=tienda.cliente,
            delta={"error": str(exc)[:300]}, motivo="Shopify no cambió el código por un token.",
        )
        if _es_mesa(request):
            messages.error(request, f"{tienda.dominio}: {exc}")
            return redirect(f"{reverse('mesa:cliente_detalle', args=[tienda.cliente_id])}?tienda={tienda.pk}")
        return render(request, "integraciones/conectada.html", {"tienda": tienda, "error": str(exc)}, status=502)
    resumen = oauth.pasos_post_conexion(tienda, actor=actor)
    if _es_mesa(request):
        messages.success(request, _resumen_conexion(tienda, resumen))
        return redirect(f"{reverse('mesa:cliente_detalle', args=[tienda.cliente_id])}?tienda={tienda.pk}")
    return render(request, "integraciones/conectada.html", {"tienda": tienda, "resumen": resumen})


def oauth_conectar_por_link(request, firma):
    """GET /oauth/conectar/<firma>/: el link que Mesa le manda al cliente; abre
    la pantalla de permisos de SU tienda sin entrar a Mesa. Vale 7 días."""
    from . import oauth  # lazy por contrato

    tienda = oauth.tienda_del_link(firma)
    if tienda is None:
        return HttpResponseNotFound("Este link de conexión no es válido o ya caducó: pide uno nuevo a Torre.")
    try:
        return redirect(oauth.iniciar_conexion(tienda))
    except ValueError as exc:
        return render(request, "integraciones/conectada.html", {"tienda": tienda, "error": str(exc)}, status=400)
