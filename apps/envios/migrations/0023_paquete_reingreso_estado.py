"""Paquete.reingreso_estado (Chema 2026-10-06, PED-00067): el reingreso se
decide POR CAJA. Las decisiones ya tomadas por pedido se copian a sus cajas
despachadas para que no vuelvan a aparecer por decidir."""
from django.db import migrations, models


def copiar_decisiones(apps, schema_editor):
    Pedido = apps.get_model("pedidos", "Pedido")
    Paquete = apps.get_model("envios", "Paquete")
    for pedido in Pedido.objects.exclude(reingreso_estado=""):
        Paquete.objects.filter(pedido=pedido, estado="DESPACHADO").update(reingreso_estado=pedido.reingreso_estado)


class Migration(migrations.Migration):

    dependencies = [
        ('envios', '0022_paquetelinea_repone_a'),
        ('pedidos', '0021_pedido_costo_entrega_propia'),
    ]

    operations = [
        migrations.AddField(
            model_name='paquete',
            name='reingreso_estado',
            field=models.CharField(blank=True, choices=[('', 'Sin decidir'), ('reingresado', 'Reingresado'), ('no_recuperado', 'Inventario no recuperado')], default='', max_length=15),
        ),
        migrations.RunPython(copiar_decisiones, migrations.RunPython.noop),
    ]
