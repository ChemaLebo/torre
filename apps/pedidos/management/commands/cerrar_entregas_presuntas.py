"""Job idempotente: pedidos EN_TRANSITO sin evento reciente → ENTREGA_PRESUNTA."""
from django.core.management.base import BaseCommand

from apps.pedidos.services import cerrar_entregas_presuntas


class Command(BaseCommand):
    help = (
        "Cierra como ENTREGA_PRESUNTA los pedidos EN_TRANSITO sin evento en N días "
        "(settings.TORRE['ENTREGA_PRESUNTA_DIAS'], default 7). Idempotente."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dias", type=int, default=None,
            help="Días sin evento para presumir entrega (default: settings.TORRE).",
        )

    def handle(self, *args, **options):
        cerrados = cerrar_entregas_presuntas(dias=options["dias"])
        for pedido in cerrados:
            self.stdout.write(f"{pedido.folio}: EN_TRANSITO → ENTREGA_PRESUNTA")
        self.stdout.write(
            self.style.SUCCESS(f"{len(cerrados)} pedido(s) cerrados como entrega presunta.")
        )
