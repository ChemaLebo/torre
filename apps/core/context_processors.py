from django.conf import settings

# Claves de settings.TORRE que las plantillas realmente usan. El dict completo
# trae parámetros internos (costos fijos, márgenes, tarifario) que NO deben
# viajar al contexto de plantillas públicas (login, rastreo): aquí solo va lo
# operativo que se pinta en pantalla.
_CLAVES_TORRE_TEMPLATES = (
    "BENCHMARK_PEDIDO_MXN",
    "CORTE_CONTRACTUAL",
    "SIN_MOVIMIENTO_FORANEO_HORAS",
    "SIN_MOVIMIENTO_LOCAL_HORAS",
    "SLA_PRIMERA_RESPUESTA_CLIENTE_HORAS",
    "SLA_RECEPCION_HORAS_CONTRACTUAL",
    "SLA_RESOLUCION_HORAS",
    "TOLERANCIA_PESO_PCT",
)


def _reingresos_pendientes(request):
    """Pedidos que ya salieron con mercancía por decidir (Mesa): badge de Recepciones."""
    if getattr(request, "rol", None) != "mesa" and not getattr(getattr(request, "user", None), "is_superuser", False):
        return 0
    try:
        from apps.pedidos.services import reingresos_por_decidir  # lazy por contrato
    except ImportError:
        return 0
    return reingresos_por_decidir().count()


def _incidencias_abiertas(request):
    """Incidencias sin cerrar para el badge de Incidencias en el menú (Chema
    2026-09-23: "abiertas" = todo lo que no está CERRADA, incluidas las
    resueltas que el cliente aún no confirma). Mesa ve las de todos los
    clientes; el portal solo las de su cliente."""
    rol = getattr(request, "rol", None)
    cliente = getattr(request, "cliente", None)
    superuser = getattr(getattr(request, "user", None), "is_superuser", False)
    if rol == "portal" and cliente is None:
        return 0
    if rol not in ("mesa", "portal") and not superuser:
        return 0
    try:
        from apps.incidencias.models import Incidencia  # lazy: modelo de otra app
    except ImportError:
        return 0
    qs = Incidencia.objects.exclude(estado=Incidencia.CERRADA)
    if rol == "portal":
        qs = qs.filter(cliente=cliente, interna=False)  # las internas son de la bodega
    return qs.count()


def torre(request):
    return {
        "reingresos_pendientes": _reingresos_pendientes(request),
        "badge_incidencias": _incidencias_abiertas(request),  # nombre propio: el dashboard del portal usa incidencias_abiertas
        "TORRE": {
            clave: settings.TORRE[clave]
            for clave in _CLAVES_TORRE_TEMPLATES
            if clave in settings.TORRE
        },
        "rol_actual": getattr(request, "rol", None),
        "cliente_actual": getattr(request, "cliente", None),
        # Llave pública VAPID para el opt-in de Web Push (vacía = sin push).
        "vapid_public_key": settings.VAPID_PUBLIC_KEY,
    }
