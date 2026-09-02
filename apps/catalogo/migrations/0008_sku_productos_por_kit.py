from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("catalogo", "0007_caja_sku_usa_caja_propia")]

    operations = [
        migrations.AddField(
            model_name="sku",
            name="productos_por_kit",
            field=models.PositiveIntegerField(default=0, help_text="Solo kits: cuántos productos EXACTOS lleva una caja (TeaBox de 3 → 3). 0 = libre. El total del pedido se deriva: cajas × cupo."),
        ),
    ]
