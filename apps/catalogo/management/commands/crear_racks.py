"""Crea las ubicaciones de los racks con el formato de septiembre 2026:
<PREFIJO>-<rack>-<I|D>-<F|B>-<piso>, con el piso de hasta arriba como reserva
(RES-1-D-F-4) y los demás como picking (PIC-1-I-F-1).

Idempotente y sin tocar inventario: solo crea las ubicaciones que faltan
(get_or_create por código) con sus medidas y prioridad (catalogo.services.
medidas_de_codigo); las existentes, con su stock, se dejan como están salvo
que se pida --medidas (les refresca medidas y prioridad). Dry-run por
default; --aplicar escribe.

    python manage.py crear_racks --racks 4 --pisos 4 --aplicar
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalogo.models import Ubicacion
from apps.catalogo.services import aplicar_medidas, medidas_de_codigo

LADOS = (("I", "izquierda"), ("D", "derecha"))
FRENTES = (("F", "frente"), ("B", "atrás"))


def codigos_racks(racks, pisos, piso_reserva, prefijo_picking="PIC", prefijo_reserva="RES"):
    """[(código, tipo)] de todas las combinaciones rack × lado × frente × piso."""
    combinaciones = []
    for rack in range(1, racks + 1):
        for lado, _ in LADOS:
            for frente, _ in FRENTES:
                for piso in range(1, pisos + 1):
                    if piso == piso_reserva:
                        combinaciones.append((f"{prefijo_reserva}-{rack}-{lado}-{frente}-{piso}", Ubicacion.RESERVA))
                    else:
                        combinaciones.append((f"{prefijo_picking}-{rack}-{lado}-{frente}-{piso}", Ubicacion.PICKING))
    return combinaciones


class Command(BaseCommand):
    help = "Crea las ubicaciones PIC/RES-<rack>-<I|D>-<F|B>-<piso> que falten (dry-run sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--racks", type=int, default=4, help="Número de racks (default 4).")
        parser.add_argument("--pisos", type=int, default=4, help="Pisos por rack (default 4).")
        parser.add_argument(
            "--piso-reserva", type=int, default=None,
            help="Piso que es reserva (RES); default: el de hasta arriba. 0 = ninguno.",
        )
        parser.add_argument("--prefijo-picking", default="PIC")
        parser.add_argument("--prefijo-reserva", default="RES")
        parser.add_argument("--aplicar", action="store_true", help="Escribe en la base (sin esto solo muestra).")
        parser.add_argument(
            "--medidas", action="store_true",
            help="Además, (re)escribe medidas y prioridad en las ubicaciones existentes con el patrón.",
        )

    def handle(self, *args, **opciones):
        piso_reserva = opciones["piso_reserva"]
        if piso_reserva is None:
            piso_reserva = opciones["pisos"]
        combinaciones = codigos_racks(
            opciones["racks"], opciones["pisos"], piso_reserva,
            opciones["prefijo_picking"], opciones["prefijo_reserva"],
        )
        existentes = set(Ubicacion.objects.filter(codigo__in=[c for c, _ in combinaciones]).values_list("codigo", flat=True))
        nuevas = [(c, t) for c, t in combinaciones if c not in existentes]
        self.stdout.write(f"{len(combinaciones)} combinaciones · {len(existentes)} ya existen · {len(nuevas)} por crear")
        for codigo, tipo in nuevas:
            self.stdout.write(f"  + {codigo} ({tipo})")
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING("Dry-run: nada escrito. Repite con --aplicar para crearlas."))
            return
        with transaction.atomic():
            for codigo, tipo in nuevas:
                Ubicacion.objects.get_or_create(
                    codigo=codigo, defaults={"tipo": tipo, "activo": True, **(medidas_de_codigo(codigo) or {})},
                )
            refrescadas = 0
            if opciones["medidas"]:
                refrescadas = aplicar_medidas(Ubicacion.objects.filter(codigo__in=list(existentes)))
        self.stdout.write(self.style.SUCCESS(
            f"{len(nuevas)} ubicación(es) creada(s) con medidas y prioridad. "
            f"Las existentes y su inventario no se tocaron"
            + (f" (medidas refrescadas en {refrescadas})." if opciones["medidas"] else ".")
        ))
