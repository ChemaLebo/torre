from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_cliente_integracion_envios"),
        ("catalogo", "0006_sku_es_kit"),
    ]

    operations = [
        migrations.CreateModel(
            name="Caja",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nombre", models.CharField(max_length=60)),
                ("largo_cm", models.PositiveIntegerField()),
                ("ancho_cm", models.PositiveIntegerField()),
                ("alto_cm", models.PositiveIntegerField()),
                ("peso_gr", models.PositiveIntegerField(default=0, help_text="Tara: peso de la caja vacía con relleno estándar")),
                ("activo", models.BooleanField(default=True)),
                ("creado", models.DateTimeField(auto_now_add=True)),
                ("cliente", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cajas", to="core.cliente")),
            ],
            options={
                "verbose_name": "caja de empaque",
                "verbose_name_plural": "cajas de empaque",
                "ordering": ["cliente", "nombre"],
                "unique_together": {("cliente", "nombre")},
            },
        ),
        migrations.AddField(
            model_name="sku",
            name="usa_caja_propia",
            field=models.BooleanField(default=False, help_text="El producto viaja en SU propio empaque (kit brandeado, caja del producto): sus dims/peso de catálogo son los del bulto y no se elige caja al empacar"),
        ),
    ]
