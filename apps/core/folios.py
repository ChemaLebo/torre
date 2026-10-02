"""Folios consecutivos `PREFIJO-####` sin tope. El siguiente sale del máximo
NUMÉRICO en la base de datos, no del orden alfabético del texto: ordenando
como texto, "PED-99999" queda después de "PED-100000", así que al pasar de
99,999 pedidos (o de 9,999 incidencias o manifiestos en un año) se volvía a
generar el mismo folio y tiraba la ingesta (Chema 2026-10-02). El relleno
de ceros es un mínimo, no un tope: el pedido 100,000 es PED-100000."""
from django.db.models import IntegerField, Max
from django.db.models.functions import Cast, Substr


def siguiente_folio(modelo, prefijo, ancho=4):
    """Siguiente `PREFIJO-<consecutivo>` para un modelo con campo `folio`;
    `prefijo` sin el guion final ("PED", "INC-2026"). Solo cuenta los folios
    con sufijo numérico: uno ajeno (import, demo) no descarrila la secuencia."""
    base = f"{prefijo}-"
    mayor = (
        modelo.objects.filter(folio__regex=rf"^{base}\d+$")
        .annotate(consecutivo=Cast(Substr("folio", len(base) + 1), IntegerField()))
        .aggregate(mayor=Max("consecutivo"))["mayor"]
    ) or 0
    return f"{base}{mayor + 1:0{ancho}d}"
