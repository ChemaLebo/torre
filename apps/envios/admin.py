from django.contrib import admin

from .models import Guia, ReglaEnvio


@admin.register(Guia)
class GuiaAdmin(admin.ModelAdmin):
    list_display = (
        "id", "pedido", "carrier", "servicio", "numero", "estado",
        "costo_cotizado", "costo_preferencial", "ts_ultimo_movimiento", "creado",
    )
    list_filter = ("carrier", "estado")
    search_fields = ("numero", "pedido__folio")
    readonly_fields = ("creado", "raw")
    date_hierarchy = "creado"


@admin.register(ReglaEnvio)
class ReglaEnvioAdmin(admin.ModelAdmin):
    list_display = ("prioridad", "cliente", "condicion", "carrier", "servicio")
    list_filter = ("carrier", "cliente")
    ordering = ("prioridad", "id")


from .models import CotizacionCache, Paquete, PaqueteLinea, TrabajoImpresion  # noqa: E402


@admin.register(TrabajoImpresion)
class TrabajoImpresionAdmin(admin.ModelAdmin):
    list_display = ("id", "guia", "estado", "intentos", "error", "creado", "ts_impreso")
    list_filter = ("estado",)
    search_fields = ("guia__numero", "guia__pedido__folio")
    readonly_fields = ("creado", "ts_impreso")
    date_hierarchy = "creado"


class PaqueteLineaInline(admin.TabularInline):
    model = PaqueteLinea
    extra = 0


@admin.register(Paquete)
class PaqueteAdmin(admin.ModelAdmin):
    list_display = ("pedido", "numero", "peso_kg", "carrier", "precio_cotizado",
                    "fuera_de_meta", "ahorro_plan_mxn", "estado")
    list_filter = ("estado", "carrier", "fuera_de_meta")
    inlines = [PaqueteLineaInline]


@admin.register(CotizacionCache)
class CotizacionCacheAdmin(admin.ModelAdmin):
    list_display = ("cp_destino", "peso_kg", "carrier", "servicio", "precio", "ok", "ts")
    list_filter = ("carrier", "ok")
    search_fields = ("cp_destino",)
