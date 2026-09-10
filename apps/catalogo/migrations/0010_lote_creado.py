import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """Lote.creado: los lotes existentes toman la fecha de la migración
    (no hay otra fuente); las sugerencias por antigüedad los tratan como recientes."""

    dependencies = [
        ("catalogo", "0009_cajastock_caja_posicion_rack"),
    ]

    operations = [
        migrations.AddField(
            model_name="lote",
            name="creado",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
