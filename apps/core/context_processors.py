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


def torre(request):
    return {
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
