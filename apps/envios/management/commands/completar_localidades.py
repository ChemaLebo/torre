"""Completa el catálogo de localidades por CP (Chema 2026-10-15).

    python manage.py completar_localidades            # dry-run: qué CP faltan
    python manage.py completar_localidades --aplicar  # consulta a envia y guarda

`envios.LocalidadCP` (localidad, municipio y estado por CP según los geocodes
de envia) se llenaba solo cuando un flujo lo necesitaba (iMile), así que el
reporte "Pedidos por ciudad" agrupaba la mayoría de los CP por el texto que
tecleó el comprador en Shopify. Este comando toma los CP distintos de TODOS
los pedidos (el del pedido o el `zip` de la dirección, a 5 dígitos) y, para
los que no tienen catálogo, pregunta a envia UNA vez por CP
(`localidades.localidad_por_cp`: solo lectura en envia; un CP que el catálogo
no conoce se guarda vacío para no volver a preguntar; sin respuesta no se
guarda nada y se reintenta en la siguiente corrida). Sin llave o con
ENVIA_MODO=off avisa y no hace nada. No toca pedidos, guías ni inventario.
Idempotente: la segunda corrida solo consulta lo que no respondió.
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.envios.localidades import localidad_por_cp
from apps.envios.models import LocalidadCP
from apps.pedidos.models import Pedido

PAUSA_SEGUNDOS = 0.2  # entre consultas, por cortesía con la API de envia


def cp_de(pedido):
    """CP de destino a 5 dígitos ("" si no hay): el del pedido o el de la dirección."""
    crudo = pedido.cp or (pedido.direccion or {}).get("zip") or ""
    digitos = "".join(ch for ch in str(crudo) if ch.isdigit())
    return digitos.zfill(5) if 0 < len(digitos) <= 5 else ""


def cps_sin_catalogo():
    """([CP sin catálogo, ordenados], total de CP distintos en los pedidos)."""
    cps = set()
    for pedido in Pedido.objects.only("cp", "direccion").iterator():
        cp = cp_de(pedido)
        if cp:
            cps.add(cp)
    conocidos = set(LocalidadCP.objects.filter(cp__in=cps).values_list("cp", flat=True))
    return sorted(cps - conocidos), len(cps)


class Command(BaseCommand):
    help = "Consulta a envia la localidad, municipio y estado de los CP de los pedidos que aún no están en el catálogo. Dry-run sin --aplicar."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Consulta a envia y guarda (sin esto solo lista los CP que faltan).")

    def handle(self, *args, **opciones):
        faltan, total = cps_sin_catalogo()
        self.stdout.write(f"{total} CP distintos en los pedidos; {total - len(faltan)} ya con catálogo; {len(faltan)} por consultar.")
        if not faltan:
            self.stdout.write(self.style.SUCCESS("Nada que consultar: todos los CP tienen catálogo."))
            return
        self.stdout.write("  " + ", ".join(faltan))
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING("Dry-run: nada consultado ni escrito. Repite con --aplicar."))
            return
        if not getattr(settings, "ENVIA_API_KEY", "") or getattr(settings, "ENVIA_MODO", "off") == "off":
            self.stdout.write(self.style.WARNING("Sin ENVIA_API_KEY o con ENVIA_MODO=off no se puede consultar a envia: nada que hacer."))
            return
        resueltos, desconocidos, sin_respuesta = 0, 0, []
        for i, cp in enumerate(faltan):
            if i:
                time.sleep(PAUSA_SEGUNDOS)
            fila = localidad_por_cp(cp)
            if fila is None:
                sin_respuesta.append(cp)
                self.stdout.write(f"  {cp}: sin respuesta de envia (no se guarda; se reintenta en la próxima corrida)")
            elif fila.localidad or fila.municipio:
                resueltos += 1
                self.stdout.write(f"  {cp}: {fila.localidad or '-'} · {fila.municipio or '-'} · {fila.estado or '--'}")
            else:
                desconocidos += 1
                self.stdout.write(f"  {cp}: envia no conoce este CP (se guarda vacío para no volver a preguntar)")
        self.stdout.write(self.style.SUCCESS(
            f"{resueltos} CP con localidad; {desconocidos} desconocidos para envia; {len(sin_respuesta)} sin respuesta."
        ))
        if sin_respuesta:
            self.stdout.write(self.style.ERROR("Sin respuesta: " + ", ".join(sin_respuesta)))
