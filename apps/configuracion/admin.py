"""Admin de las configuraciones de sistema: la lista fija de correos vive aquí."""
from django.contrib import admin

from .models import CorreoIncidencias


@admin.register(CorreoIncidencias)
class CorreoIncidenciasAdmin(admin.ModelAdmin):
    list_display = ("correo", "nombre", "cliente", "activo", "creado")
    list_filter = ("activo", "cliente")
    search_fields = ("correo", "nombre")
