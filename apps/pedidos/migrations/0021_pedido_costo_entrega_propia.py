"""Pedido.costo_entrega_propia (Chema 2026-10-06): lo que nos costó llevar un
pedido sin guía de carrier; vacío = $0. Es costo (finanzas), no cobro."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pedidos', '0020_fundir_lineas_de_reposicion'),
    ]

    operations = [
        migrations.AddField(
            model_name='pedido',
            name='costo_entrega_propia',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
    ]
