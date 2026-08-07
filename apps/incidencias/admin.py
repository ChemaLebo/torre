"""Admin de incidencias: gestión de folios, timeline, compensaciones y reclamaciones."""
from django.contrib import admin

from .models import Compensacion, Incidencia, MensajeIncidencia, ReclamacionCarrier


class MensajeIncidenciaInline(admin.TabularInline):
    model = MensajeIncidencia
    extra = 0
    fields = ["ts", "autor", "rol_autor", "interno", "texto"]
    readonly_fields = ["ts"]


class CompensacionInline(admin.TabularInline):
    model = Compensacion
    extra = 0
    fields = ["tipo", "monto", "estado", "aprobo", "fecha_pago", "referencia_pago"]
    readonly_fields = ["estado"]
    show_change_link = True


class ReclamacionCarrierInline(admin.TabularInline):
    model = ReclamacionCarrier
    extra = 0
    fields = ["carrier", "monto_reclamado", "estado", "monto_recuperado", "fecha_presentacion"]
    readonly_fields = ["estado"]
    show_change_link = True


@admin.register(Incidencia)
class IncidenciaAdmin(admin.ModelAdmin):
    list_display = [
        "folio", "cliente", "tipo", "prioridad", "origen", "estado", "pedido",
        "dueno", "ts_apertura", "fuera_de_sla_respuesta", "fuera_de_sla_resolucion",
    ]
    list_filter = ["estado", "tipo", "prioridad", "origen", "cliente"]
    search_fields = ["folio", "dueno", "pedido__folio"]
    date_hierarchy = "ts_apertura"
    readonly_fields = [
        "folio", "ts_apertura", "ts_primera_respuesta", "ts_resolucion", "ts_cierre",
        "sla_respuesta_limite", "sla_resolucion_limite",
    ]
    inlines = [MensajeIncidenciaInline, CompensacionInline, ReclamacionCarrierInline]

    @admin.display(boolean=True, description="Vencida respuesta")
    def fuera_de_sla_respuesta(self, obj):
        return obj.vencida_respuesta

    @admin.display(boolean=True, description="Vencida resolución")
    def fuera_de_sla_resolucion(self, obj):
        return obj.vencida_resolucion


@admin.register(MensajeIncidencia)
class MensajeIncidenciaAdmin(admin.ModelAdmin):
    list_display = ["incidencia", "ts", "autor", "rol_autor", "interno"]
    list_filter = ["rol_autor", "interno"]
    search_fields = ["incidencia__folio", "autor", "texto"]
    readonly_fields = ["ts"]


@admin.register(Compensacion)
class CompensacionAdmin(admin.ModelAdmin):
    list_display = ["incidencia", "tipo", "monto", "estado", "aprobo", "fecha_pago", "referencia_pago"]
    list_filter = ["estado", "tipo"]
    search_fields = ["incidencia__folio", "referencia_pago", "aprobo"]


@admin.register(ReclamacionCarrier)
class ReclamacionCarrierAdmin(admin.ModelAdmin):
    list_display = [
        "incidencia", "carrier", "monto_reclamado", "estado", "monto_recuperado",
        "fecha_presentacion", "fecha_resolucion", "fecha_pago",
    ]
    list_filter = ["estado", "carrier"]
    search_fields = ["incidencia__folio", "carrier"]
