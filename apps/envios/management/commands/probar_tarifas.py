"""Prueba de tarifas contra la API real de Envia.com (modo cotizar: no genera guías).

Corre la matriz de destinos × pedidos típicos de Cervecería Colima, muestra qué
elegiría el optimizador de división y si cumple la meta de $115 por envío.

    manage.py probar_tarifas            # matriz completa
    manage.py probar_tarifas --cp 06600 # un solo destino
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.envios import cotizador

DESTINOS = [
    ("06600", "CDMX"), ("44100", "Guadalajara"), ("64000", "Monterrey"),
    ("72000", "Puebla"), ("76000", "Querétaro"), ("91700", "Veracruz"),
    ("97000", "Mérida"), ("22000", "Tijuana"),
]

# Pedidos típicos: (nombre, [pesos unitarios kg]) — el optimizador decide la división.
PEDIDOS = [
    ("Six (4 kg)", [4]),
    ("Caja 12 (8 kg)", [8]),
    ("Caja 12 + six (12 kg)", [8, 4]),
    ("2 cajas 12 (16 kg)", [8, 8]),
    ("2 cajas + six (20 kg)", [8, 8, 4]),
    ("3 cajas 12 (24 kg)", [8, 8, 8]),
]


class Command(BaseCommand):
    help = "Cotiza la matriz Colima→nacional contra la API real y evalúa la meta de $115"

    def add_arguments(self, parser):
        parser.add_argument("--cp", help="Probar solo este CP destino")

    def _plan_optimo(self, cp, unidades):
        """Réplica de la lógica del planificador sobre pesos sueltos (sin pedido)."""
        max_kg = Decimal(str(settings.TORRE["MAX_PESO_ENVIO_KG"]))
        unidades_t = [(None, Decimal(str(u))) for u in sorted(unidades, reverse=True)]
        candidatas = cotizador._particiones_candidatas(unidades_t, max_kg)
        mejor = None
        for bins in candidatas:
            total, opciones = Decimal(0), []
            valida = True
            for b in bins:
                opcion = cotizador.mejor_opcion(cp, cotizador._peso_bin(b))
                if opcion is None:
                    valida = False
                    break
                total += opcion["precio"]
                opciones.append((cotizador._peso_bin(b), opcion))
            if valida and (mejor is None or (total, len(bins)) < (mejor[0], len(mejor[1]))):
                mejor = (total, opciones)
        return mejor

    def handle(self, *args, **options):
        modo = "API REAL" if (settings.ENVIA_API_KEY and settings.ENVIA_MODO != "off") else "MOCK"
        meta = Decimal(str(settings.TORRE["TARIFA_OBJETIVO_MXN"]))
        w = self.stdout.write
        w(f"\nTarifas {modo} · origen Colima 28017 · meta ≤ ${meta}/envío · tope 20 kg/paquete\n")

        destinos = [(options["cp"], options["cp"])] if options.get("cp") else DESTINOS
        fuera_de_meta = []

        for cp, nombre in destinos:
            w(f"\n── {nombre} ({cp}) " + "─" * 40)
            for etiqueta, unidades in PEDIDOS:
                plan = self._plan_optimo(cp, unidades)
                if plan is None:
                    w(f"  {etiqueta:<26} SIN COBERTURA")
                    continue
                total, opciones = plan
                # La meta de Diego es POR ENVÍO (cada guía), no por pedido:
                # dividir existe justo para que cada paquete quede ≤ $115.
                excedidos = [op for _, op in opciones if op["precio"] > meta]
                division = " + ".join(
                    f"{peso}kg {op['carrier']} ${op['precio']:.0f}"
                    f"{'⚠' if op['precio'] > meta else ''}"
                    for peso, op in opciones
                )
                cumple = "OK " if not excedidos else "❌ "
                if excedidos:
                    fuera_de_meta.append((nombre, etiqueta, total))
                w(f"  {cumple}{etiqueta:<26} total ${total:>7.2f}  → {division}")

        w("\n" + "═" * 60)
        if fuera_de_meta:
            w(f"Combinaciones con algún ENVÍO fuera de meta (${meta}): {len(fuera_de_meta)}.")
            w("La meta por envío la cumple PuntoPost ($86-91, ≤10 kg) en zonas metro;")
            w("donde no hay PuntoPost (Mérida, Tijuana...), el mínimo real es ~$155 y el")
            w("paquete queda marcado fuera_de_meta=True, visible en Mesa y portal.")
        else:
            w("TODOS los envíos cumplen la meta por guía.")
