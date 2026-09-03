from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("pedidos", "0004_lineapedido_kit_caja"),
    ]

    operations = [
        migrations.AddField(
            model_name="pedido",
            name="asignado_a",
            field=models.ForeignKey(blank=True, help_text="Operador de piso dueño: de iniciar picking a la última foto de cierre", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pedidos_asignados", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="pedido",
            name="transferencia_a",
            field=models.ForeignKey(blank=True, help_text="Transferencia pendiente: el destinatario debe ACEPTAR para volverse dueño", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pedidos_por_aceptar", to=settings.AUTH_USER_MODEL),
        ),
    ]
