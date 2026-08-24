"""Anuncio de ASN, base compartida entre Mesa y portal.

Renglones dinámicos (mixin de catalogo.forms) o, para recepciones grandes,
un CSV de renglones (codigo,cantidad) que SUSTITUYE a lo capturado a mano:
la hoja vive fuera del navegador y un refresh no pierde nada.
"""
import csv
import io

from django import forms
from django.utils import timezone

from apps.catalogo.forms import ConRenglonesSKU
from apps.catalogo.models import SKU


class FormAnuncioASNBase(ConRenglonesSKU, forms.Form):
    error_sin_lineas = "Captura al menos un producto con sus piezas para registrar la ASN."

    fecha_compromiso = forms.DateField(
        label="¿Qué día llega?",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={
            "required": "Captura la fecha de la cita para agendar la descarga.",
            "invalid": "Esa fecha no se entiende; elígela del calendario.",
        },
    )
    tarimas = forms.IntegerField(
        required=False,
        min_value=0,
        label="Tarimas anunciadas",
        help_text="Las que el cliente anunció; vacío si no las mencionó.",
        widget=forms.NumberInput(attrs={"placeholder": "0"}),
        error_messages={
            "min_value": "Las tarimas no pueden ser un número negativo.",
            "invalid": "Captura las tarimas con un número entero.",
        },
    )
    renglones_csv = forms.FileField(
        required=False,
        label="…o sube los renglones por CSV",
        help_text="Columnas codigo,cantidad. Si lo adjuntas, sustituye a los renglones capturados.",
    )

    def __init__(self, cliente, data=None, files=None, **kwargs):
        super().__init__(data, files, **kwargs)
        self._armar_renglones(cliente)

    def clean_fecha_compromiso(self):
        fecha = self.cleaned_data["fecha_compromiso"]
        if fecha < timezone.localdate():
            raise forms.ValidationError("La cita debe ser de hoy en adelante.")
        return fecha

    def clean(self):
        datos = super().clean()
        archivo = datos.get("renglones_csv")
        lineas = self._lineas_desde_csv(archivo) if archivo else self.consolidar_renglones(datos)
        if not lineas:
            raise forms.ValidationError(self.error_sin_lineas)
        datos["lineas"] = lineas
        return datos

    def _lineas_desde_csv(self, archivo):
        try:
            texto = archivo.read().decode("utf-8-sig")  # BOM de Excel incluido
        except UnicodeDecodeError:
            raise forms.ValidationError(
                "El CSV de renglones no se pudo leer. Guárdalo como CSV UTF-8 y súbelo de nuevo."
            )
        lector = csv.DictReader(io.StringIO(texto))
        lector.fieldnames = [e.strip() for e in (lector.fieldnames or [])]
        if "codigo" not in lector.fieldnames or "cantidad" not in lector.fieldnames:
            raise forms.ValidationError(
                "El CSV de renglones lleva encabezados codigo,cantidad."
            )
        por_codigo = {s.codigo: s for s in SKU.objects.filter(cliente=self.cliente, activo=True)}
        consolidadas, errores = {}, []
        for numero, fila in enumerate(lector, start=2):
            codigo = (fila.get("codigo") or "").strip()
            crudo = (fila.get("cantidad") or "").strip()
            if not codigo and not crudo:
                continue
            sku = por_codigo.get(codigo)
            if sku is None:
                errores.append(f"fila {numero}: SKU desconocido '{codigo}'")
                continue
            try:
                cantidad = int(crudo)
            except ValueError:
                errores.append(f"fila {numero}: cantidad no es número")
                continue
            if cantidad < 1:
                errores.append(f"fila {numero}: la cantidad mínima es 1")
                continue
            consolidadas[sku] = consolidadas.get(sku, 0) + cantidad
        if errores:
            raise forms.ValidationError("El CSV de renglones trae errores: " + "; ".join(errores))
        return list(consolidadas.items())
