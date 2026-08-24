"""Renglones de producto compartidos (ASN y pedido manual).

Renglones dinámicos: bound, el form arma sus campos a partir de los índices
sku_N presentes en el POST (el botón "+" del template clona renglones por JS);
unbound pinta `renglones_iniciales` vacíos. El dropdown agrupa los productos
por categoría (optgroup), Otros al final, alfabético por descripción.
"""
import re

from django import forms

from .models import SKU, opciones_sku_agrupadas

MAX_RENGLONES = 500
_INDICE_SKU = re.compile(r"^sku_(\d+)$")


class ConRenglonesSKU:
    """Mixin: llamar `_armar_renglones(cliente)` al final del __init__ del form."""

    renglones_iniciales = 6
    error_renglon_incompleto = "Completa producto y piezas en cada renglón que uses."

    def _armar_renglones(self, cliente):
        self.cliente = cliente
        opciones = [("", "Elige un producto")] + opciones_sku_agrupadas(cliente)
        if self.is_bound:
            indices = sorted({
                int(m.group(1))
                for clave in self.data
                if (m := _INDICE_SKU.match(clave))
            })[:MAX_RENGLONES] or [1]
        else:
            indices = list(range(1, self.renglones_iniciales + 1))
        self.indices_renglones = indices
        for i in indices:
            self.fields[f"sku_{i}"] = forms.ChoiceField(
                choices=opciones, required=False, label="Producto",
            )
            self.fields[f"cantidad_{i}"] = forms.IntegerField(
                required=False,
                min_value=1,
                label="Piezas",
                widget=forms.NumberInput(attrs={"placeholder": "Piezas"}),
                error_messages={"min_value": "Las piezas deben ser al menos 1."},
            )

    def renglones(self):
        """Pares (producto, piezas) para pintar la tabla del formulario."""
        for i in self.indices_renglones:
            yield self[f"sku_{i}"], self[f"cantidad_{i}"]

    def consolidar_renglones(self, datos):
        """[(SKU, piezas)] consolidando duplicados; ValidationError si un renglón cojea."""
        activos = {str(s.pk): s for s in SKU.objects.filter(cliente=self.cliente, activo=True)}
        consolidadas = {}
        for i in self.indices_renglones:
            sku = activos.get(datos.get(f"sku_{i}") or "")
            cantidad = datos.get(f"cantidad_{i}")
            if sku is not None and cantidad:
                consolidadas[sku] = consolidadas.get(sku, 0) + cantidad
            elif sku is not None or cantidad:
                raise forms.ValidationError(self.error_renglon_incompleto)
        return list(consolidadas.items())
