"""LineaPedido.precio_unitario: precio de venta real de Shopify (line_items[].price)
para el reporte de ventas. Backfill desde el último webhook orders/* de cada
pedido (WebhookEvento.payload), empatando por SKU; pedidos manuales y líneas
sin item quedan en null."""
from decimal import Decimal, InvalidOperation

from django.db import migrations, models


def rellenar_precios(apps, schema_editor):
    Pedido = apps.get_model("pedidos", "Pedido")
    LineaPedido = apps.get_model("pedidos", "LineaPedido")
    WebhookEvento = apps.get_model("integraciones", "WebhookEvento")
    pedidos = Pedido.objects.exclude(origen="manual").exclude(tienda__isnull=True).exclude(shopify_order_id="")
    for pedido in pedidos.iterator():
        if not str(pedido.shopify_order_id).isdigit():
            continue
        evento = (
            WebhookEvento.objects.filter(tienda_id=pedido.tienda_id, topic__startswith="orders/")
            .filter(payload__id=int(pedido.shopify_order_id)).order_by("-ts").first()
        )
        if evento is None:
            continue
        precios = {}
        for item in (evento.payload or {}).get("line_items") or []:
            codigo = str(item.get("sku") or "").strip()
            crudo = item.get("price")
            if not codigo or crudo in (None, ""):
                continue
            try:
                precios[codigo] = Decimal(str(crudo)).quantize(Decimal("0.01"))
            except InvalidOperation:
                continue
        for linea in LineaPedido.objects.filter(
            pedido=pedido, precio_unitario__isnull=True, parte_de_kit__isnull=True,
        ).select_related("sku"):
            precio = precios.get(linea.sku.codigo)
            if precio is not None:
                LineaPedido.objects.filter(pk=linea.pk).update(precio_unitario=precio)


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0009_reparto_carrier"),
        ("integraciones", "0002_tienda_webhook_secret"),
    ]

    operations = [
        migrations.AddField(
            model_name="lineapedido",
            name="precio_unitario",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.RunPython(rellenar_precios, migrations.RunPython.noop),
    ]
