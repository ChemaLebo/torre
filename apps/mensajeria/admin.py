from django.contrib import admin

from .models import NotificacionEnviada, PlantillaMensaje, SuscripcionPush


@admin.register(PlantillaMensaje)
class PlantillaMensajeAdmin(admin.ModelAdmin):
    list_display = ("clave", "nombre", "cliente", "aprobada_por_cliente", "creado")
    list_filter = ("clave", "aprobada_por_cliente", "cliente")
    search_fields = ("nombre", "cuerpo")


@admin.register(NotificacionEnviada)
class NotificacionEnviadaAdmin(admin.ModelAdmin):
    """Registro de salida: se consulta, no se edita — la historia no se retoca."""

    list_display = ("ts", "canal", "destinatario", "plantilla_clave", "referencia", "cliente")
    list_filter = ("canal", "plantilla_clave", "cliente")
    search_fields = ("destinatario", "clave_idempotencia", "referencia", "cuerpo")
    date_hierarchy = "ts"
    readonly_fields = (
        "clave_idempotencia", "canal", "destinatario", "cuerpo", "ts",
        "cliente", "plantilla_clave", "referencia",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SuscripcionPush)
class SuscripcionPushAdmin(admin.ModelAdmin):
    list_display = ("usuario", "activo", "user_agent", "creado", "endpoint_corto")
    list_filter = ("activo",)
    search_fields = ("usuario__username", "endpoint", "user_agent")

    @admin.display(description="endpoint")
    def endpoint_corto(self, obj):
        return obj.endpoint[:80]
