"""Miniaturas de las fotos de evidencia que no la tienen (Chema 2026-10-14).

    python manage.py generar_miniaturas

Idempotente: solo las que no tienen miniatura; una foto que no sea imagen
(o cuyo archivo falte) se reporta y sigue sin miniatura (se sirve el original).
"""
from django.core.management.base import BaseCommand

from apps.core.models import EvidenciaFoto


class Command(BaseCommand):
    help = "Genera la miniatura de cada EvidenciaFoto que no la tenga."

    def handle(self, *args, **opciones):
        hechas, sin = 0, []
        for foto in EvidenciaFoto.objects.filter(miniatura="").order_by("pk").iterator():
            if foto.generar_miniatura():
                hechas += 1
            else:
                sin.append(foto.pk)
        self.stdout.write(self.style.SUCCESS(f"{hechas} miniatura(s) generada(s)."))
        if sin:
            self.stdout.write(self.style.WARNING(f"{len(sin)} sin miniatura (no es imagen o falta el archivo): {sin[:20]}"))
