"""App de finanzas: motor de facturación por tarifario (cortes, estados de
cuenta, reembolsos de paquetería). Las vistas viven en Mesa."""
from django.apps import AppConfig


class FinanzasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finanzas"
    verbose_name = "Finanzas"
