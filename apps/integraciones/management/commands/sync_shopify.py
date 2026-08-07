"""Job: sincronización completa con Shopify (reconciliar + replay + push).

Idempotente: correrlo dos veces seguidas no duplica pedidos ni pushes.
Programación sugerida en dev: cada 10 min (BLUEPRINT §2.2.8 — polling de respaldo).
"""
from django.core.management.base import BaseCommand

from apps.integraciones import services
from apps.integraciones.models import Tienda


class Command(BaseCommand):
    help = "Reconcilia pedidos por tienda, reintenta webhooks pendientes y empuja inventario."

    def handle(self, *args, **options):
        tiendas = Tienda.objects.filter(activo=True)
        if not tiendas.exists():
            self.stdout.write(self.style.WARNING("No hay tiendas activas registradas."))
            return
        for tienda in tiendas:
            nuevos = services.reconciliar_pedidos(tienda)
            self.stdout.write(f"{tienda.dominio}: {nuevos} pedidos nuevos por reconciliación")
        replays = services.reprocesar_pendientes()
        if replays:
            self.stdout.write(f"Replay: {replays} webhooks pendientes reprocesados")
        resumen = services.push_inventario()
        self.stdout.write(self.style.SUCCESS(
            f"Push de inventario: {resumen['skus']} SKUs, "
            f"{resumen['pushes_ok']} ok, {resumen['pushes_error']} con error"
        ))
