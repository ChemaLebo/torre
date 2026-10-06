"""LineaManifiesto.no_salio + ts_no_salio: "Quitar de salida" en Mesa (Chema
2026-10-05, PED-00319). La caja firmó en el manifiesto pero nunca se fue; la
hoja y la firma se conservan y la línea deja de contar como salida."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('envios', '0020_paquete_carrier_forzado'),
    ]

    operations = [
        migrations.AddField(
            model_name='lineamanifiesto',
            name='no_salio',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='lineamanifiesto',
            name='ts_no_salio',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
