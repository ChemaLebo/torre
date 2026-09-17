"""Arranque de la rotación A/B/C desde el reporte de ventas de Shopify
("Net items sold by product title": columnas "Product title" y "Net items
sold"), mientras Torre no tenga sus propios 90 días de ventas.

    python manage.py rotacion_desde_ventas --cliente colima --archivo ventas.csv
    python manage.py rotacion_desde_ventas --cliente colima --archivo ventas.csv --aplicar

Clasifica los títulos por acumulado (catalogo.services.clases_por_volumen,
cortes de TORRE["ROTACION_CORTES"]), empata cada título con los SKUs del
cliente por descripción (sin acentos, mayúsculas ni guiones tipográficos; las
variantes de un producto heredan la clase) y fuerza SKU.rotacion. Los SKUs
activos sin título en el reporte no vendieron: quedan en C. Los títulos sin
SKU se listan para revisarlos. Dry-run por default.
"""
import csv
import re
import unicodedata
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import SKU
from apps.catalogo.services import clases_por_volumen
from apps.core.models import Cliente
from apps.core.services import registrar_evento


def normalizar(texto):
    """Clave de empate: sin acentos, mayúsculas, guiones tipográficos ni espacios dobles."""
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    texto = texto.replace("—", "-").replace("–", "-").replace('"', "")
    return re.sub(r"\s+", " ", texto).strip().upper()


def leer_reporte(ruta):
    """{título: piezas} del CSV de Shopify; filas sin título o sin número se saltan."""
    ventas = {}
    with Path(ruta).open(encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            titulo = (fila.get("Product title") or "").strip()
            crudo = (fila.get("Net items sold") or "").strip().replace(",", "")
            if not titulo:
                continue
            try:
                ventas[titulo] = ventas.get(titulo, 0) + int(float(crudo))
            except ValueError:
                continue
    return ventas


class Command(BaseCommand):
    help = "Fuerza SKU.rotacion (A/B/C) desde el reporte de ventas por producto de Shopify. Dry-run sin --aplicar."

    def add_arguments(self, parser):
        parser.add_argument("--cliente", required=True, help="Slug del cliente.")
        parser.add_argument("--archivo", required=True, help="CSV de Shopify: Product title, Net items sold.")
        parser.add_argument("--aplicar", action="store_true", help="Escribe en la base (sin esto solo muestra).")

    def handle(self, *args, **opciones):
        cliente = Cliente.objects.filter(slug=opciones["cliente"]).first()
        if cliente is None:
            raise CommandError(f"Cliente '{opciones['cliente']}' no existe.")
        ventas = leer_reporte(opciones["archivo"])
        if not ventas:
            raise CommandError("El reporte no trae títulos con ventas.")
        clases = clases_por_volumen(ventas)
        por_clave = {}
        for titulo, clase in clases.items():
            por_clave[normalizar(titulo)] = (titulo, clase, ventas[titulo])

        skus = list(SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo"))
        asignaciones, sin_ventas, usados = [], [], set()
        for sku in skus:
            clave = normalizar(sku.descripcion)
            if clave in por_clave:
                titulo, clase, piezas = por_clave[clave]
                usados.add(clave)
                asignaciones.append((sku, clase, piezas))
            else:
                sin_ventas.append(sku)
        sin_sku = [t for k, (t, _c, _p) in por_clave.items() if k not in usados]

        conteo = {c: sum(1 for _s, cl, _p in asignaciones if cl == c) for c in "ABC"}
        self.stdout.write(
            f"{len(ventas)} títulos en el reporte · {len(asignaciones)} SKUs empatados "
            f"(A {conteo['A']} · B {conteo['B']} · C {conteo['C']}) · {len(sin_ventas)} SKUs sin ventas → C"
        )
        for sku, clase, piezas in asignaciones:
            cambio = "" if sku.rotacion == clase else f"  (antes {sku.rotacion})"
            self.stdout.write(f"  {clase}  {sku.codigo:16} {piezas:5} · {sku.descripcion}{cambio}")
        for sku in sin_ventas:
            self.stdout.write(f"  C  {sku.codigo:16}     0 · {sku.descripcion} (sin ventas en el reporte)")
        if sin_sku:
            self.stdout.write(self.style.WARNING(f"{len(sin_sku)} título(s) del reporte sin SKU en Torre:"))
            for titulo in sin_sku:
                self.stdout.write(f"  ? {titulo}")
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING("Dry-run: nada escrito. Repite con --aplicar para asignar."))
            return
        with transaction.atomic():
            cambiados = 0
            for sku, clase, _piezas in asignaciones + [(s, "C", 0) for s in sin_ventas]:
                if sku.rotacion != clase:
                    SKU.objects.filter(pk=sku.pk).update(rotacion=clase)
                    cambiados += 1
            registrar_evento(
                "sku", "rotacion_desde_ventas", "rotacion_asignada", cliente=cliente,
                delta={"archivo": Path(opciones["archivo"]).name, "titulos": len(ventas),
                       "empatados": len(asignaciones), "sin_ventas": len(sin_ventas),
                       "sin_sku": sin_sku[:50], "clases": conteo, "cambiados": cambiados},
                motivo="Rotación A/B/C asignada desde el reporte de ventas por producto de Shopify.",
            )
        self.stdout.write(self.style.SUCCESS(f"{cambiados} SKU(s) con rotación nueva."))
