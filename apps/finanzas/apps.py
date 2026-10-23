"""App de finanzas: motor de facturación por tarifario (cortes, estados de
cuenta, reembolsos de paquetería). Las vistas viven en Mesa."""
from django.apps import AppConfig


class FinanzasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finanzas"
    verbose_name = "Finanzas"

    def ready(self):
        """La caché de zonas por CP se olvida al tocar orígenes o rangos en el admin."""
        from django.db.models.signals import post_delete, post_save

        from apps.configuracion.models import Origen, ZonaCP

        from . import zonas

        for modelo in (Origen, ZonaCP):
            post_save.connect(zonas.invalidar, sender=modelo, dispatch_uid=f"finanzas_zonas_save_{modelo.__name__}")
            post_delete.connect(zonas.invalidar, sender=modelo, dispatch_uid=f"finanzas_zonas_delete_{modelo.__name__}")
