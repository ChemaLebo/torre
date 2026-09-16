"""EventoGuia: historial de rastreo por guía con la hora del carrier (reporte
de tiempos logísticos)."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("envios", "0009_paquete_cierre"),
    ]

    operations = [
        migrations.CreateModel(
            name="EventoGuia",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("estado", models.CharField(blank=True, help_text="Estado canónico de Torre, si se pudo normalizar", max_length=20)),
                ("crudo", models.CharField(blank=True, help_text="Código o status tal cual lo manda el carrier", max_length=80)),
                ("descripcion", models.CharField(blank=True, max_length=300)),
                ("ts_carrier", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("ts_visto", models.DateTimeField(auto_now_add=True)),
                ("raw", models.JSONField(blank=True, default=dict)),
                ("guia", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="eventos", to="envios.guia")),
            ],
            options={
                "verbose_name": "evento de guía",
                "verbose_name_plural": "eventos de guía",
                "ordering": ["ts_carrier", "pk"],
            },
        ),
    ]
