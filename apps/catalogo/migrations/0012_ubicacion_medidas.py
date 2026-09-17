"""Ubicacion.largo_cm/ancho_cm/alto_cm/prioridad para la capacidad y el acomodo
sugerido. Backfill: las ubicaciones con código PIC/RES-<rack>-<I|D>-<F|B>-<piso>
reciben las medidas y la prioridad de settings (catalogo.services)."""
from django.db import migrations, models


def desde_codigos(apps, schema_editor):
    from apps.catalogo.services import medidas_de_codigo

    Ubicacion = apps.get_model("catalogo", "Ubicacion")
    for u in Ubicacion.objects.all():
        valores = medidas_de_codigo(u.codigo)
        if valores is None:
            continue
        Ubicacion.objects.filter(pk=u.pk).update(**valores)


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0011_sku_rotacion"),
    ]

    operations = [
        migrations.AddField(model_name="ubicacion", name="largo_cm", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="ubicacion", name="ancho_cm", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(
            model_name="ubicacion", name="alto_cm",
            field=models.PositiveIntegerField(default=0, help_text="0 = sin tope de alto (reserva)"),
        ),
        migrations.AddField(
            model_name="ubicacion", name="prioridad",
            field=models.PositiveIntegerField(blank=True, null=True, help_text="Orden de acceso: 1 = mejor; vacío = no se sugiere"),
        ),
        migrations.RunPython(desde_codigos, migrations.RunPython.noop),
    ]
