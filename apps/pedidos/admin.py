from django.contrib import admin

from .models import LineaPedido, Pedido


class LineaPedidoInline(admin.TabularInline):
    model = LineaPedido
    extra = 0
    raw_id_fields = ("sku", "lote_asignado")


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = (
        "folio", "cliente", "tienda", "estado", "es_local", "cp",
        "comprador_nombre", "valor_declarado", "incidencia_activa", "creado",
    )
    list_filter = ("estado", "es_local", "incidencia_activa", "origen", "cliente")
    search_fields = ("folio", "shopify_order_id", "comprador_nombre", "comprador_email", "comprador_tel")
    date_hierarchy = "creado"
    inlines = [LineaPedidoInline]
    readonly_fields = (
        "folio", "creado", "actualizado",
        "ts_picking", "ts_empacado", "ts_guia", "ts_recolectado", "ts_en_transito", "ts_entregado",
    )
    raw_id_fields = ("tienda", "cliente")


@admin.register(LineaPedido)
class LineaPedidoAdmin(admin.ModelAdmin):
    list_display = ("pedido", "sku", "cantidad", "cantidad_pickeada", "reservada", "lote_asignado")
    list_filter = ("reservada",)
    search_fields = ("pedido__folio", "sku__codigo")
    raw_id_fields = ("pedido", "sku", "lote_asignado")
