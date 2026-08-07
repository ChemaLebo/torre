from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect


class LoginView(auth_views.LoginView):
    template_name = "core/login.html"


class LogoutView(auth_views.LogoutView):
    """Logout que además poda (activo=False) las suscripciones push del
    usuario: un celular compartido del piso no sigue recibiendo avisos de
    una sesión que ya se cerró."""

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            try:
                from apps.mensajeria import push  # lazy por contrato
                push.desactivar(request.user)
            except Exception:
                pass  # el logout jamás falla por el push
        return super().post(request, *args, **kwargs)


@login_required
def post_login(request):
    """Enruta al home según el rol."""
    if request.rol == "portal":
        return redirect("portal:dashboard")
    if request.rol == "piso":
        return redirect("piso:home")
    if request.rol == "mesa" or request.user.is_superuser:
        return redirect("mesa:dashboard")
    return redirect("core:login")


def raiz(request):
    if request.user.is_authenticated:
        return post_login(request)
    return redirect("core:login")


# Caché en memoria del sw.js estampado: (versión, contenido).
_SW_CACHE = {"version": None, "contenido": ""}


def _version_static():
    """Versión de los estáticos: el mtime más nuevo bajo static/.

    Cambia cualquier archivo → cambia la constante CACHE del SW → el SW se
    reinstala y tira el shell viejo. Nada de versiones a mano.
    """
    base = settings.BASE_DIR / "static"
    mas_nuevo = 0.0
    for archivo in base.rglob("*"):
        try:
            if archivo.is_file():
                mas_nuevo = max(mas_nuevo, archivo.stat().st_mtime)
        except OSError:
            continue
    return int(mas_nuevo)


def service_worker(request):
    """sw.js servido desde la raíz para que su scope cubra TODO el sitio.

    Un SW servido desde /static/ solo controlaría /static/; por eso el
    archivo vive en static/pwa/ pero se responde aquí, en /sw.js — con la
    versión del shell estampada desde el servidor (mtime de static/), el
    contenido cacheado en memoria y Cache-Control: no-cache para que el
    navegador siempre revalide la versión.
    """
    ruta = settings.BASE_DIR / "static" / "pwa" / "sw.js"
    version = _version_static()
    if _SW_CACHE["version"] != version:
        try:
            crudo = ruta.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise Http404("No hay service worker publicado.")
        _SW_CACHE["contenido"] = crudo.replace("torre-shell-v1", f"torre-shell-{version}")
        _SW_CACHE["version"] = version
    respuesta = HttpResponse(
        _SW_CACHE["contenido"],
        content_type="application/javascript",
    )
    respuesta["Cache-Control"] = "no-cache"
    return respuesta
