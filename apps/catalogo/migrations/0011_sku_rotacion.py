"""SKU.rotacion: clase A/B/C para el acomodo sugerido, "auto" = por ventas."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0010_lote_creado"),
    ]

    operations = [
        migrations.AddField(
            model_name="sku",
            name="rotacion",
            field=models.CharField(
                choices=[("auto", "Automática"), ("A", "A · alta"), ("B", "B · media"), ("C", "C · baja")],
                default="auto", max_length=4,
            ),
        ),
    ]
