"""Link al pedido del portal en las órdenes de Shopify (Chema 2026-09-23).

Servicio al cliente trabaja desde el admin de Shopify con el "nombre" de la
orden; Torre no lo guarda: la orden lleva un metafield `torre.pedido_url`
(tipo url) con el link al pedido en el portal. La ingesta lo escribe sola en
cada orden nueva; este command cubre lo demás:

    manage.py shopify_metafield_torre --crear-definicion   # una vez por tienda: define y fija el metafield
    manage.py shopify_metafield_torre --backfill            # plan: pedidos existentes que llevarían el link
    manage.py shopify_metafield_torre --backfill --aplicar  # escribe el link en sus órdenes
    manage.py shopify_metafield_torre --backfill --aplicar --folio PED-00031

Reentrable: escribir dos veces deja el mismo valor; la definición ya creada
no es error.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.integraciones.models import Tienda
from apps.integraciones.services import crear_definicion_link, escribir_link_pedido, url_pedido_portal
from apps.pedidos.models import Pedido


class Command(BaseCommand):
    help = "Define el metafield torre.pedido_url en Shopify y/o escribe el link al portal en las órdenes de pedidos existentes."

    def add_arguments(self, parser):
        parser.add_argument("--crear-definicion", action="store_true",
                            help="Crea (y fija) la definición del metafield en cada tienda activa con token.")
        parser.add_argument("--backfill", action="store_true",
                            help="Recorre los pedidos con orden de Shopify; sin --aplicar solo imprime el plan.")
        parser.add_argument("--aplicar", action="store_true", help="Escribe el link (solo con --backfill).")
        parser.add_argument("--folio", default="", help="Limita el backfill a un pedido (PED-xxxxx).")

    def handle(self, *args, **options):
        if not (options["crear_definicion"] or options["backfill"]):
            raise CommandError("Indica --crear-definicion y/o --backfill.")
        if options["crear_definicion"]:
            self._crear_definicion()
        if options["backfill"]:
            self._backfill(options["aplicar"], options["folio"])

    def _crear_definicion(self):
        tiendas = Tienda.objects.filter(activo=True)
        if not tiendas.exists():
            self.stdout.write(self.style.WARNING("No hay tiendas activas."))
        for tienda in tiendas:
            resultado = crear_definicion_link(tienda)
            texto = {"creada": "definición creada y fijada", "existia": "la definición ya existía"}.get(
                resultado, "ERROR (ver SyncLog)",
            )
            estilo = self.style.ERROR if resultado == "error" else self.style.SUCCESS
            self.stdout.write(estilo(f"{tienda.dominio}: {texto}"))

    def _backfill(self, aplicar, folio):
        qs = Pedido.objects.filter(tienda__isnull=False).exclude(shopify_order_id="").select_related("tienda")
        if folio:
            qs = qs.filter(folio=folio)
        pedidos = list(qs.order_by("pk"))
        if not pedidos:
            self.stdout.write(self.style.WARNING("Sin pedidos con orden de Shopify que procesar."))
            return
        ok = errores = 0
        for pedido in pedidos:
            if not aplicar:
                self.stdout.write(f"{pedido.folio}: orden {pedido.shopify_order_id} → {url_pedido_portal(pedido)}")
                continue
            if escribir_link_pedido(pedido):
                ok += 1
            else:
                errores += 1
                self.stdout.write(self.style.ERROR(f"{pedido.folio}: no se escribió (ver SyncLog)"))
        if aplicar:
            self.stdout.write(self.style.SUCCESS(f"Link escrito en {ok} orden(es); {errores} con error."))
        else:
            self.stdout.write(f"{len(pedidos)} pedido(s) en el plan; corre con --aplicar para escribir.")
