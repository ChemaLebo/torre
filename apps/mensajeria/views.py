"""Endpoints Web Push de la PWA: alta y baja de suscripciones (los llama push.js)."""
import json

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from . import push

# Solo los roles operativos reciben push (piso y mesa). El portal del cliente
# jamás registra suscripciones: menos superficie, cero push a terceros.
ROLES_PUSH = {"piso", "mesa"}


def _quiere_json(request):
    return "application/json" in (request.META.get("HTTP_ACCEPT") or "")


def _autorizar(request):
    """None si el usuario puede tocar push; respuesta de error si no.

    Anónimo con Accept: application/json → 401 JSON (push.js distingue sesión
    expirada de un error real en vez de celebrar el 200 de la página de login);
    anónimo clásico → redirect al login. Rol no operativo → 403 JSON.
    """
    if not request.user.is_authenticated:
        if _quiere_json(request):
            return JsonResponse(
                {"ok": False, "error": "Sesión expirada: vuelve a entrar."}, status=401,
            )
        return redirect_to_login(request.get_full_path())
    if not (request.user.is_superuser or getattr(request, "rol", None) in ROLES_PUSH):
        return JsonResponse(
            {"ok": False, "error": "Las notificaciones push son solo para piso y mesa."},
            status=403,
        )
    return None


@require_POST
def suscribir_push(request):
    """Guarda (o refresca) la suscripción push del usuario autenticado.

    Recibe el JSON estándar de PushSubscription.toJSON(). Restringido a los
    roles piso/mesa. Payload incompleto o endpoint fuera de la lista blanca
    de push services → 400 con el motivo.
    """
    negado = _autorizar(request)
    if negado is not None:
        return negado
    try:
        datos = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(
            {"ok": False, "error": "El cuerpo debe ser JSON de PushSubscription."},
            status=400,
        )
    try:
        push.suscribir(
            request.user, datos,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@require_POST
def baja_push(request):
    """Poda la suscripción del usuario (botón "Desactivar notificaciones").

    Body JSON opcional {"endpoint": ...} para bajar solo ese dispositivo;
    sin endpoint se podan todas las del usuario.
    """
    negado = _autorizar(request)
    if negado is not None:
        return negado
    endpoint = None
    if request.body:
        try:
            endpoint = (json.loads(request.body.decode("utf-8")) or {}).get("endpoint")
        except (ValueError, UnicodeDecodeError):
            endpoint = None
    podadas = push.desactivar(request.user, endpoint=endpoint)
    return JsonResponse({"ok": True, "podadas": podadas})
