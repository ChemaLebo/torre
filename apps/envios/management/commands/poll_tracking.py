"""Command `poll_tracking`: cron cada 30 min desde el día 1 (BLUEPRINT §1.4)."""
from django.core.management.base import BaseCommand

from apps.envios.services import poll_tracking


class Command(BaseCommand):
    help = (
        "Rastrea toda guía no terminal vía el adapter de carrier, normaliza "
        "estados, avanza pedidos y abre incidencias (RET/RF) según umbrales."
    )

    def handle(self, *args, **options):
        resumen = poll_tracking()
        self.stdout.write(self.style.SUCCESS(
            "Tracking: {rastreadas} guías rastreadas · {actualizadas} actualizadas · "
            "{incidencias} incidencias abiertas · {errores} errores de rastreo".format(**resumen)
        ))
