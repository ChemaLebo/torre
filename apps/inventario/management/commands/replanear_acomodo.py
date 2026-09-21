"""Reacomodo total de un cliente: planea de cero dónde va cada SKU y lote
como si la bodega estuviera vacía de sus productos (inventario.replanear_bodega),
escribe la hoja CSV para el piso y, con --aplicar, mueve el inventario del
sistema a los anaqueles del plan. Por consola a propósito (Chema 2026-09-21)."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import SKU
from apps.core.models import Cliente
from apps.inventario.services import replanear_bodega


class Command(BaseCommand):
    help = (
        "Plan de reacomodo total de un cliente (SKU, lote, rack, cantidad) como si la "
        "bodega estuviera vacía de sus productos; escribe el CSV y con --aplicar mueve el "
        "inventario del sistema a esos anaqueles."
    )

    def add_arguments(self, parser):
        parser.add_argument("--cliente", required=True, help="Slug del cliente (colima).")
        parser.add_argument("--csv", default="", help="Ruta del CSV (default /tmp/reacomodo-<slug>.csv).")
        parser.add_argument("--usuario", default="", help="Username que firma el reacomodo (default: consola).")
        parser.add_argument("--aplicar", action="store_true", help="Mueve el inventario del sistema; sin la bandera solo planea.")

    def handle(self, *args, **options):
        cliente = Cliente.objects.filter(slug=options["cliente"]).first()
        if cliente is None:
            raise CommandError(f"No existe el cliente {options['cliente']}.")
        actor = "consola"
        if options["usuario"]:
            from django.contrib.auth import get_user_model
            actor = get_user_model().objects.filter(username=options["usuario"]).first()
            if actor is None:
                raise CommandError(f"No existe el usuario {options['usuario']}.")
        r = replanear_bodega(cliente, actor=actor, aplicar=options["aplicar"])
        ruta = Path(options["csv"] or f"/tmp/reacomodo-{cliente.slug}.csv")
        skus = {s.pk: s for s in SKU.objects.filter(pk__in={p["sku_id"] for p in r["pasos"]})}
        with ruta.open("w", newline="", encoding="utf-8-sig") as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow(["sku", "nombre", "codigo_barras", "lote", "rack", "cantidad", "desde"])
            for p in r["pasos"]:
                sku = skus[p["sku_id"]]
                desde = " · ".join(f"{codigo}: {n}" for codigo, n in p["desde"])
                escritor.writerow([
                    p["sku"], sku.descripcion, sku.codigo_barras, p["lote"],
                    p["ubicacion"] or "SIN ESPACIO (se queda donde está)", p["cantidad"], desde,
                ])
        for p in r["pasos"]:
            self.stdout.write(
                f"{p['sku']:16} {p['lote'] or '-':12} {p['ubicacion'] or 'SIN ESPACIO':14} {p['cantidad']:4}  {p['motivo']}"
            )
        self.stdout.write(f"CSV: {ruta} ({len(r['pasos'])} pasos)")
        if r["sin_espacio"]:
            self.stdout.write(self.style.WARNING(f"{r['sin_espacio']} pieza(s) sin anaquel con espacio: se quedan donde están."))
        if options["aplicar"]:
            self.stdout.write(self.style.SUCCESS(f"Inventario movido en el sistema: {r['movidas']} pieza(s). El piso acomoda con el CSV."))
        else:
            self.stdout.write(self.style.WARNING("Simulación: el inventario no se movió. Repite con --aplicar cuando el piso vaya a acomodar con esta hoja."))
