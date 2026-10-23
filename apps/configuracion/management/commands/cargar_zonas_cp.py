"""Carga el catálogo de zonas de cobro por CP en Configuración de sistema.

    python manage.py cargar_zonas_cp                      # origen WOP + rangos de config/zonas_cp.csv
    python manage.py cargar_zonas_cp --archivo zonas.csv  # otro archivo (cp_desde,cp_hasta,zona[,ciudad])
    python manage.py cargar_zonas_cp --archivo zonas.csv --reemplazar

Idempotente: crea el origen principal (WOP, el ORIGEN_DEFAULT de los adapters)
solo si no hay ninguno, y carga los rangos solo si ese origen no tiene; con
--reemplazar borra los suyos y vuelve a cargar. Todo en una transacción; una
fila inválida (CP de 5 dígitos, desde ≤ hasta, sin traslapes) aborta sin
escribir. Se corre una vez después del deploy; después, el admin.
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.configuracion.models import Origen, ZonaCP
from apps.finanzas import zonas


def origen_wop():
    """El origen principal; si no existe, nace de ORIGEN_DEFAULT (bodega Local 380 E)."""
    origen = Origen.objects.filter(principal=True).first()
    if origen is not None:
        return origen, False
    from apps.envios.adapters import ORIGEN_DEFAULT  # lazy: el remitente de los adapters

    d = ORIGEN_DEFAULT
    origen = Origen(
        nombre=d.get("name") or "WOP Fulfillment", empresa=d.get("company") or "", calle=d.get("street") or "",
        numero=d.get("number") or "", colonia=d.get("district") or "", ciudad=d.get("city") or "",
        estado=d.get("state") or "", cp=d.get("postalCode") or "", telefono=d.get("phone") or "",
        correo=d.get("email") or "", principal=True, activo=True,
    )
    origen.full_clean()
    origen.save()
    return origen, True


class Command(BaseCommand):
    help = "Crea el origen WOP si falta y carga los rangos de zona por CP desde un CSV (config/zonas_cp.csv por default)."

    def add_arguments(self, parser):
        parser.add_argument("--archivo", help="CSV con cp_desde, cp_hasta, zona y opcionalmente ciudad (default: config/zonas_cp.csv)")
        parser.add_argument("--reemplazar", action="store_true", help="Borra los rangos del origen y carga los del archivo.")

    def handle(self, *args, **opciones):
        filas = zonas.rangos_de_archivo(opciones.get("archivo"))
        if not filas:
            raise CommandError("El archivo no trae rangos válidos (columnas cp_desde, cp_hasta, zona).")
        with transaction.atomic():
            origen, nuevo = origen_wop()
            if nuevo:
                self.stdout.write(f"Origen principal creado: {origen}")
            existentes = ZonaCP.objects.filter(origen=origen)
            if existentes.exists() and not opciones["reemplazar"]:
                self.stdout.write(self.style.WARNING(
                    f"{origen.nombre} ya tiene {existentes.count()} rango(s); nada que hacer (usa --reemplazar para cargar de nuevo)."
                ))
                return
            borrados = existentes.count()
            existentes.delete()
            for desde, hasta, zona, ciudad in filas:
                rango = ZonaCP(origen=origen, cp_desde=f"{desde:05d}", cp_hasta=f"{hasta:05d}", zona=zona, ciudad=ciudad)
                try:
                    rango.full_clean()
                except ValidationError as exc:
                    raise CommandError(f"Rango {desde:05d}-{hasta:05d}: {'; '.join(exc.messages)}") from exc
                rango.save()
        zonas.recargar()
        self.stdout.write(self.style.SUCCESS(
            f"{len(filas)} rango(s) cargados para {origen.nombre}" + (f" ({borrados} reemplazados)" if borrados else "") + "."
        ))
