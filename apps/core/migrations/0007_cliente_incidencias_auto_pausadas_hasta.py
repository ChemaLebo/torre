"""Pausa de incidencias automáticas por cliente: hasta esta fecha (inclusive)
el sistema no abre FAL/CAN/RF/RET/DES por sí solo; las manuales y las del
comprador siguen (Chema, 2026-09-20)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_reparto'),
    ]

    operations = [
        migrations.AddField(
            model_name='cliente',
            name='incidencias_auto_pausadas_hasta',
            field=models.DateField(blank=True, help_text='Hasta esta fecha (inclusive) el sistema NO abre incidencias automáticas (FAL, CAN, RF, RET, DES); las manuales y las del comprador siguen. Vacío = activas.', null=True),
        ),
    ]
