"""Prueba de humo de las notificaciones push: manda una real al rol piso.

Uso (con los celulares ya suscritos desde la PWA):
    ../torre-env/bin/python manage.py probar_push
"""
from django.core.management.base import BaseCommand

from apps.mensajeria import push


class Command(BaseCommand):
    help = "Manda una notificación push de prueba a las suscripciones del rol piso."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rol", default="piso", choices=["piso", "mesa", "portal"],
            help="Rol destinatario de la prueba (default: piso)",
        )

    def handle(self, *args, **options):
        if not push.vapid_configurado():
            self.stdout.write(self.style.WARNING(
                "VAPID no está configurado (revisa VAPID_* en torre/.env y el .pem); "
                "no se mandó nada."
            ))
            return
        rol = options["rol"]
        enviadas = push.enviar_push_a_rol(
            rol,
            "🔔 Prueba de Torre",
            "Si ves esto, las notificaciones jalan.",
            url="/",
        )
        if enviadas:
            self.stdout.write(self.style.SUCCESS(
                f"Notificaciones enviadas al rol {rol}: {enviadas}"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"Salieron 0 notificaciones al rol {rol}. "
                "¿Ya tocaron 'Activar notificaciones' en la PWA?"
            ))
