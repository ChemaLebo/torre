from django.contrib import admin

from .models import PushInventarioPendiente, SyncLog, Tienda, WebhookEvento


@admin.register(Tienda)
class TiendaAdmin(admin.ModelAdmin):
    list_display = ("dominio", "cliente", "plataforma", "location_id", "activo", "checkpoint_reconciliacion")
    list_filter = ("plataforma", "activo", "cliente")
    search_fields = ("dominio", "cliente__nombre")


@admin.register(WebhookEvento)
class WebhookEventoAdmin(admin.ModelAdmin):
    list_display = ("ts", "tienda", "topic", "webhook_id", "origen", "procesado")
    list_filter = ("topic", "origen", "procesado", "tienda")
    search_fields = ("webhook_id",)
    readonly_fields = ("ts",)
    date_hierarchy = "ts"


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ("ts", "tienda", "direccion", "resultado", "detalle_corto")
    list_filter = ("direccion", "resultado", "tienda")
    search_fields = ("detalle",)
    date_hierarchy = "ts"

    @admin.display(description="detalle")
    def detalle_corto(self, obj):
        return obj.detalle[:90]


@admin.register(PushInventarioPendiente)
class PushInventarioPendienteAdmin(admin.ModelAdmin):
    list_display = ("sku", "creado")
    search_fields = ("sku__codigo",)
