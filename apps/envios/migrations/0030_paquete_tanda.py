"""Paquete.ts_etiquetas + ts_listo_salida: los sellos de la tanda de empaque
(etiquetas pegadas, lista en salida; Chema 2026-10-20). Relleno: toda caja
que ya tenía cierre recibe ambos sellos con la hora de su cierre, para que
nada desaparezca del corral al desplegar; las empacadas sin cierre quedan en
blanco (entran por la pantalla Etiquetas) y las despachadas ya no piden nada."""
from django.db import migrations, models
from django.db.models import F


def desde_cierre(apps, schema_editor):
    Paquete = apps.get_model("envios", "Paquete")
    Paquete.objects.filter(ts_cierre__isnull=False, ts_etiquetas__isnull=True).update(ts_etiquetas=F("ts_cierre"))
    Paquete.objects.filter(ts_cierre__isnull=False, ts_listo_salida__isnull=True).update(ts_listo_salida=F("ts_cierre"))


class Migration(migrations.Migration):

    dependencies = [
        ("envios", "0029_trabajo_impresion_tipos"),
    ]

    operations = [
        migrations.AddField(
            model_name="paquete",
            name="ts_etiquetas",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paquete",
            name="ts_listo_salida",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(desde_cierre, migrations.RunPython.noop),
    ]
