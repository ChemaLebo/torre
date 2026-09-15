"""Paquete.ts_cierre + foto_cierre: el cierre de caja con evidencia deja de
leerse de la auditoría (evento caja_cerrada_con_evidencia) y vive en columnas.
Backfill desde los eventos existentes: ts del evento y delta.evidencia_id."""
import django.db.models.deletion
from django.db import migrations, models


def desde_eventos(apps, schema_editor):
    Paquete = apps.get_model("envios", "Paquete")
    EventoAuditoria = apps.get_model("core", "EventoAuditoria")
    EvidenciaFoto = apps.get_model("core", "EvidenciaFoto")
    eventos = EventoAuditoria.objects.filter(
        entidad="paquete", accion="caja_cerrada_con_evidencia",
    ).order_by("ts")
    for evento in eventos.iterator():
        if not str(evento.entidad_id).isdigit():
            continue
        paquete = Paquete.objects.filter(pk=int(evento.entidad_id), ts_cierre__isnull=True).first()
        if paquete is None:
            continue
        foto_id = (evento.delta or {}).get("evidencia_id")
        paquete.ts_cierre = evento.ts
        if foto_id and EvidenciaFoto.objects.filter(pk=foto_id).exists():
            paquete.foto_cierre_id = foto_id
        paquete.save(update_fields=["ts_cierre", "foto_cierre"])


class Migration(migrations.Migration):

    dependencies = [
        ("envios", "0008_recoleccion"),
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="paquete",
            name="ts_cierre",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paquete",
            name="foto_cierre",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to="core.evidenciafoto",
            ),
        ),
        migrations.RunPython(desde_eventos, migrations.RunPython.noop),
    ]
