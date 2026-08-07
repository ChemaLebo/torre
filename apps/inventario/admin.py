from django.contrib import admin

from .models import Ajuste, Conteo, LineaASN, Movimiento, OrdenEntrada, Saldo, TareaConteo


@admin.register(Saldo)
class SaldoAdmin(admin.ModelAdmin):
    list_display = ("sku", "ubicacion", "lote", "estado", "cantidad")
    list_filter = ("estado", "ubicacion", "sku__cliente")
    search_fields = ("sku__codigo", "ubicacion__codigo", "lote__codigo")


@admin.register(Movimiento)
class MovimientoAdmin(admin.ModelAdmin):
    """Kardex append-only: solo lectura. Se corrige con otro movimiento, nunca editando."""

    list_display = ("ts", "sku", "tipo", "delta", "estado_origen", "estado_destino", "referencia", "actor")
    list_filter = ("tipo", "sku__cliente")
    search_fields = ("sku__codigo", "referencia", "actor")
    date_hierarchy = "ts"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class LineaASNInline(admin.TabularInline):
    model = LineaASN
    extra = 0


@admin.register(OrdenEntrada)
class OrdenEntradaAdmin(admin.ModelAdmin):
    list_display = ("folio", "cliente", "estado", "fecha_compromiso", "ts_descarga_fin", "ts_vendible", "sla_vencido")
    list_filter = ("estado", "cliente")
    search_fields = ("folio",)
    inlines = [LineaASNInline]
    readonly_fields = ("folio", "ts_descarga_fin", "ts_vendible")

    @admin.display(boolean=True, description="SLA vencido")
    def sla_vencido(self, obj):
        return obj.sla_vencido


@admin.register(Conteo)
class ConteoAdmin(admin.ModelAdmin):
    list_display = ("folio", "sku", "esperado", "contado", "diferencia", "contador", "ts")
    list_filter = ("sku__cliente",)
    search_fields = ("folio", "sku__codigo", "contador")
    readonly_fields = ("folio",)

    @admin.display(description="Diferencia")
    def diferencia(self, obj):
        return f"{obj.diferencia:+d}"


@admin.register(Ajuste)
class AjusteAdmin(admin.ModelAdmin):
    """Los ajustes nacen por servicio con doble firma; aquí solo se consultan."""

    list_display = ("folio", "sku", "delta", "motivo", "autorizo_1", "autorizo_2", "incidencia_ref", "ts")
    list_filter = ("motivo", "sku__cliente")
    search_fields = ("folio", "sku__codigo", "autorizo_1", "autorizo_2", "incidencia_ref")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TareaConteo)
class TareaConteoAdmin(admin.ModelAdmin):
    list_display = ("fecha", "sku", "estado", "conteo")
    list_filter = ("estado", "fecha")
    search_fields = ("sku__codigo",)
