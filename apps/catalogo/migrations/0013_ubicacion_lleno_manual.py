"""Ubicacion.lleno_manual: marca del piso al contar (anaquel lleno / con espacio)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0012_ubicacion_medidas"),
    ]

    operations = [
        migrations.AddField(model_name="ubicacion", name="lleno_manual", field=models.BooleanField(default=False)),
    ]
