from django.db import migrations, models

# Backfill del mapeo que antes vivía hardcodeado en piso._corral_de_carrier.
_LEGADO = {"SAL-PQX": "paquetexpress", "SAL-LOCAL": "local"}


def _backfill(apps, schema_editor):
    Ubicacion = apps.get_model("catalogo", "Ubicacion")
    for codigo, carriers in _LEGADO.items():
        Ubicacion.objects.filter(codigo=codigo, carriers="").update(carriers=carriers)


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0002_sku_empaques_divisibles"),
    ]

    operations = [
        migrations.AddField(
            model_name="ubicacion",
            name="carriers",
            field=models.CharField(
                blank=True, default="", max_length=200,
                help_text="Solo corrales: carriers separados por coma (vacío = comodín)",
            ),
        ),
        migrations.RunPython(_backfill, migrations.RunPython.noop),
    ]
