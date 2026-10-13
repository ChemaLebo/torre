"""Admin de las configuraciones de sistema: la lista fija de correos y la tanda de etiquetas viven aquí."""
from django.contrib import admin

from .models import CorreoIncidencias, TandaEtiquetas


@admin.register(CorreoIncidencias)
class CorreoIncidenciasAdmin(admin.ModelAdmin):
    list_display = ("correo", "nombre", "cliente", "activo", "creado")
    list_filter = ("activo", "cliente")
    search_fields = ("correo", "nombre")


@admin.register(TandaEtiquetas)
class TandaEtiquetasAdmin(admin.ModelAdmin):
    list_display = ("cajas", "actualizado")
