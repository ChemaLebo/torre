"""Funde las líneas de reposición (modelo viejo: LineaPedido.reposicion_de)
en su línea original (Chema 2026-10-05: "los line items son de la orden y
nunca se agregan"). Por cada línea de reposición R de la original O:
repuestas, repuestas con stock, pickeadas y despachadas se suman a O; los
renglones de caja de R pasan a O con `repone_a` = la caja despachada que
llevaba O con guía "sustituida" (o la primera despachada), y R se borra.
Irreversible: respaldar la base antes de migrar."""
import logging

from django.db import migrations

log = logging.getLogger("torre.migraciones")


def fundir(apps, schema_editor):
    LineaPedido = apps.get_model("pedidos", "LineaPedido")
    Paquete = apps.get_model("envios", "Paquete")
    PaqueteLinea = apps.get_model("envios", "PaqueteLinea")
    Guia = apps.get_model("envios", "Guia")
    resumen = []
    for rep in LineaPedido.objects.filter(reposicion_de__isnull=False).select_related("pedido").order_by("pk"):
        orig = LineaPedido.objects.get(pk=rep.reposicion_de_id)  # fresco: dos reposiciones de la misma línea se acumulan
        orig.cantidad_repuesta += rep.cantidad
        if rep.reservada:
            orig.cantidad_repuesta_reservada += rep.cantidad
        orig.cantidad_pickeada += rep.cantidad_pickeada
        orig.cantidad_despachada += rep.cantidad_despachada
        cajas_rep = set(PaqueteLinea.objects.filter(linea_pedido=rep).values_list("paquete_id", flat=True))
        candidatas = list(
            Paquete.objects.filter(lineas__linea_pedido=orig, estado="DESPACHADO")
            .exclude(pk__in=cajas_rep).distinct().order_by("numero")
        )
        origen = next(
            (c for c in candidatas if Guia.objects.filter(paquete=c).exclude(sustituida_motivo="").exists()),
            candidatas[0] if candidatas else None,
        )
        orig.origen_reposicion = list(orig.origen_reposicion or []) + [
            {"caja": origen.pk if origen else None, "numero": origen.numero if origen else None, "cantidad": rep.cantidad},
        ]
        orig.save(update_fields=[
            "cantidad_repuesta", "cantidad_repuesta_reservada", "cantidad_pickeada", "cantidad_despachada", "origen_reposicion",
        ])
        PaqueteLinea.objects.filter(linea_pedido=rep).update(linea_pedido=orig, repone_a=origen)
        resumen.append(
            f"{rep.pedido.folio} {orig.sku_id}: +{rep.cantidad} repuestas (pick {rep.cantidad_pickeada}, "
            f"desp {rep.cantidad_despachada}) → caja origen {origen.numero if origen else 'desconocida'}"
        )
        rep.delete()
    for linea in resumen:
        log.info("fundir_reposiciones: %s", linea)
    if resumen:
        print(f"\n  reposiciones fundidas: {len(resumen)}\n  " + "\n  ".join(resumen))


class Migration(migrations.Migration):

    dependencies = [
        ('pedidos', '0019_lineapedido_repuestas'),
        ('envios', '0022_paquetelinea_repone_a'),
    ]

    operations = [
        migrations.RunPython(fundir, migrations.RunPython.noop),
    ]
