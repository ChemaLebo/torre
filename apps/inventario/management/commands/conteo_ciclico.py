"""Job diario (9:00): genera las tareas del conteo cíclico.

Sin Celery en dev: la lógica idempotente vive en services.generar_conteo_ciclico;
este command solo la invoca (cron/systemd en producción).
"""
import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.inventario.services import generar_conteo_ciclico


class Command(BaseCommand):
    help = "Conteo cíclico: elige los 3 SKUs del día (por antigüedad de conteo) y crea las tareas."

    def add_arguments(self, parser):
        parser.add_argument("--fecha", help="Fecha objetivo AAAA-MM-DD (default: hoy)")

    def handle(self, *args, **options):
        fecha = None
        if options.get("fecha"):
            try:
                fecha = datetime.date.fromisoformat(options["fecha"])
            except ValueError:
                raise CommandError(f"Fecha inválida: {options['fecha']} (usa AAAA-MM-DD).")
        tareas = generar_conteo_ciclico(fecha=fecha)
        if not tareas:
            self.stdout.write("No hay SKUs activos que contar hoy.")
            return
        self.stdout.write(self.style.SUCCESS(f"Tareas de conteo para {tareas[0].fecha}:"))
        for tarea in tareas:
            self.stdout.write(f"  · {tarea.sku.codigo} — {tarea.get_estado_display()}")
