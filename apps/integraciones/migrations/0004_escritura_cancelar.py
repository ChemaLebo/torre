"""Acción "cancelar" en la cola de escrituras a Shopify: fulfillmentCancel de
una caja que firmó manifiesto pero nunca se fue ("Quitar de salida", 2026-10-05)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('integraciones', '0003_escritura_shopify_pendiente'),
    ]

    operations = [
        migrations.AlterField(
            model_name='escriturashopifypendiente',
            name='accion',
            field=models.CharField(choices=[('fulfillment', 'Crear fulfillment'), ('tracking', 'Actualizar rastreo (reposición)'), ('evento', 'Evento de avance'), ('cancelar', 'Cancelar fulfillment (caja que no salió)')], max_length=20),
        ),
    ]
