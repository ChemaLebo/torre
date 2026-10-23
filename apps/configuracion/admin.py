"""Admin de las configuraciones de sistema: la lista fija de correos, la tanda
de etiquetas, las cantidades de etiquetas de manejo, los orígenes de envío y
las zonas de cobro por CP viven aquí."""
from django.contrib import admin

from .models import CorreoIncidencias, EtiquetasManejo, Origen, TandaEtiquetas, ZonaCP


@admin.register(CorreoIncidencias)
class CorreoIncidenciasAdmin(admin.ModelAdmin):
    list_display = ("correo", "nombre", "cliente", "activo", "creado")
    list_filter = ("activo", "cliente")
    search_fields = ("correo", "nombre")


@admin.register(TandaEtiquetas)
class TandaEtiquetasAdmin(admin.ModelAdmin):
    list_display = ("cajas", "actualizado")


@admin.register(EtiquetasManejo)
class EtiquetasManejoAdmin(admin.ModelAdmin):
    list_display = ("fragil", "orientacion", "actualizado")


@admin.register(Origen)
class OrigenAdmin(admin.ModelAdmin):
    list_display = ("nombre", "cp", "ciudad", "estado", "principal", "activo")
    list_filter = ("activo", "principal")
    search_fields = ("nombre", "cp", "ciudad")


@admin.register(ZonaCP)
class ZonaCPAdmin(admin.ModelAdmin):
    list_display = ("origen", "cp_desde", "cp_hasta", "zona", "ciudad", "activo")
    list_filter = ("origen", "zona", "activo")
    search_fields = ("cp_desde", "cp_hasta", "ciudad")
    ordering = ("origen", "cp_desde")
