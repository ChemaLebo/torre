"""LineaPedido.cantidad_repuesta + cantidad_repuesta_reservada (Chema
2026-10-05): la línea es el line item de Shopify y nunca se agrega otra; lo
repuesto se suma aquí y el planeador arma cajas nuevas (PaqueteLinea.repone_a)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pedidos', '0018_lineapedido_reposicion_de'),
    ]

    operations = [
        migrations.AddField(
            model_name='lineapedido',
            name='cantidad_repuesta',
            field=models.PositiveIntegerField(default=0, help_text='Piezas aprobadas para reponer (se suman a lo pedido para surtir)'),
        ),
        migrations.AddField(
            model_name='lineapedido',
            name='cantidad_repuesta_reservada',
            field=models.PositiveIntegerField(default=0, help_text='De las repuestas, cuántas ya tienen stock apartado'),
        ),
        migrations.AddField(
            model_name='lineapedido',
            name='origen_reposicion',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
