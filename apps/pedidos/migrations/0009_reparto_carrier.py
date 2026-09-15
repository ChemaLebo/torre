"""Pedido.reparto_carrier: la carta que le tocó en el reparto por porcentajes
del cliente (vacío = no repartido)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0008_canal"),
    ]

    operations = [
        migrations.AddField(
            model_name="pedido",
            name="reparto_carrier",
            field=models.CharField(blank=True, max_length=40),
        ),
    ]
