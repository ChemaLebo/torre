"""Registra (o lista) el webhook de rastreo en 99minutos (2026-09-30).

    manage.py registrar_webhook_99minutos --url https://fulfillment.wop.partners/hooks/carriers/99minutos/<TOKEN>/
    manage.py registrar_webhook_99minutos --listar

99minutos admite 2 configuraciones como máximo (POST /api/v3/webhooks {url,
headers}); manda el shipment con su historial de Events[] a esa URL con
User-Agent 99notifications. El token de la URL es NOVENTA9_WEBHOOK_TOKEN.
El de envia se registra en su panel (tipo 3, tracking.simple), no por aquí.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.envios.adapters import Adapter99Minutos, ErrorCarrier


class Command(BaseCommand):
    help = "Registra o lista el webhook de rastreo de 99minutos."

    def add_arguments(self, parser):
        parser.add_argument("--url", help="URL pública del webhook (https://…/hooks/carriers/99minutos/<token>/)")
        parser.add_argument("--listar", action="store_true", help="Solo lista las configuraciones existentes")

    def handle(self, *args, **options):
        if not settings.NOVENTA9_API_KEY:
            raise CommandError("Falta NOVENTA9_API_KEY: sin cuenta no hay webhook que registrar.")
        adapter = Adapter99Minutos()
        try:
            if options["listar"] or not options["url"]:
                resp = adapter._request("GET", "/api/v3/webhooks")
                self.stdout.write(str(adapter._json(resp, "/webhooks")))
                if not options["url"]:
                    return
            url = options["url"]
            if not url.startswith("https://"):
                raise CommandError("La URL del webhook debe empezar con https://")
            resp = adapter._request("POST", "/api/v3/webhooks", json_body={"url": url})
            self.stdout.write(self.style.SUCCESS(f"Webhook registrado: {adapter._json(resp, '/webhooks')}"))
        except ErrorCarrier as exc:
            raise CommandError(f"99minutos respondió con error: {exc}") from exc
