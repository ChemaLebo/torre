from django.contrib import admin

from .models import Cliente, EventoAuditoria, PerfilUsuario


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ("nombre", "slug", "carrier_preferente", "activo")
    prepopulated_fields = {"slug": ("nombre",)}


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "rol", "cliente")
    list_filter = ("rol",)


@admin.register(EventoAuditoria)
class EventoAuditoriaAdmin(admin.ModelAdmin):
    list_display = ("ts", "entidad", "entidad_id", "accion", "actor_tipo", "actor_id", "cliente")
    list_filter = ("entidad", "actor_tipo")
    search_fields = ("entidad_id", "accion", "motivo")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
