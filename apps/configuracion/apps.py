"""Configuraciones de sistema (Chema 2026-10-06): ajustes a nivel Torre, no
por cliente, cada uno como su propio modelo con renglones (nunca valores
separados por comas ni variables de entorno que nadie ve). Se editan en el
admin de Django; lo que además es por cliente se edita en su ficha de Mesa."""
from django.apps import AppConfig


class ConfiguracionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.configuracion"
    verbose_name = "Configuraciones de sistema"
