"""Backfill de ids de fulfillment y eventos de avance en Shopify (2026-09-21).

Torre nunca guardó el id del fulfillment que creaba al firmar el manifiesto y
jamás escribió eventos de avance, así que en el admin de Shopify esos pedidos
se quedaron en "Tracking added". Para cada pedido con tienda y orden de
Shopify ya fulfilleado (RECOLECTADO en adelante, o PARCIALMENTE_DESPACHADO)
y sin ids guardados: consulta los fulfillments de la orden, empareja por
número de guía (fulfillment con la guía de UNA caja → Paquete.shopify_fulfillment_id;
sin caja, o con varias guías → Pedido.shopify_fulfillment_id) y reproduce el
historial en Shopify: CARRIER_PICKED_UP con la hora del manifiesto y luego
cada EventoGuia con estado canónico, con su hora del carrier
(integraciones.services.registrar_evento_fulfillment, que ya aplica el mapa
y la regla de DELIVERED para fulfillments de varias guías). Sin --aplicar
solo imprime el plan. Reentrable: un pedido que ya tiene id se salta.

    manage.py shopify_eventos_backfill                 # plan
    manage.py shopify_eventos_backfill --aplicar       # escribe ids y eventos
    manage.py shopify_eventos_backfill --folio PED-00031 --aplicar
"""
from django.core.management.base import BaseCommand

from apps.envios.models import Paquete
from apps.integraciones.services import (
    EVENTO_FULFILLMENT_POR_ESTADO,
    registrar_evento_fulfillment,
)
from apps.integraciones.shopify import ShopifyClient, ShopifyError
from apps.pedidos.models import Pedido

ESTADOS_FULFILLEADOS = (
    Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGADO, Pedido.ENTREGA_PRESUNTA,
    Pedido.PARCIALMENTE_DESPACHADO, Pedido.RETORNADO,
)


class Command(BaseCommand):
    help = "Recupera ids de fulfillment de Shopify para pedidos ya fulfilleados y reproduce sus eventos de avance."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe ids y eventos; sin él solo imprime el plan.")
        parser.add_argument("--folio", action="append", default=[], help="Solo estos folios (repetible).")

    def handle(self, *args, **options):
        aplicar = options["aplicar"]
        pedidos = (
            Pedido.objects.filter(tienda__isnull=False, estado__in=ESTADOS_FULFILLEADOS)
            .exclude(shopify_order_id="")
            .select_related("tienda", "cliente")
            .prefetch_related("paquetes", "guias__paquete", "guias__eventos")
            .order_by("pk")
        )
        if options["folio"]:
            pedidos = pedidos.filter(folio__in=options["folio"])
        totales = {"pedidos": 0, "con_id": 0, "eventos": 0, "saltados": 0}
        for pedido in pedidos:
            totales["pedidos"] += 1
            resultado = self._procesar(pedido, aplicar)
            totales[resultado] = totales.get(resultado, 0) + 1
        self.stdout.write(
            f"{'Aplicado' if aplicar else 'Simulación: nada cambió'} · pedidos revisados {totales['pedidos']}, "
            f"con id {totales['con_id']}, eventos mandados {totales['eventos']}, saltados {totales['saltados']}"
        )

    def _procesar(self, pedido, aplicar):
        cajas = list(pedido.paquetes.all())
        if pedido.shopify_fulfillment_id or any(c.shopify_fulfillment_id for c in cajas):
            self.stdout.write(f"{pedido.folio}: ya tiene id, se salta")
            return "saltados"
        tienda = pedido.tienda
        if not tienda.token:
            self.stdout.write(f"{pedido.folio}: tienda {tienda.dominio} sin token, se salta")
            return "saltados"
        try:
            fulfillments = ShopifyClient(tienda).fulfillments_de_orden(pedido.shopify_order_id)
        except ShopifyError as exc:
            self.stdout.write(f"{pedido.folio}: Shopify no respondió ({exc}), se salta")
            return "saltados"
        vivos = [(gid, st, nums) for gid, st, nums in fulfillments if gid and st.upper() != "CANCELLED"]
        if not vivos:
            self.stdout.write(f"{pedido.folio}: sin fulfillment en Shopify (¿lo fulfilleó otro?), se salta")
            return "saltados"
        guias = [g for g in pedido.guias.all() if g.es_activa]
        plan_cajas, plan_pedido = {}, ""
        for gid, _estado, numeros in vivos:
            de_este = [g for g in guias if g.numero and g.numero in numeros]
            if len(de_este) == 1 and de_este[0].paquete_id:
                plan_cajas[de_este[0].paquete_id] = gid
            elif not plan_pedido:
                plan_pedido = gid
        if not plan_cajas and not plan_pedido:
            self.stdout.write(f"{pedido.folio}: ningún fulfillment coincide con sus guías, se salta")
            return "saltados"
        self.stdout.write(
            f"{pedido.folio}: fulfillments {len(vivos)} → cajas {sorted(plan_cajas)} "
            f"{'+ pedido entero' if plan_pedido else ''}"
        )
        if not aplicar:
            for guia in guias:
                for estado, ts in self._historial(pedido, guia):
                    self.stdout.write(f"    {guia.numero}: {EVENTO_FULFILLMENT_POR_ESTADO[estado]} @ {ts:%d/%m %H:%M}")
            return "con_id"
        for paquete_id, gid in plan_cajas.items():
            Paquete.objects.filter(pk=paquete_id).update(shopify_fulfillment_id=gid)
        if plan_pedido:
            Pedido.objects.filter(pk=pedido.pk).update(shopify_fulfillment_id=plan_pedido)
        pedido = Pedido.objects.select_related("tienda", "cliente").get(pk=pedido.pk)
        mandados = 0
        for guia in pedido.guias.select_related("paquete").order_by("pk"):
            if not guia.es_activa:
                continue
            for estado, ts in self._historial(pedido, guia):
                if registrar_evento_fulfillment(pedido, guia, estado, ts=ts):
                    mandados += 1
        self.stdout.write(f"    {mandados} evento(s) mandados")
        return "eventos" if mandados else "con_id"

    @staticmethod
    def _historial(pedido, guia):
        """[(estado_guia, ts)] a reproducir: el manifiesto y luego lo que dijo el carrier."""
        historial = []
        if pedido.ts_recolectado:
            historial.append((Pedido.RECOLECTADO, pedido.ts_recolectado))
        for evento in guia.eventos.order_by("ts_carrier", "pk"):
            if evento.estado in EVENTO_FULFILLMENT_POR_ESTADO and evento.estado != Pedido.RECOLECTADO:
                historial.append((evento.estado, evento.ts_carrier or evento.ts_visto))
        if (
            guia.estado in EVENTO_FULFILLMENT_POR_ESTADO
            and guia.estado != Pedido.RECOLECTADO
            and not any(e == guia.estado for e, _ in historial)
        ):
            historial.append((guia.estado, guia.ts_ultimo_movimiento or pedido.ts_recolectado))
        return historial
