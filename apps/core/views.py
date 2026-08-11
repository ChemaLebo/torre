from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect

from .models import EvidenciaFoto


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


# ─────────────────────────────────────────────────────────────────────────────
# Evidencia con autorización. MEDIA no tiene auth (y en producción ni ruta):
# las fotos llevan datos de clientes y compradores, así que salen SOLO por
# aquí — mismo criterio que las etiquetas PDF en envios.api_impresion.
# ─────────────────────────────────────────────────────────────────────────────

def _resolver_referencia(modelo, eid, relacion):
    """Primer objeto cuyo pk (si eid es numérico) o folio coincide con eid."""
    filtro = {"pk": int(eid)} if eid.isdigit() else {"folio": eid}
    return modelo.objects.filter(**filtro).select_related(relacion).first()


def _cliente_de_evidencia(foto):
    """Cliente dueño de la foto, resolviendo la referencia (entidad, entidad_id).

    entidad_id mezcla pk y folio según el punto de captura (deuda conocida —
    ver incidencias._congelar_evidencia_pedido): se aceptan ambas formas.
    Referencia irresoluble → None (el portal la trata como ajena).
    """
    # Lazy por contrato: core es la hoja del grafo, importa dentro de la función.
    from apps.incidencias.models import Incidencia
    from apps.inventario.models import Conteo, OrdenEntrada
    from apps.pedidos.models import Pedido

    rutas = {
        "pedido": (Pedido, "cliente"),
        "entrega_local": (Pedido, "cliente"),
        "asn": (OrdenEntrada, "cliente"),
        "incidencia": (Incidencia, "cliente"),
        "conteo": (Conteo, "sku__cliente"),
    }
    if foto.entidad not in rutas:
        return None
    modelo, relacion = rutas[foto.entidad]
    objeto = _resolver_referencia(modelo, str(foto.entidad_id), relacion)
    if objeto is None:
        return None
    return objeto.sku.cliente if foto.entidad == "conteo" else objeto.cliente


@login_required
def evidencia(request, pk):
    """Sirve una EvidenciaFoto con autorización por rol y tenant.

    piso/mesa (y superuser) ven todo: la bodega opera a todos los clientes.
    portal SOLO lo de su cliente — foto ajena o irresoluble regresa el mismo
    404 que una inexistente (cero filtración de existencia).
    """
    foto = get_object_or_404(EvidenciaFoto, pk=pk)
    es_interno = request.user.is_superuser or request.rol in ("piso", "mesa")
    if not es_interno:
        if request.rol != "portal" or request.cliente is None:
            raise Http404
        if _cliente_de_evidencia(foto) != request.cliente:
            raise Http404
    try:
        return FileResponse(foto.archivo.open("rb"))
    except (FileNotFoundError, ValueError):
        raise Http404
