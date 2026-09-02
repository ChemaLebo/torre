"""Cliente.integracion_envios: el flip de 99minutos es por cliente.

Los clientes EXISTENTES migran a "envia" (la conducta que ya tenían); el
default "99minutos" aplica solo a clientes nuevos.
"""
from django.db import migrations, models


def existentes_a_envia(apps, schema_editor):
    Cliente = apps.get_model("core", "Cliente")
    Cliente.objects.update(integracion_envios="envia")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_pin_hasheado"),
    ]

    operations = [
        migrations.AddField(
            model_name="cliente",
            name="integracion_envios",
            field=models.CharField(
                choices=[("envia", "envia.com"), ("99minutos", "99minutos directo")],
                default="99minutos",
                max_length=12,
            ),
        ),
        migrations.RunPython(existentes_a_envia, migrations.RunPython.noop),
    ]
