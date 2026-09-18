"""OrdenEntrada.plan_acomodo: plan de put-away de toda la orden (acomodo sugerido por ASN)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventario", "0005_orden_entrada_reingreso"),
    ]

    operations = [
        migrations.AddField(model_name="ordenentrada", name="plan_acomodo", field=models.JSONField(blank=True, default=dict)),
    ]
