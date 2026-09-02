from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0007_caja_sku_usa_caja_propia"),
        ("envios", "0006_guia_etiqueta_pdf_guia_proveedor"),
    ]

    operations = [
        migrations.AddField(
            model_name="paquete",
            name="caja",
            field=models.ForeignKey(blank=True, help_text="Caja de empaque usada; su tara vuelve honesto el peso esperado", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalogo.caja"),
        ),
    ]
