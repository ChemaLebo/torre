from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("pedidos", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccesoRastreo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(editable=False, max_length=12, unique=True)),
                ("creado", models.DateTimeField(auto_now_add=True)),
                (
                    "pedido",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="acceso_rastreo",
                        to="pedidos.pedido",
                    ),
                ),
            ],
            options={
                "verbose_name": "acceso de rastreo",
                "verbose_name_plural": "accesos de rastreo",
                "ordering": ["-creado"],
            },
        ),
    ]
