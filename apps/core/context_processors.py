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


def torre(request):
    return {
        "reingresos_pendientes": _reingresos_pendientes(request),
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
