from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


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
