"""Entrega en mano / recolección en bodega: el pedido sale sin guía y queda
ENTREGADO, con fulfillment en Shopify sin rastreo (pedidos.entregar_sin_guia).
Se corre por consola con el folio, a propósito: no vive en Mesa (Chema
2026-09-21). Sin --aplicar solo muestra qué haría."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.pedidos.models import Pedido
from apps.pedidos.services import entregar_sin_guia


class Command(BaseCommand):
    help = (
        "Entrega un pedido en bodega sin guía: confirma el pick que falte, despacha el "
        "stock, cierra las cajas, lo deja ENTREGADO y libre del operador, y marca el "
        "fulfillment en Shopify sin rastreo. Sin --aplicar es simulación."
    )

    def add_arguments(self, parser):
        parser.add_argument("folio", help="Folio del pedido (PED-00029) o su id.")
        parser.add_argument("--recibio", default="", help="Quién recibió el pedido en bodega.")
        parser.add_argument("--motivo", default="", help="Motivo para la auditoría.")
        parser.add_argument("--usuario", default="", help="Username que firma la entrega (default: consola).")
        parser.add_argument("--aplicar", action="store_true", help="Ejecuta; sin la bandera solo muestra qué haría.")

    def handle(self, *args, **options):
        folio = options["folio"].strip()
        pedido = Pedido.objects.filter(folio=folio).first()
        if pedido is None and folio.isdigit():
            pedido = Pedido.objects.filter(pk=int(folio)).first()
        if pedido is None:
            raise CommandError(f"No existe el pedido {folio}.")
        self.stdout.write(f"{pedido.folio} · {pedido.cliente.nombre} · {pedido.get_estado_display()}")
        self.stdout.write(f"  operador: {pedido.asignado_a.username if pedido.asignado_a else '—'}")
        self.stdout.write(f"  shopify: {pedido.shopify_order_id or 'no es de Shopify'}")
        for linea in pedido.lineas.select_related("sku"):
            self.stdout.write(
                f"  {linea.sku.codigo:16} pide {linea.cantidad} · pickeadas {linea.cantidad_pickeada}"
                f" · reservada {'sí' if linea.reservada else 'NO'}{' · kit' if linea.sku.es_kit else ''}"
            )
        for caja in pedido.paquetes.order_by("numero"):
            self.stdout.write(f"  caja {caja.numero}: {caja.estado} · {caja.carrier}")
        if not options["aplicar"]:
            self.stdout.write(self.style.WARNING("Simulación: nada cambió. Repite con --aplicar para entregar."))
            return
        actor = "consola"
        if options["usuario"]:
            actor = get_user_model().objects.filter(username=options["usuario"]).first()
            if actor is None:
                raise CommandError(f"No existe el usuario {options['usuario']}.")
        try:
            pedido = entregar_sin_guia(pedido, actor, recibio=options["recibio"], motivo=options["motivo"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"{pedido.folio} entregado sin guía: stock despachado, cajas cerradas, "
            "operador liberado. Fulfillment en Shopify: revisa Mesa → Salud de sync."
        ))
