from django.contrib import admin

from .models import SKU, Lote, Ubicacion


@admin.register(SKU)
class SKUAdmin(admin.ModelAdmin):
    list_display = (
        "codigo", "cliente", "descripcion", "unidad", "peso_gr",
        "precio_declarado", "punto_reorden", "requiere_lote", "backorder_habilitado", "activo",
    )
    list_filter = ("cliente", "activo", "requiere_lote", "backorder_habilitado")
    search_fields = ("codigo", "codigo_barras", "descripcion")
    ordering = ("cliente", "codigo")


@admin.register(Ubicacion)
class UbicacionAdmin(admin.ModelAdmin):
    list_display = ("codigo", "tipo", "activo")
    list_filter = ("tipo", "activo")
    search_fields = ("codigo",)


@admin.register(Lote)
class LoteAdmin(admin.ModelAdmin):
    list_display = ("codigo", "sku", "fecha_caducidad")
    list_filter = ("sku__cliente",)
    search_fields = ("codigo", "sku__codigo")
    ordering = ("fecha_caducidad",)
