"""Reparto de carriers por porcentajes: tercera integración de envíos
("reparto") y su estado por cliente — pesos, cursor y base del bloque."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_cliente_integracion_envios"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cliente",
            name="integracion_envios",
            field=models.CharField(
                choices=[
                    ("envia", "envia.com"),
                    ("99minutos", "99minutos directo"),
                    ("reparto", "Reparto por porcentajes"),
                ],
                default="99minutos",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="cliente",
            name="reparto_pesos",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="cliente",
            name="reparto_cursor",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="cliente",
            name="reparto_base",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
