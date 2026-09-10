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
    excluir_kits = False  # ASN lo prende: recibir un kit fabricaría stock inexistente
    con_lotes = False  # ASN lo prende: cada renglón trae lote y caducidad opcionales
    error_renglon_incompleto = "Completa producto y piezas en cada renglón que uses."

    def _armar_renglones(self, cliente):
        self.cliente = cliente
        opciones = [("", "Elige un producto")] + opciones_sku_agrupadas(
            cliente, excluir_kits=self.excluir_kits,
        )
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
            if self.con_lotes:
                self.fields[f"lote_{i}"] = forms.CharField(
                    required=False, max_length=60, label="Lote",
                    widget=forms.TextInput(attrs={
                        "placeholder": "L-2026-09", "class": "mono", "autocomplete": "off",
                        "list": "lotes-recientes",
                    }),
                )
                self.fields[f"caducidad_{i}"] = forms.DateField(
                    required=False, label="Caducidad",
                    widget=forms.DateInput(attrs={"type": "date"}),
                    error_messages={"invalid": "Esa caducidad no se entiende; elígela del calendario."},
                )

    def renglones(self):
        """Pares (producto, piezas) para pintar la tabla del formulario."""
        for i in self.indices_renglones:
            yield self[f"sku_{i}"], self[f"cantidad_{i}"]

    def renglones_con_lote(self):
        """(producto, piezas, lote, caducidad) para los forms con `con_lotes`."""
        for i in self.indices_renglones:
            yield self[f"sku_{i}"], self[f"cantidad_{i}"], self[f"lote_{i}"], self[f"caducidad_{i}"]

    def consolidar_renglones(self, datos):
        """Renglones consolidados; ValidationError si un renglón cojea.

        Sin lotes: [(SKU, piezas)] sumando duplicados por SKU. Con lotes:
        [(SKU, piezas, lote_codigo, fecha_caducidad)] sumando por (SKU, lote).
        """
        qs = SKU.objects.filter(cliente=self.cliente, activo=True)
        if self.excluir_kits:
            qs = qs.filter(es_kit=False)
        activos = {str(s.pk): s for s in qs}
        consolidadas = {}
        caducidades = {}
        for i in self.indices_renglones:
            sku = activos.get(datos.get(f"sku_{i}") or "")
            cantidad = datos.get(f"cantidad_{i}")
            if sku is not None and cantidad:
                if self.con_lotes:
                    lote = (datos.get(f"lote_{i}") or "").strip()
                    clave = (sku, lote)
                    if datos.get(f"caducidad_{i}"):
                        caducidades[clave] = datos[f"caducidad_{i}"]
                else:
                    clave = sku
                consolidadas[clave] = consolidadas.get(clave, 0) + cantidad
            elif sku is not None or cantidad:
                raise forms.ValidationError(self.error_renglon_incompleto)
        if not self.con_lotes:
            return list(consolidadas.items())
        return [
            (sku, piezas, lote, caducidades.get((sku, lote)))
            for (sku, lote), piezas in consolidadas.items()
        ]
