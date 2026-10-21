from django.contrib import admin

from .models import LineaPedido, Pedido, PickLinea


class LineaPedidoInline(admin.TabularInline):
    model = LineaPedido
    extra = 0
    raw_id_fields = ("sku", "lote_asignado")


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = (
        "folio", "cliente", "tienda", "estado", "es_local", "cp",
        "comprador_nombre", "valor_declarado", "incidencia_activa",
        "asignado_a", "transferencia_a", "creado",
    )
    list_filter = ("estado", "es_local", "incidencia_activa", "origen", "cliente")
    search_fields = ("folio", "shopify_order_id", "comprador_nombre", "comprador_email", "comprador_tel")
    date_hierarchy = "creado"
    inlines = [LineaPedidoInline]
    readonly_fields = (
        "folio", "creado", "actualizado",
        "ts_picking", "ts_empacado", "ts_guia", "ts_recolectado", "ts_en_transito", "ts_entregado",
    )
    # asignado_a/transferencia_a como raw_id: el RESCATE de un pedido cuyo
    # dueño no está se hace aquí (reasignar o dejar en blanco = liberarlo).
    raw_id_fields = ("tienda", "cliente", "asignado_a", "transferencia_a")


@admin.register(LineaPedido)
class LineaPedidoAdmin(admin.ModelAdmin):
    list_display = ("pedido", "sku", "cantidad", "cantidad_repuesta", "cantidad_pickeada", "cantidad_despachada", "reservada", "lote_asignado")
    list_filter = ("reservada",)
    search_fields = ("pedido__folio", "sku__codigo")
    raw_id_fields = ("pedido", "sku", "lote_asignado")


@admin.register(PickLinea)
class PickLineaAdmin(admin.ModelAdmin):
    """Piezas por pedido (Chema 2026-10-22): de qué posición y lote salió cada
    pieza pickeada y en qué etapa va; solo lectura operativa (se escribe desde
    el picking, el empaque y la salida)."""
    list_display = ("pedido_folio", "sku_codigo", "ubicacion", "lote", "cantidad", "estado", "caja", "ts")
    list_filter = ("estado",)
    search_fields = ("linea__pedido__folio", "linea__sku__codigo", "lote__codigo", "ubicacion__codigo")
    raw_id_fields = ("linea", "lote", "ubicacion", "caja")
    list_select_related = ("linea__pedido", "linea__sku", "ubicacion", "lote", "caja")

    @admin.display(description="pedido", ordering="linea__pedido__folio")
    def pedido_folio(self, obj):
        return obj.linea.pedido.folio

    @admin.display(description="producto", ordering="linea__sku__codigo")
    def sku_codigo(self, obj):
        return obj.linea.sku.codigo
