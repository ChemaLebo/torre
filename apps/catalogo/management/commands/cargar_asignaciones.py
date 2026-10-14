"""Carga el producto fijo de cada posición desde el plan de acomodo.

    python manage.py cargar_asignaciones --archivo plan-acomodo-posiciones.csv
    python manage.py cargar_asignaciones --archivo plan-acomodo-posiciones.csv --aplicar

Lee `nombre_nuevo` y `producto` (filas sin producto, "libre", la repisa de
merch y el piso 4 se ignoran). Por cada posición del archivo deja EXACTAMENTE
esos productos (quita los que ya no estén, asigna los nuevos; un producto que
vivía en otra posición se cambia). Dry-run por default; se valida todo antes
de escribir y todo va en una sola transacción.
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import SKU, AsignacionPosicion, Ubicacion
from apps.catalogo.services import asignar_producto, desasignar_producto


def leer_plan(archivo):
    """{posición: [códigos de SKU]} en el orden del archivo."""
    plan = {}
    with Path(archivo).open(encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            posicion = (fila.get("nombre_nuevo") or fila.get("posicion") or "").strip().upper()
            sku = (fila.get("producto") or "").strip()
            if not posicion:
                continue
            plan.setdefault(posicion, [])
            if sku and sku.upper() != "MERCH":
                plan[posicion].append(sku)
    return plan


class Command(BaseCommand):
    help = "Carga el producto fijo de cada posición desde el CSV del plan de acomodo. Dry-run sin --aplicar."

    def add_arguments(self, parser):
        parser.add_argument("--archivo", required=True, help="CSV con columnas nombre_nuevo (o posicion) y producto")
        parser.add_argument("--aplicar", action="store_true", help="Escribe en la base (sin esto solo muestra).")

    def handle(self, *args, **opciones):
        plan = leer_plan(opciones["archivo"])
        errores, cambios = [], []
        for codigo, skus in plan.items():
            ubicacion = Ubicacion.objects.filter(codigo=codigo, activo=True).first()
            if ubicacion is None:
                errores.append(f"{codigo}: no existe o está inactiva.")
                continue
            if len(skus) > AsignacionPosicion.MAXIMO_POR_POSICION:
                errores.append(f"{codigo}: {len(skus)} productos; el máximo es {AsignacionPosicion.MAXIMO_POR_POSICION}.")
            productos = []
            for sku_codigo in skus:
                sku = SKU.objects.filter(codigo=sku_codigo).first()
                if sku is None:
                    errores.append(f"{codigo}: el producto {sku_codigo} no existe en Torre.")
                else:
                    productos.append(sku)
            actuales = {a.sku.codigo: a for a in AsignacionPosicion.objects.filter(ubicacion=ubicacion).select_related("sku")}
            quitar = [a.sku for a in actuales.values() if a.sku.codigo not in {p.codigo for p in productos}]
            poner = [p for p in productos if p.codigo not in actuales]
            cambios.append((ubicacion, productos, quitar, poner))
        if errores:
            raise CommandError("Plan con errores, nada escrito:\n  " + "\n  ".join(errores))
        for ubicacion, productos, quitar, poner in cambios:
            self.stdout.write(
                f"  {ubicacion.codigo}: {', '.join(p.codigo for p in productos) or '(sin producto)'}"
                + (f" · quita {', '.join(s.codigo for s in quitar)}" if quitar else "")
                + (f" · pone {', '.join(s.codigo for s in poner)}" if poner else "")
            )
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING("Dry-run: nada escrito. Repite con --aplicar para cargar."))
            return
        with transaction.atomic():
            for ubicacion, _productos, quitar, _poner in cambios:
                for sku in quitar:
                    desasignar_producto(sku, actor="cargar_asignaciones")
            for ubicacion, productos, _quitar, _poner in cambios:
                for sku in productos:
                    asignar_producto(ubicacion, sku, actor="cargar_asignaciones")
        self.stdout.write(self.style.SUCCESS(f"{len(cambios)} posición(es) con su producto fijo."))
