"""PaqueteLinea.repone_a (Chema 2026-10-05): el renglón de un producto dentro
de una caja de reposición apunta a la caja original cuyas piezas repone."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('envios', '0021_lineamanifiesto_no_salio'),
    ]

    operations = [
        migrations.AddField(
            model_name='paquetelinea',
            name='repone_a',
            field=models.ForeignKey(blank=True, help_text='Caja original cuyas piezas de este producto repone este renglón', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='repuesta_en', to='envios.paquete'),
        ),
    ]
