"""Regularizar salidas sin registro (Chema 2026-10-14, pendiente 96).

Un pedido puede llegar a la calle sin pasar por Salida: el carrier escanea la
caja y el rastreo lo pasa de GUIA_GENERADA a EN_TRANSITO (transición
permitida), o la pantalla de Salida se trabó antes de registrarlo (bug del
14 de octubre). Entonces el pedido viaja, pero en Torre sus piezas siguen "en
empaque", la línea tiene `cantidad_despachada` 0 y la caja sigue EMPACADA.

Este comando encuentra esos pedidos (estados de la calle con pickeadas >
despachadas) y, con --aplicar, hace la salida que faltó. El kardex de "en
empaque" no sabe de qué pedido es cada pieza, así que solo se despacha del
kardex el EXCEDENTE por producto: lo que hay en empaque menos lo que
respaldan los pedidos que siguen en bodega (en picking, empacados, con guía,
parciales); el resto (piezas que una limpieza anterior ya retiró) se
regulariza solo en el pedido, sin inventar un descuento. Las cajas
planeadas/en empaque/empacadas pasan a DESPACHADO y queda el evento
`salida_regularizada`. No toca Shopify ni crea manifiesto. Idempotente.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.services import registrar_evento
from apps.envios.models import Paquete
from apps.inventario.models import Saldo
from apps.inventario.services import despachar
from apps.pedidos.models import LineaPedido, Pedido

ESTADOS_EN_LA_CALLE = (Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGA_PRESUNTA, Pedido.ENTREGADO, Pedido.RETORNADO)
ESTADOS_EN_BODEGA = (
    Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO, Pedido.CANCELACION_PENDIENTE,
)


def pendientes_de_salida():
    """[(pedido, [líneas con pickeadas > despachadas])] en estados de la calle, del más viejo al más nuevo."""
    lineas = (
        LineaPedido.objects.select_related("pedido", "sku")
        .filter(pedido__estado__in=ESTADOS_EN_LA_CALLE, sku__es_kit=False)
        .order_by("pedido__creado", "pedido__pk", "pk")
    )
    por_pedido = {}
    for linea in lineas:
        if linea.cantidad_pickeada > linea.cantidad_despachada:
            por_pedido.setdefault(linea.pedido_id, (linea.pedido, []))[1].append(linea)
    return list(por_pedido.values())


def excedente_en_empaque():
    """{sku_id: piezas en empaque que NO respalda ningún pedido en bodega}:
    lo único que se puede despachar del kardex sin quitarle piezas a un
    pedido que sigue aquí."""
    en_empaque = dict(
        Saldo.objects.filter(estado=Saldo.EN_EMPAQUE, cantidad__gt=0).values_list("sku_id").annotate(t=Sum("cantidad"))
        .values_list("sku_id", "t")
    )
    respaldo = defaultdict(int)
    for linea in LineaPedido.objects.filter(pedido__estado__in=ESTADOS_EN_BODEGA, sku__es_kit=False):
        respaldo[linea.sku_id] += max(0, linea.cantidad_pickeada - linea.cantidad_despachada)
    return {sku_id: max(0, total - respaldo.get(sku_id, 0)) for sku_id, total in en_empaque.items()}


def plan_de_regularizacion():
    """[(pedido, [(línea, del_kardex, solo_marcar)], cajas)] repartiendo el
    excedente por producto del pedido más viejo al más nuevo."""
    excedente = excedente_en_empaque()
    plan = []
    for pedido, lineas in pendientes_de_salida():
        reparto = []
        for linea in lineas:
            por_despachar = linea.cantidad_pickeada - linea.cantidad_despachada
            del_kardex = min(por_despachar, excedente.get(linea.sku_id, 0))
            excedente[linea.sku_id] = excedente.get(linea.sku_id, 0) - del_kardex
            reparto.append((linea, del_kardex, por_despachar - del_kardex))
        cajas = list(pedido.paquetes.exclude(estado=Paquete.DESPACHADO).order_by("numero"))
        plan.append((pedido, reparto, cajas))
    return plan


def _despachar_caja(caja, pedido):
    """Caja planeada / en empaque / empacada → DESPACHADO por la máquina de estados de Paquete."""
    motivo = f"Salida regularizada de {pedido.folio}: la caja ya iba en la calle."
    if caja.estado in (Paquete.PLANEADO, Paquete.EN_EMPAQUE):
        caja.transicionar(Paquete.EMPACADO, motivo=motivo)
    if caja.estado == Paquete.EMPACADO:
        caja.transicionar(Paquete.DESPACHADO, motivo=motivo)


class Command(BaseCommand):
    help = "Pedidos en la calle cuyas piezas siguen 'en empaque' en Torre (salieron sin registrar la salida). Con --aplicar hace la salida que faltó."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Despacha en el kardex (solo el excedente) y marca líneas y cajas (default: solo diagnóstico).")

    def handle(self, *args, **opciones):
        plan = plan_de_regularizacion()
        if not plan:
            self.stdout.write(self.style.SUCCESS("Nada que regularizar: todo lo pickeado de los pedidos en la calle ya salió del kardex."))
            return
        total_kardex = total_marcar = 0
        for pedido, reparto, cajas in plan:
            partes = []
            for linea, del_kardex, solo_marcar in reparto:
                total_kardex += del_kardex
                total_marcar += solo_marcar
                partes.append(f"{linea.sku.codigo} pickeadas {linea.cantidad_pickeada}: del kardex {del_kardex}, solo marcar {solo_marcar}")
            self.stdout.write(
                f"{pedido.folio} ({pedido.get_estado_display()}): " + "; ".join(partes)
                + (f" · cajas: {', '.join(f'{c.numero} ({c.get_estado_display().lower()})' for c in cajas)} → despachadas" if cajas else "")
            )
        self.stdout.write(f"Totales: {total_kardex} pieza(s) salen del kardex (excedente de 'en empaque'); {total_marcar} solo se marcan en el pedido (el kardex ya no las tenía).")
        if not opciones["aplicar"]:
            self.stdout.write(self.style.WARNING(f"{len(plan)} pedido(s) por regularizar. Corre con --aplicar para hacer la salida que faltó."))
            return
        hechos, errores = 0, []
        for pedido, reparto, cajas in plan:
            try:
                with transaction.atomic():
                    detalle = []
                    for linea, del_kardex, solo_marcar in reparto:
                        if del_kardex:
                            despachar(linea.sku, del_kardex, pedido.folio)
                        linea.cantidad_despachada = linea.cantidad_pickeada
                        linea.save(update_fields=["cantidad_despachada"])
                        detalle.append({"sku": linea.sku.codigo, "kardex": del_kardex, "solo_marcadas": solo_marcar})
                    for caja in cajas:
                        _despachar_caja(caja, pedido)
                    if pedido.ts_recolectado is None:
                        pedido.ts_recolectado = timezone.now()
                        pedido.save(update_fields=["ts_recolectado"])
                    registrar_evento(
                        "pedido", pedido.pk, "salida_regularizada", cliente=pedido.cliente,
                        delta={"lineas": detalle, "cajas": [c.numero for c in cajas], "estado": pedido.estado},
                        motivo="Salió sin registrar la salida (rastreo del carrier o Salida trabada): se despacha del kardex solo el excedente de 'en empaque'; el resto ya se había limpiado.",
                    )
            except ValueError as exc:
                errores.append(f"{pedido.folio}: {exc}")
                continue
            hechos += 1
            self.stdout.write(self.style.SUCCESS(f"{pedido.folio}: regularizado."))
        for error in errores:
            self.stdout.write(self.style.ERROR(error))
        self.stdout.write(self.style.SUCCESS(f"{hechos} pedido(s) regularizado(s); {len(errores)} con error."))
