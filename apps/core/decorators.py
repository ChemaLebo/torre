from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def redirigir_rol(**destinos):
    """Un link compartido entre Mesa y el portal (Chema 2026-09-30): quien
    entra con el rol equivocado a un pedido o una incidencia se redirige a la
    MISMA entidad en su propia pantalla, en vez de un 403. `destinos` =
    {rol: nombre de URL}; los argumentos de la vista (pk) se reusan. Va
    ENCIMA del decorador de rol, que sigue mandando para todo lo demás. Solo
    redirige un GET: una acción (POST) con el rol equivocado sigue siendo
    403, jamás se traslada. El superuser pasa a todas partes y no se
    redirige."""

    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            destino = destinos.get(getattr(request, "rol", None))
            if destino and request.method == "GET" and not request.user.is_superuser:
                return redirect(destino, *args, **kwargs)
            return view(request, *args, **kwargs)
        return wrapped

    return deco


def rol_requerido(*roles):
    """Restringe una vista a los roles dados. Superuser siempre pasa."""

    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if request.user.is_superuser or request.rol in roles:
                return view(request, *args, **kwargs)
            raise PermissionDenied
        return wrapped

    return deco


def portal_requerido(view):
    """Vista de portal: exige rol portal Y tenant asignado."""

    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if request.rol == "portal" and request.cliente is not None:
            return view(request, *args, **kwargs)
        if request.user.is_superuser and request.cliente is not None:
            return view(request, *args, **kwargs)
        raise PermissionDenied
    return wrapped
