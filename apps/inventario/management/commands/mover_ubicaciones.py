"""Reacomodo de racks: mueve ubicaciones completas según un mapa VIEJO=NUEVO.

    python manage.py mover_ubicaciones PIC-5-1=PIC-4-I-F-1 PIC-6-3=PIC-4-D-F-3 --aplicar
    python manage.py mover_ubicaciones --archivo mapa.csv --aplicar   (columnas: viejo,nuevo)

Por cada par, inventario.services.mover_ubicacion: si el nuevo código no
existe, la ubicación se renombra con todo su inventario; si ya existe, los
saldos se fusionan ahí y la vieja queda inactiva. Dry-run por default; se
valida el mapa completo antes de escribir y todo va en una sola transacción.
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from apps.catalogo.models import Ubicacion
from apps.inventario.models import Saldo
from apps.inventario.services import mover_ubicacion


def leer_mapa(pares, archivo):
    """[(viejo, nuevo)] desde los pares VIEJO=NUEVO y/o el CSV viejo,nuevo."""
    mapa = []
    for par in pares or []:
        if "=" not in par:
            raise CommandError(f"Par inválido '{par}': usa VIEJO=NUEVO.")
        viejo, nuevo = (p.strip() for p in par.split("=", 1))
        mapa.append((viejo, nuevo))
    if archivo:
        with Path(archivo).open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                viejo, nuevo = (fila.get("viejo") or "").strip(), (fila.get("nuevo") or "").strip()
                if viejo and nuevo:
                    mapa.append((viejo, nuevo))
    if not mapa:
        raise CommandError("Sin pares que mover: pasa VIEJO=NUEVO o --archivo.")
    return mapa


class Command(BaseCommand):
    help = "Mueve ubicaciones completas (renombra o fusiona) según un mapa VIEJO=NUEVO. Dry-run sin --aplicar."

    def add_arguments(self, parser):
        parser.add_argument("pares", nargs="*", help="VIEJO=NUEVO (varios)")
        parser.add_argument("--archivo", help="CSV con columnas viejo,nuevo")
        parser.add_argument("--aplicar", action="store_true", help="Escribe en la base (sin esto solo muestra).")

    def handle(self, *args, **opciones):
        mapa = leer_mapa(opciones["pares"], opciones["archivo"])
        errores = []
        vistos_origen, vistos_destino = set(), set()
        for viejo, nuevo in mapa:
            if viejo == nuevo:
                errores.append(f"{viejo}: origen y destino iguales.")
            if viejo in vistos_origen:
                errores.append(f"{viejo}: aparece dos veces como origen.")
            if nuevo in vistos_destino:
                errores.append(f"{nuevo}: dos orígenes al mismo destino (fusiona en dos pasos).")
            vistos_origen.add(viejo)
            vistos_destino.add(nuevo)
            if not Ubicacion.objects.filter(codigo=viejo).exists():
                errores.append(f"{viejo}: no existe.")
        if errores:
            raise CommandError("Mapa con errores, nada escrito:\n  " + "\n  ".join(errores))

        for viejo, nuevo in mapa:
            piezas = Saldo.objects.filter(ubicacion__codigo=viejo).aggregate(t=Sum("cantidad"))["t"] or 0
            existe = Ubicacion.objects.filter(codigo=nuevo).exists()
            accion = "fusionar en la existente" if existe else "renombrar"
            self.stdout.write(f"  {viejo} → {nuevo}: {piezas} pieza(s), {accion}")
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING("Dry-run: nada escrito. Repite con --aplicar para mover."))
            return
        with transaction.atomic():
            for viejo, nuevo in mapa:
                r = mover_ubicacion(viejo, nuevo, actor="mover_ubicaciones")
                self.stdout.write(f"  ✓ {viejo} → {nuevo} ({r['modo']}, {r['piezas']} pieza(s))")
        self.stdout.write(self.style.SUCCESS(f"{len(mapa)} ubicación(es) movida(s)."))
