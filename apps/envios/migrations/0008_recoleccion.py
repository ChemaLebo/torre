from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("envios", "0007_paquete_caja")]

    operations = [
        migrations.CreateModel(
            name="Recoleccion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("carrier", models.CharField(max_length=40)),
                ("fecha", models.DateField()),
                ("hora_desde", models.PositiveIntegerField(help_text="Hora entera 0-23 (timeFrom)")),
                ("hora_hasta", models.PositiveIntegerField(help_text="Hora entera 1-23 (timeTo)")),
                ("instrucciones", models.CharField(blank=True, default="", max_length=200)),
                ("folio_carrier", models.CharField(blank=True, default="", max_length=60)),
                ("costo", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("creado", models.DateTimeField(auto_now_add=True)),
                ("guias", models.ManyToManyField(blank=True, related_name="recolecciones", to="envios.guia")),
            ],
            options={
                "verbose_name": "recolección",
                "verbose_name_plural": "recolecciones",
                "ordering": ["-fecha", "carrier"],
                "unique_together": {("carrier", "fecha")},
            },
        ),
    ]
