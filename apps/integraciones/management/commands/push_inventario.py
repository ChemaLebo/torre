"""Job: drena la cola de push de inventario hacia Shopify.

Idempotente: cola vacía = no-op; con token real, compareQuantity impide pisar
un snapshot más nuevo (nada de last-writer-wins).
"""
from django.core.management.base import BaseCommand

from apps.integraciones import services


class Command(BaseCommand):
    help = "Empuja on_hand (vendible − cuarentena − buffer) de los SKUs encolados a todas sus tiendas."

    def handle(self, *args, **options):
        resumen = services.push_inventario()
        if resumen["skus"] == 0:
            self.stdout.write("Cola de push vacía: nada que empujar.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"{resumen['skus']} SKUs procesados: "
            f"{resumen['pushes_ok']} pushes ok, {resumen['pushes_error']} con error"
        ))
