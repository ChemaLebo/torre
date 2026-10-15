"""Regularizar salidas sin registro (Chema 2026-10-14, pendiente 96).

Un pedido puede llegar a la calle sin pasar por Salida: el carrier escanea la
caja y el rastreo lo pasa de GUIA_GENERADA a EN_TRANSITO (transición
permitida), o la pantalla de Salida se trabó antes de registrarlo (bug del
14 de octubre). Entonces el pedido viaja, pero en Torre sus piezas siguen "en
empaque", la línea tiene `cantidad_despachada` 0 y la caja sigue EMPACADA.
Este comando encuentra esos pedidos (estados de la calle con pickeadas >
despachadas) y, con --aplicar, hace la salida que faltó: despacha en el
kardex lo pickeado que no salió, estampa `cantidad_despachada`, pasa las
cajas EMPACADAS a DESPACHADO y deja el evento `salida_regularizada`. No toca
Shopify ni crea manifiesto. Idempotente: después de aplicar no queda nada.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.services import registrar_evento
from apps.envios.models import Paquete
from apps.inventario.services import despachar
from apps.pedidos.models import LineaPedido, Pedido

ESTADOS_EN_LA_CALLE = (Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGA_PRESUNTA, Pedido.ENTREGADO, Pedido.RETORNADO)


def pendientes_de_salida():
    """[(pedido, [líneas con pickeadas > despachadas])] en estados de la calle."""
    lineas = (
        LineaPedido.objects.select_related("pedido", "sku")
        .filter(pedido__estado__in=ESTADOS_EN_LA_CALLE, sku__es_kit=False)
        .order_by("pedido__creado", "pk")
    )
    por_pedido = {}
    for linea in lineas:
        if linea.cantidad_pickeada > linea.cantidad_despachada:
            por_pedido.setdefault(linea.pedido_id, (linea.pedido, []))[1].append(linea)
    return list(por_pedido.values())


class Command(BaseCommand):
    help = "Pedidos en la calle cuyas piezas siguen 'en empaque' en Torre (salieron sin registrar la salida). Con --aplicar hace la salida que faltó."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Despacha en el kardex y marca las cajas (default: solo diagnóstico).")

    def handle(self, *args, **opciones):
        casos = pendientes_de_salida()
        if not casos:
            self.stdout.write(self.style.SUCCESS("Nada que regularizar: todo lo pickeado de los pedidos en la calle ya salió del kardex."))
            return
        for pedido, lineas in casos:
            cajas = [c for c in pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero")]
            self.stdout.write(
                f"{pedido.folio} ({pedido.get_estado_display()}): "
                + "; ".join(f"{l.sku.codigo} pickeadas {l.cantidad_pickeada}, despachadas {l.cantidad_despachada}" for l in lineas)
                + (f" · cajas sin despachar: {', '.join(f'{c.numero} ({c.get_estado_display().lower()})' for c in cajas)}" if cajas else "")
            )
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING(f"{len(casos)} pedido(s) por regularizar. Corre con --aplicar para hacer la salida que faltó."))
            return
        hechos, errores = 0, []
        for pedido, lineas in casos:
            try:
                with transaction.atomic():
                    detalle = []
                    for linea in lineas:
                        por_despachar = linea.cantidad_pickeada - linea.cantidad_despachada
                        despachar(linea.sku, por_despachar, pedido.folio)
                        linea.cantidad_despachada = linea.cantidad_pickeada
                        linea.save(update_fields=["cantidad_despachada"])
                        detalle.append({"sku": linea.sku.codigo, "despachadas": por_despachar})
                    cajas = []
                    for caja in pedido.paquetes.filter(estado=Paquete.EMPACADO).order_by("numero"):
                        caja.transicionar(Paquete.DESPACHADO, motivo=f"Salida regularizada de {pedido.folio}: la caja ya iba en la calle.")
                        cajas.append(caja.numero)
                    if pedido.ts_recolectado is None:
                        pedido.ts_recolectado = timezone.now()
                        pedido.save(update_fields=["ts_recolectado"])
                    registrar_evento(
                        "pedido", pedido.pk, "salida_regularizada", cliente=pedido.cliente,
                        delta={"lineas": detalle, "cajas": cajas, "estado": pedido.estado},
                        motivo="Salió sin registrar la salida (rastreo del carrier o Salida trabada): se despacha en el kardex lo pickeado.",
                    )
            except ValueError as exc:
                errores.append(f"{pedido.folio}: {exc}")
                continue
            hechos += 1
            self.stdout.write(self.style.SUCCESS(f"{pedido.folio}: regularizado."))
        for error in errores:
            self.stdout.write(self.style.ERROR(error))
        self.stdout.write(self.style.SUCCESS(f"{hechos} pedido(s) regularizado(s); {len(errores)} con error."))
