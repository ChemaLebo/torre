"""Formularios del portal: las acciones mínimas del día 1 (BLUEPRINT §3.1).

Copy pensado para Karina: nada de jerga técnica. Todo queryset se acota al
cliente del request en __init__ — un formulario jamás ofrece datos ajenos.
"""
from django import forms
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.incidencias.models import Incidencia
from apps.pedidos.models import Pedido

# Renglones fijos del anuncio de recepción (sin JavaScript, sin formsets).
RENGLONES_ASN = 4

# Tipos de incidencia en palabras de cliente, no claves de catálogo.
TIPOS_INCIDENCIA_PORTAL = [
    (Incidencia.TIPO_DAN, "Llegó dañado o roto"),
    (Incidencia.TIPO_RET, "Va retrasado"),
    (Incidencia.TIPO_RF, "Dicen que lo entregaron y no llegó"),
    (Incidencia.TIPO_FAL, "Llegó incompleto"),
    (Incidencia.TIPO_DIR, "Hay un problema con la dirección"),
    (Incidencia.TIPO_CAN, "Necesito cancelar un pedido"),
    (Incidencia.TIPO_DES, "Mi inventario no cuadra"),
]


class FormRespuestaIncidencia(forms.Form):
    """Mensaje del cliente al timeline de una incidencia."""

    texto = forms.CharField(
        label="Tu mensaje",
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": "Escríbenos lo que necesites; todo queda en el expediente.",
        }),
        error_messages={"required": "Escribe tu mensaje antes de enviarlo."},
    )


class FormNuevaIncidencia(forms.Form):
    """Los 3 campos del blueprint: pedido, tipo y descripción."""

    pedido = forms.ModelChoiceField(
        queryset=Pedido.objects.none(),
        required=False,
        label="Pedido relacionado",
        empty_label="No está ligada a un pedido",
    )
    tipo = forms.ChoiceField(
        choices=TIPOS_INCIDENCIA_PORTAL,
        label="¿Qué pasó?",
        error_messages={"required": "Elige qué pasó para poder canalizarla."},
    )
    descripcion = forms.CharField(
        label="Cuéntanos con tus palabras",
        widget=forms.Textarea(attrs={
            "rows": 5,
            "placeholder": "Entre más detalle nos des, más rápido lo resolvemos.",
        }),
        error_messages={"required": "Cuéntanos qué pasó; sin descripción no podemos ayudarte."},
    )

    def __init__(self, cliente, *args, **kwargs):
        super().__init__(*args, **kwargs)
        campo = self.fields["pedido"]
        campo.queryset = Pedido.objects.filter(cliente=cliente).order_by("-creado")
        campo.label_from_instance = (
            lambda p: f"{p.folio} · {p.comprador_nombre or 'sin nombre'} · {p.get_estado_display()}"
        )


class FormAnuncioASN(forms.Form):
    """Anuncio de una entrega entrante: cita + hasta 4 renglones de producto."""

    fecha_compromiso = forms.DateField(
        label="¿Qué día llega?",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={
            "required": "Dinos qué día llega tu mercancía para agendar la descarga.",
            "invalid": "Esa fecha no se entiende; elígela del calendario.",
        },
    )
    tarimas = forms.IntegerField(
        required=False,
        min_value=0,
        label="¿Cuántas tarimas llegan?",
        help_text="Si viene suelto o todavía no sabes, déjalo vacío.",
        widget=forms.NumberInput(attrs={"placeholder": "0"}),
        error_messages={
            "min_value": "Las tarimas no pueden ser un número negativo.",
            "invalid": "Captura las tarimas con un número entero.",
        },
    )

    def __init__(self, cliente, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo")
        for i in range(1, RENGLONES_ASN + 1):
            self.fields[f"sku_{i}"] = forms.ModelChoiceField(
                queryset=qs,
                required=False,
                label="Producto",
                empty_label="Elige un producto",
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
        for i in range(1, RENGLONES_ASN + 1):
            yield self[f"sku_{i}"], self[f"cantidad_{i}"]

    def clean_fecha_compromiso(self):
        fecha = self.cleaned_data["fecha_compromiso"]
        if fecha < timezone.localdate():
            raise forms.ValidationError("La cita debe ser de hoy en adelante.")
        return fecha

    def clean(self):
        datos = super().clean()
        consolidadas = {}
        for i in range(1, RENGLONES_ASN + 1):
            sku = datos.get(f"sku_{i}")
            cantidad = datos.get(f"cantidad_{i}")
            if sku is not None and cantidad:
                consolidadas[sku] = consolidadas.get(sku, 0) + cantidad
            elif sku is not None or cantidad:
                raise forms.ValidationError(
                    "Completa producto y piezas en cada renglón que uses."
                )
        if not consolidadas:
            raise forms.ValidationError(
                "Anuncia al menos un producto con sus piezas para registrar la entrega."
            )
        datos["lineas"] = list(consolidadas.items())
        return datos


class FormSugerenciaManual(forms.Form):
    """Sugerencia de mejora sobre un manual: llega a la Mesa como evento auditado."""

    texto = forms.CharField(
        label="Tu sugerencia",
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": "¿Qué cambiarías o qué le falta a este manual? La Mesa de Control lo lee.",
        }),
        error_messages={"required": "Cuéntanos tu sugerencia antes de enviarla."},
    )
