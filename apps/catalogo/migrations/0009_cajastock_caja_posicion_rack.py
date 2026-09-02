from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [("catalogo", "0008_sku_productos_por_kit")]

    operations = [
        migrations.AddField(
            model_name="caja",
            name="posicion_rack",
            field=models.CharField(blank=True, default="", help_text="Dónde se almacena el fajo de cajas (ej. RES-6-4)", max_length=40),
        ),
        migrations.CreateModel(
            name="CajaStock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("zona", models.CharField(choices=[("rack", "Rack / almacén"), ("packing", "Packing")], max_length=10)),
                ("cantidad", models.PositiveIntegerField(default=0)),
                ("caja", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stock", to="catalogo.caja")),
            ],
            options={
                "verbose_name": "stock de caja",
                "verbose_name_plural": "stock de cajas",
                "unique_together": {("caja", "zona")},
            },
        ),
    ]
