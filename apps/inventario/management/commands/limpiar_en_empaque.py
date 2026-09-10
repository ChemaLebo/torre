"""Limpieza única del saldo EN_EMPAQUE huérfano.

Hasta 2026-09-09 la cancelación de un pedido ya empacado reingresaba lo
pickeado a cuarentena sin sacarlo de EN_EMPAQUE, así que cada cancelación
post-empaque dejaba unidades contadas dos veces. Este comando compara, por
SKU, el EN_EMPAQUE real contra lo que respalda: Σ cantidad_pickeada de las
líneas (no kit) de pedidos EMPACADO / GUIA_GENERADA (y CANCELACION_PENDIENTE
ya empacados), y retira el excedente con un movimiento AJUSTE auditado.

Sin --aplicar solo imprime el diagnóstico. Idempotente: tras aplicar, el
excedente es cero. Correr una vez después de desplegar el flujo de reingreso.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from apps.core.services import registrar_evento
from apps.inventario.models import Movimiento, Saldo
from apps.inventario.services import _restar


def excedentes_en_empaque():
    """[(sku, en_empaque_real, respaldado, excedente)] solo con excedente > 0."""
    from apps.pedidos.models import LineaPedido, Pedido

    respaldo = dict(
        LineaPedido.objects.filter(
            sku__es_kit=False,
            pedido__estado__in=[Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.CANCELACION_PENDIENTE],
            pedido__ts_empacado__isnull=False,
        )
        .values_list("sku_id")
        .annotate(t=Sum("cantidad_pickeada"))
        .values_list("sku_id", "t")
    )
    filas = (
        Saldo.objects.filter(estado=Saldo.EN_EMPAQUE, cantidad__gt=0)
        .values("sku_id").annotate(t=Sum("cantidad")).order_by("sku_id")
    )
    from apps.catalogo.models import SKU

    resultado = []
    for fila in filas:
        real = fila["t"] or 0
        respaldado = respaldo.get(fila["sku_id"], 0) or 0
        if real > respaldado:
            resultado.append((SKU.objects.get(pk=fila["sku_id"]), real, respaldado, real - respaldado))
    return resultado


class Command(BaseCommand):
    help = "Diagnostica (y con --aplicar retira) el saldo EN_EMPAQUE sin pedido que lo respalde."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Retira el excedente (default: solo diagnóstico).")

    def handle(self, *args, **opciones):
        excedentes = excedentes_en_empaque()
        if not excedentes:
            self.stdout.write("EN_EMPAQUE cuadra con los pedidos empacados: nada que limpiar.")
            return
        for sku, real, respaldado, exceso in excedentes:
            self.stdout.write(f"{sku.codigo}: en_empaque={real} respaldado={respaldado} excedente={exceso}")
        if not opciones["aplicar"]:
            self.stdout.write("Diagnóstico solamente. Corre con --aplicar para retirar el excedente.")
            return
        for sku, real, respaldado, exceso in excedentes:
            with transaction.atomic():
                filas = list(Saldo.objects.select_for_update().filter(sku=sku, estado=Saldo.EN_EMPAQUE))
                _restar(sorted(filas, key=lambda f: f.pk), exceso)
                Movimiento.objects.create(
                    sku=sku, tipo=Movimiento.AJUSTE, delta=-exceso,
                    estado_origen=Saldo.EN_EMPAQUE, estado_destino="",
                    referencia="LIMPIEZA-EMPAQUE", actor="limpiar_en_empaque",
                )
                registrar_evento(
                    "sku", sku.codigo, "limpieza_en_empaque", cliente=sku.cliente,
                    delta={"en_empaque": real, "respaldado": respaldado, "retirado": exceso},
                    motivo="Saldo EN_EMPAQUE huérfano de cancelaciones post-empaque anteriores al reingreso.",
                )
            self.stdout.write(f"  → {sku.codigo}: retiradas {exceso} de en_empaque.")
        self.stdout.write(f"Listo: {len(excedentes)} SKU(s) limpiados.")
