"""Formularios de Mesa de Control: alta/edición de cliente, tarifario y catálogo.

Mismo estilo que apps/portal/forms.py: forms.Form planos (no ModelForm) con
error_messages en español. El copy aquí es operativo — le habla a la Mesa,
no a Karina. Los querysets/choices con datos de negocio salen de settings.TORRE.
"""
from decimal import Decimal

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from apps.catalogo.models import SKU
from apps.core.models import Cliente

# Renglones fijos de la captura de ASN desde Mesa (sin JavaScript, sin formsets).
RENGLONES_ASN_MESA = 6

# Renglones fijos del pedido manual desde Mesa (sin JavaScript, sin formsets).
RENGLONES_PEDIDO_MANUAL = 6

# Claves del JSON `branding` que administra el formulario de cliente
# (en el form van con prefijo brand_; en el JSON van sin prefijo).
CLAVES_BRANDING = [
    "color_primario",
    "color_fondo",
    "logo_url",
    "nombre_publico",
    "whatsapp_soporte",
    "dominio_tienda",
]

# Campos simples del tarifario (los envio_* van anidados en envio_bloque).
CAMPOS_TARIFARIO_SIMPLES = [
    "almacenaje_mes",
    "alistamiento_pedido",
    "empaque_pedido",
    "bloque_kg",
    "recepcion_tarima",
    "minimo_mes",
]
ZONAS_ENVIO = ["local", "metro", "nacional"]


def _numero_json(valor):
    """Decimal → int o float para que el JSONField lo pueda guardar."""
    if valor == valor.to_integral_value():
        return int(valor)
    return float(valor)


class FormCliente(forms.Form):
    """Alta y edición de un cliente desde Mesa (cero /admin/)."""

    nombre = forms.CharField(
        label="Nombre",
        max_length=120,
        error_messages={"required": "El nombre del cliente es obligatorio."},
    )
    slug = forms.SlugField(
        label="Slug",
        max_length=50,
        required=False,
        help_text="Identificador corto para URLs y seed. Vacío = se genera del nombre.",
        error_messages={"invalid": "El slug solo lleva letras, números y guiones (ej. mezcal-nocturno)."},
    )
    razon_social = forms.CharField(label="Razón social", max_length=200, required=False)
    rfc = forms.CharField(label="RFC", max_length=13, required=False)
    contacto_nombre = forms.CharField(label="Nombre del contacto", max_length=120, required=False)
    contacto_whatsapp = forms.CharField(
        label="WhatsApp del contacto", max_length=20, required=False,
        help_text="Con lada, ej. +523121112233. Aquí llegan los avisos.",
    )
    buffer_stock = forms.IntegerField(
        label="Buffer de stock (unidades)",
        min_value=0,
        initial=0,
        error_messages={
            "required": "Captura el buffer de stock (0 si no aplica).",
            "min_value": "El buffer de stock no puede ser negativo.",
            "invalid": "El buffer de stock debe ser un número entero.",
        },
    )
    carrier_preferente = forms.ChoiceField(
        label="Carrier preferente",
        error_messages={"required": "Elige el carrier preferente."},
    )
    naked_packing_local = forms.BooleanField(
        label="Naked packing en entrega local", required=False, initial=True,
    )
    umbral_visto_bueno_mxn = forms.DecimalField(
        label="Visto bueno desde (MXN)",
        max_digits=8,
        decimal_places=2,
        min_value=0,
        initial=Decimal("2000"),
        error_messages={
            "required": "Captura el umbral de visto bueno en MXN.",
            "invalid": "El umbral de visto bueno debe ser un monto (ej. 2000.00).",
            "min_value": "El umbral de visto bueno no puede ser negativo.",
        },
    )
    guia_de_voz = forms.CharField(
        label="Guía de voz",
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": "Cómo habla la marca: tono, firma, qué jamás se dice.",
        }),
    )
    activo = forms.BooleanField(label="Activo", required=False, initial=True)

    # Branding aplanado (en el JSON van sin el prefijo brand_).
    brand_color_primario = forms.CharField(
        label="Color primario", max_length=20, required=False,
        widget=forms.TextInput(attrs={"placeholder": "#9E2B25"}),
    )
    brand_color_fondo = forms.CharField(
        label="Color de fondo", max_length=20, required=False,
        widget=forms.TextInput(attrs={"placeholder": "#F5EFE0"}),
    )
    brand_logo_url = forms.CharField(label="URL del logo", max_length=300, required=False)
    brand_nombre_publico = forms.CharField(label="Nombre público", max_length=120, required=False)
    brand_whatsapp_soporte = forms.CharField(label="WhatsApp de soporte", max_length=20, required=False)
    brand_dominio_tienda = forms.CharField(label="Dominio de la tienda", max_length=120, required=False)

    def __init__(self, *args, cliente=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cliente = cliente
        carriers = list(settings.TORRE["CARRIERS_COTIZAR"]) + ["local"]
        self.fields["carrier_preferente"].choices = [(c, c) for c in carriers]
        if cliente is not None:
            # El slug es la llave de idempotencia del seed: en edición no se toca.
            self.fields["slug"].disabled = True
            self.fields["slug"].initial = cliente.slug
            self.fields["slug"].help_text = "El slug no se cambia: es la llave del cliente en URLs y seed."

    def clean(self):
        datos = super().clean()
        if self.cliente is not None:
            datos["slug"] = self.cliente.slug
            return datos
        slug = datos.get("slug") or slugify(datos.get("nombre") or "")
        if not slug:
            raise forms.ValidationError("No se pudo generar el slug a partir del nombre; captúralo a mano.")
        if Cliente.objects.filter(slug=slug).exists():
            self.add_error(
                "slug",
                f"Ya existe un cliente con el slug '{slug}'. Elige otro slug (o ajusta el nombre).",
            )
        datos["slug"] = slug
        return datos

    def branding_actualizado(self, base=None):
        """JSON de branding: solo claves NO vacías, sin el prefijo brand_.

        Las claves que este formulario no administra (ej. color_texto del seed)
        se conservan tal cual.
        """
        branding = dict(base or {})
        for clave in CLAVES_BRANDING:
            valor = (self.cleaned_data.get(f"brand_{clave}") or "").strip()
            if valor:
                branding[clave] = valor
            else:
                branding.pop(clave, None)
        return branding

    def datos_cliente(self):
        """Campos del modelo Cliente (sin slug ni branding) ya limpios."""
        d = self.cleaned_data
        return {
            "nombre": d["nombre"].strip(),
            "razon_social": (d.get("razon_social") or "").strip(),
            "rfc": (d.get("rfc") or "").strip(),
            "contacto_nombre": (d.get("contacto_nombre") or "").strip(),
            "contacto_whatsapp": (d.get("contacto_whatsapp") or "").strip(),
            "buffer_stock": d["buffer_stock"],
            "carrier_preferente": d["carrier_preferente"],
            "naked_packing_local": d.get("naked_packing_local", False),
            "umbral_visto_bueno_mxn": d["umbral_visto_bueno_mxn"],
            "guia_de_voz": (d.get("guia_de_voz") or "").strip(),
            "activo": d.get("activo", False),
        }


class FormTarifario(forms.Form):
    """Editor del tarifario del cliente: guarda SOLO los overrides.

    Semántica dura (finanzas.tarifario_de): `Cliente.tarifario` lleva solo las
    claves que difieren del default de settings.TORRE["TARIFARIO_DEFAULT"];
    dict vacío = usa el default completo. Campo vacío = quitar el override.
    """

    almacenaje_mes = forms.DecimalField(label="Almacenaje al mes (MXN)", required=False, min_value=0)
    alistamiento_pedido = forms.DecimalField(label="Alistamiento por pedido (MXN)", required=False, min_value=0)
    empaque_pedido = forms.DecimalField(label="Empaque por pedido (MXN)", required=False, min_value=0)
    bloque_kg = forms.DecimalField(label="Tamaño del bloque de envío (kg)", required=False, min_value=0)
    envio_local = forms.DecimalField(label="Envío local por bloque (MXN)", required=False, min_value=0)
    envio_metro = forms.DecimalField(label="Envío metro por bloque (MXN)", required=False, min_value=0)
    envio_nacional = forms.DecimalField(label="Envío nacional por bloque (MXN)", required=False, min_value=0)
    recepcion_tarima = forms.DecimalField(label="Recepción por tarima (MXN)", required=False, min_value=0)
    minimo_mes = forms.DecimalField(label="Mínimo al mes (MXN)", required=False, min_value=0)

    def overrides(self):
        """Dict listo para Cliente.tarifario: solo lo que difiere del default."""
        default = settings.TORRE["TARIFARIO_DEFAULT"]
        # recepcion_tarima y minimo_mes pueden no existir aún en el default:
        # comparamos contra 0 en ese caso (.get con default 0).
        nuevo = {}
        for campo in CAMPOS_TARIFARIO_SIMPLES:
            valor = self.cleaned_data.get(campo)
            if valor is None:
                continue
            if Decimal(str(default.get(campo, 0))) != valor:
                nuevo[campo] = _numero_json(valor)
        defaults_envio = default.get("envio_bloque", {})
        envio = {}
        for zona in ZONAS_ENVIO:
            valor = self.cleaned_data.get(f"envio_{zona}")
            if valor is None:
                continue
            if Decimal(str(defaults_envio.get(zona, 0))) != valor:
                envio[zona] = _numero_json(valor)
        if envio:
            nuevo["envio_bloque"] = envio
        return nuevo


class FormSKU(forms.Form):
    """Alta/edición de un SKU del catálogo del cliente desde Mesa."""

    sku_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    codigo = forms.CharField(
        label="Código",
        max_length=60,
        error_messages={"required": "El código del SKU es obligatorio."},
    )
    descripcion = forms.CharField(
        label="Descripción",
        max_length=200,
        error_messages={"required": "La descripción del SKU es obligatoria."},
    )
    codigo_barras = forms.CharField(label="Código de barras", max_length=64, required=False)
    peso_gr = forms.IntegerField(
        label="Peso (gramos)",
        min_value=0,
        help_text="Pesa la unidad de venta completa.",
        error_messages={
            "required": "Captura el peso en gramos (pesa la unidad de venta completa).",
            "min_value": "El peso no puede ser negativo.",
            "invalid": "El peso debe ser un número entero de gramos.",
        },
    )
    largo_cm = forms.IntegerField(label="Largo (cm)", min_value=0, required=False, initial=0)
    ancho_cm = forms.IntegerField(label="Ancho (cm)", min_value=0, required=False, initial=0)
    alto_cm = forms.IntegerField(label="Alto (cm)", min_value=0, required=False, initial=0)
    requiere_lote = forms.BooleanField(label="Requiere lote", required=False, initial=True)
    unidad = forms.CharField(label="Unidad", max_length=20, required=False, initial="pieza")
    precio_declarado = forms.DecimalField(
        label="Precio declarado (MXN)", max_digits=10, decimal_places=2,
        min_value=0, required=False, initial=0,
        error_messages={"invalid": "El precio declarado debe ser un monto (ej. 250.00)."},
    )
    punto_reorden = forms.IntegerField(label="Punto de reorden", min_value=0, required=False, initial=0)
    empaques_divisibles = forms.IntegerField(
        label="Empaques divisibles",
        min_value=1,
        initial=1,
        help_text="En cuántas cajas puede dividirse para abaratar envío; 1 = indivisible.",
        error_messages={
            "required": "Captura en cuántos empaques puede dividirse (1 = indivisible).",
            "min_value": "Los empaques divisibles son mínimo 1 (indivisible).",
            "invalid": "Los empaques divisibles deben ser un número entero.",
        },
    )
    backorder_habilitado = forms.BooleanField(label="Backorder habilitado", required=False, initial=False)
    activo = forms.BooleanField(label="Activo", required=False, initial=True)

    def datos_sku(self):
        """Campos del modelo SKU (sin cliente) ya limpios."""
        d = self.cleaned_data
        return {
            "codigo": d["codigo"].strip(),
            "descripcion": d["descripcion"].strip(),
            "codigo_barras": (d.get("codigo_barras") or "").strip(),
            "peso_gr": d["peso_gr"],
            "largo_cm": d.get("largo_cm") or 0,
            "ancho_cm": d.get("ancho_cm") or 0,
            "alto_cm": d.get("alto_cm") or 0,
            "requiere_lote": d.get("requiere_lote", False),
            "unidad": (d.get("unidad") or "").strip() or "pieza",
            "precio_declarado": d.get("precio_declarado") if d.get("precio_declarado") is not None else Decimal("0"),
            "punto_reorden": d.get("punto_reorden") or 0,
            "empaques_divisibles": d["empaques_divisibles"],
            "backorder_habilitado": d.get("backorder_habilitado", False),
            "activo": d.get("activo", False),
        }


class FormPedidoManual(forms.Form):
    """Captura de un pedido manual: clientes sin Shopify (mayoreo, B2B).

    Patrón portal: recibe el cliente como primer argumento y acota los SKUs a
    ese cliente (solo activos). La dirección se captura por campos y direccion()
    la arma con el mismo shape que el shipping_address de la ingesta Shopify.
    """

    comprador_nombre = forms.CharField(
        label="Nombre del comprador",
        max_length=120,
        error_messages={"required": "Captura el nombre de quien recibe el pedido."},
    )
    comprador_tel = forms.CharField(
        label="Teléfono del comprador",
        max_length=20,
        required=False,
        help_text="Con lada, ej. +525512345678. Con teléfono le llega la confirmación.",
    )
    comprador_email = forms.EmailField(
        label="Correo del comprador",
        required=False,
        error_messages={"invalid": "Ese correo no se ve bien; revísalo (ej. nombre@dominio.com)."},
    )
    calle = forms.CharField(
        label="Calle y número",
        max_length=200,
        error_messages={"required": "Captura calle y número de la entrega."},
    )
    colonia = forms.CharField(label="Colonia", max_length=120, required=False)
    ciudad = forms.CharField(
        label="Ciudad",
        max_length=120,
        error_messages={"required": "Captura la ciudad de la entrega."},
    )
    estado = forms.CharField(label="Estado", max_length=120, required=False)
    cp = forms.CharField(
        label="Código postal",
        max_length=5,
        error_messages={"required": "Captura el código postal (5 dígitos)."},
    )
    valor_declarado = forms.DecimalField(
        label="Valor declarado (MXN)",
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        help_text="Vacío = suma del catálogo (precio declarado × piezas).",
        error_messages={
            "invalid": "El valor declarado debe ser un monto en MXN (ej. 1500.00).",
            "min_value": "El valor declarado no puede ser negativo.",
        },
    )
    nota_regalo = forms.CharField(
        label="Nota de regalo",
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Se imprime tal cual y va dentro de la caja; vacío si no lleva.",
        }),
    )

    def __init__(self, cliente, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cliente = cliente
        qs = SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo")
        for i in range(1, RENGLONES_PEDIDO_MANUAL + 1):
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
        for i in range(1, RENGLONES_PEDIDO_MANUAL + 1):
            yield self[f"sku_{i}"], self[f"cantidad_{i}"]

    def clean_cp(self):
        cp = (self.cleaned_data["cp"] or "").strip()
        if len(cp) != 5 or not cp.isdigit():
            raise forms.ValidationError("El código postal lleva 5 dígitos (ej. 01780).")
        return cp

    def clean(self):
        datos = super().clean()
        consolidadas = {}
        for i in range(1, RENGLONES_PEDIDO_MANUAL + 1):
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
                "Captura al menos un producto con sus piezas para crear el pedido."
            )
        datos["lineas"] = list(consolidadas.items())
        return datos

    def direccion(self):
        """Dict de dirección con el mismo shape que el shipping_address de Shopify."""
        from apps.envios.cotizador import CP_ESTADO  # lazy por contrato

        d = self.cleaned_data
        cp = d["cp"]
        return {
            "name": d["comprador_nombre"].strip(),
            "address1": d["calle"].strip(),
            "address2": (d.get("colonia") or "").strip(),
            "city": d["ciudad"].strip(),
            "province": (d.get("estado") or "").strip(),
            # Shopify manda province_code y es la clave que el adapter de
            # envia.com lee para el estado destino: derivarla del CP.
            "province_code": CP_ESTADO.get(cp[:2], ""),
            "zip": cp,
            "country": "México",
            "phone": (d.get("comprador_tel") or "").strip(),
        }


class FormAnuncioASNMesa(forms.Form):
    """Captura de una ASN que llegó por WhatsApp: la Mesa la registra en ≤15 min.

    Mismo patrón que portal.FormAnuncioASN: recibe el cliente como primer
    argumento, acota los SKUs a ese cliente y consolida renglones duplicados.
    """

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
        help_text="Las que el cliente dijo por WhatsApp; vacío si no las mencionó.",
        widget=forms.NumberInput(attrs={"placeholder": "0"}),
        error_messages={
            "min_value": "Las tarimas no pueden ser un número negativo.",
            "invalid": "Captura las tarimas con un número entero.",
        },
    )

    def __init__(self, cliente, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cliente = cliente
        qs = SKU.objects.filter(cliente=cliente, activo=True).order_by("codigo")
        for i in range(1, RENGLONES_ASN_MESA + 1):
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
        for i in range(1, RENGLONES_ASN_MESA + 1):
            yield self[f"sku_{i}"], self[f"cantidad_{i}"]

    def clean_fecha_compromiso(self):
        fecha = self.cleaned_data["fecha_compromiso"]
        if fecha < timezone.localdate():
            raise forms.ValidationError("La cita debe ser de hoy en adelante.")
        return fecha

    def clean(self):
        datos = super().clean()
        consolidadas = {}
        for i in range(1, RENGLONES_ASN_MESA + 1):
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
                "Captura al menos un producto con sus piezas para registrar la ASN."
            )
        datos["lineas"] = list(consolidadas.items())
        return datos
