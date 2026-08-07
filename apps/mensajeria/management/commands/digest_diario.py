"""Command: digest_diario — el resumen consolidado de las 17:30 por cliente.

Idempotente: correrlo dos veces el mismo día no duplica mensajes (la clave
de idempotencia por día/cliente lo garantiza). En producción se agenda a
las 17:30; en dev se corre a mano.
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.mensajeria.services import digest_diario


class Command(BaseCommand):
    help = "Envía el digest diario consolidado (uno solo, 17:30) a cada cliente activo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fecha", default=None,
            help="Fecha del digest en formato AAAA-MM-DD (default: hoy).",
        )

    def handle(self, *args, **options):
        fecha = None
        if options["fecha"]:
            try:
                fecha = datetime.strptime(options["fecha"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("Fecha inválida. Usa el formato AAAA-MM-DD, por ejemplo 2026-07-21.")

        notificaciones = digest_diario(fecha=fecha)
        if not notificaciones:
            self.stdout.write(self.style.WARNING(
                "No salió ningún digest: no hay clientes activos o el canal de salida falló."
            ))
            return
        for notificacion in notificaciones:
            self.stdout.write(f"  · {notificacion.canal} → {notificacion.destinatario} ({notificacion.referencia})")
        self.stdout.write(self.style.SUCCESS(f"Digest del día listo: {len(notificaciones)} cliente(s)."))
