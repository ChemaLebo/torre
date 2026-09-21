"""Paquete.foto_contenido: la foto del contenido de cada caja deja de vivir
solo en la auditoría (evento caja_empacada: delta.caja + delta.evidencia_id)
y queda en columna, como foto_cierre — el wizard de empaque la muestra por
caja y permite reemplazarla. Backfill desde los eventos existentes."""
import django.db.models.deletion
from django.db import migrations, models


def desde_eventos(apps, schema_editor):
    Paquete = apps.get_model("envios", "Paquete")
    EventoAuditoria = apps.get_model("core", "EventoAuditoria")
    EvidenciaFoto = apps.get_model("core", "EvidenciaFoto")
    eventos = EventoAuditoria.objects.filter(
        entidad="pedido", accion="caja_empacada",
    ).order_by("ts")
    for evento in eventos.iterator():
        delta = evento.delta or {}
        foto_id, numero = delta.get("evidencia_id"), delta.get("caja")
        if not (str(evento.entidad_id).isdigit() and foto_id and numero):
            continue
        if not EvidenciaFoto.objects.filter(pk=foto_id).exists():
            continue
        Paquete.objects.filter(
            pedido_id=int(evento.entidad_id), numero=numero, foto_contenido__isnull=True,
        ).update(foto_contenido_id=foto_id)


class Migration(migrations.Migration):

    dependencies = [
        ("envios", "0010_eventoguia"),
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="paquete",
            name="foto_contenido",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to="core.evidenciafoto",
            ),
        ),
        migrations.RunPython(desde_eventos, migrations.RunPython.noop),
    ]
