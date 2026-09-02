from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("pedidos", "0003_lineapedido_nota_kit_lineapedido_parte_de_kit")]

    operations = [
        migrations.AddField(
            model_name="lineapedido",
            name="kit_caja",
            field=models.PositiveIntegerField(blank=True, help_text="Nº de caja del kit (1..cantidad) a la que pertenece esta hija — el stepper 'TeaBox 1 de N' declara caja por caja", null=True),
        ),
    ]
