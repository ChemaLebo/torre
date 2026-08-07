from django.contrib import admin

from .models import AccesoEtiqueta, AccesoRastreo


@admin.register(AccesoRastreo)
class AccesoRastreoAdmin(admin.ModelAdmin):
    list_display = ("token", "pedido", "creado")
    search_fields = ("token", "pedido__folio")
    readonly_fields = ("token",)


@admin.register(AccesoEtiqueta)
class AccesoEtiquetaAdmin(admin.ModelAdmin):
    list_display = ("token", "guia", "creado")
    search_fields = ("token", "guia__numero", "guia__pedido__folio")
    readonly_fields = ("token",)
