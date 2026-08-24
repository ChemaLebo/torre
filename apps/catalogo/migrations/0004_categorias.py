import django.db.models.deletion
from django.db import migrations, models


def _backfill(apps, schema_editor):
    Cliente = apps.get_model("core", "Cliente")
    Categoria = apps.get_model("catalogo", "Categoria")
    SKU = apps.get_model("catalogo", "SKU")
    for cliente in Cliente.objects.all():
        otros, _ = Categoria.objects.get_or_create(cliente=cliente, nombre="Otros")
        SKU.objects.filter(cliente=cliente, categoria__isnull=True).update(categoria=otros)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        ("catalogo", "0003_ubicacion_carriers"),
    ]

    operations = [
        migrations.CreateModel(
            name="Categoria",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nombre", models.CharField(max_length=60)),
                ("cliente", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="categorias", to="core.cliente")),
            ],
            options={
                "ordering": ["cliente", "nombre"],
                "verbose_name": "categoría",
                "verbose_name_plural": "categorías",
                "unique_together": {("cliente", "nombre")},
            },
        ),
        migrations.AddField(
            model_name="sku",
            name="categoria",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="skus", to="catalogo.categoria",
                help_text="Vacío = Otros (la default del cliente)",
            ),
        ),
        migrations.RunPython(_backfill, migrations.RunPython.noop),
    ]
