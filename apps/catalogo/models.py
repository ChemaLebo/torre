"""Catálogo maestro: SKUs por cliente, ubicaciones del Local 380 E y lotes.

El catálogo es la referencia estable del inventario: aquí no hay cantidades,
solo identidad. Las cantidades viven en `inventario.Saldo` (kardex aparte).
"""
from django.db import models


class Categoria(models.Model):
    """Categoría del catálogo de UN cliente. "Otros" es la default de todo SKU."""

    OTROS = "Otros"

    cliente = models.ForeignKey("core.Cliente", on_delete=models.CASCADE, related_name="categorias")
    nombre = models.CharField(max_length=60)

    class Meta:
        unique_together = [("cliente", "nombre")]
        ordering = ["cliente", "nombre"]
        verbose_name = "categoría"
        verbose_name_plural = "categorías"

    def __str__(self):
        return f"{self.nombre} · {self.cliente.nombre}"

    @classmethod
    def otros_de(cls, cliente):
        """La categoría default del cliente (se crea si aún no existe)."""
        categoria, _ = cls.objects.get_or_create(cliente=cliente, nombre=cls.OTROS)
        return categoria


class SKU(models.Model):
    """Producto de un cliente. El código es único POR cliente (multi-tenant)."""

    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="skus")
    categoria = models.ForeignKey(
        Categoria, null=True, blank=True, on_delete=models.PROTECT, related_name="skus",
        help_text="Vacío = Otros (la default del cliente)",
    )
    codigo = models.CharField(max_length=60)
    codigo_barras = models.CharField(max_length=64, blank=True)
    descripcion = models.CharField(max_length=200)
    variante = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Sabor/tamaño/presentación cuando el producto tiene variantes (ej. 500 g); vacío si no",
    )
    peso_gr = models.PositiveIntegerField(default=0, help_text="Peso unitario en gramos")
    largo_cm = models.PositiveIntegerField(default=0)
    ancho_cm = models.PositiveIntegerField(default=0)
    alto_cm = models.PositiveIntegerField(default=0)
    requiere_lote = models.BooleanField(default=True)
    unidad = models.CharField(max_length=20, default="pieza")
    precio_declarado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    punto_reorden = models.PositiveIntegerField(default=0)
    empaques_divisibles = models.PositiveIntegerField(
        default=1,
        help_text="En cuántas cajas puede reempacarse una unidad de venta para "
                  "abaratar el envío (ej. caja de 24 → 2 medias de 12). 1 = indivisible.",
    )
    backorder_habilitado = models.BooleanField(default=False)
    fecha_resurtido = models.DateField(null=True, blank=True)
    activo = models.BooleanField(default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("cliente", "codigo")]
        ordering = ["cliente", "codigo"]
        verbose_name = "SKU"
        verbose_name_plural = "SKUs"

    def __str__(self):
        return f"{self.codigo} · {self.descripcion}"


def opciones_sku_agrupadas(cliente):
    """Choices de SKU agrupadas por categoría para selects con optgroup.

    [(categoria, [(pk, "Descripción — SKU"), ...]), ...] — categorías en orden
    alfabético con Otros al final; productos por descripción. SKU sin categoría
    cuenta como Otros.
    """
    grupos = {}
    qs = SKU.objects.filter(cliente=cliente, activo=True).select_related("categoria").order_by("descripcion")
    for sku in qs:
        nombre = sku.categoria.nombre if sku.categoria else Categoria.OTROS
        etiqueta = f"{sku.descripcion} ({sku.variante})" if sku.variante else sku.descripcion
        grupos.setdefault(nombre, []).append((sku.pk, f"{etiqueta} — {sku.codigo}"))
    orden = sorted(grupos, key=lambda n: (n == Categoria.OTROS, n.lower()))
    return [(nombre, grupos[nombre]) for nombre in orden]


class Ubicacion(models.Model):
    """Ubicación física del Local 380 E. Los corrales (tipo salida) declaran en
    `carriers` qué guías se estacionan ahí; uno sin carriers es el comodín."""

    RECEPCION = "recepcion"
    PICKING = "picking"
    RESERVA = "reserva"
    MERMA = "merma"
    RETORNO = "retorno"
    SALIDA = "salida"
    TIPOS = [
        (RECEPCION, "Recepción"),
        (PICKING, "Picking"),
        (RESERVA, "Reserva"),
        (MERMA, "Merma"),
        (RETORNO, "Retorno"),
        (SALIDA, "Salida (corral)"),
    ]

    codigo = models.CharField(max_length=20, unique=True, help_text="Ej. A-01-2, REC-01, SAL-PQX")
    tipo = models.CharField(max_length=12, choices=TIPOS)
    carriers = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Solo corrales: carriers separados por coma (vacío = comodín)",
    )
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["codigo"]
        verbose_name = "ubicación"
        verbose_name_plural = "ubicaciones"

    def __str__(self):
        return f"{self.codigo} ({self.get_tipo_display()})"

    def lista_carriers(self):
        """Carriers declarados del corral, ya limpios."""
        return [c.strip() for c in (self.carriers or "").split(",") if c.strip()]


class Lote(models.Model):
    """Lote de producción de un SKU; la caducidad manda el FEFO del picking."""

    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name="lotes")
    codigo = models.CharField(max_length=60)
    fecha_caducidad = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = [("sku", "codigo")]
        ordering = ["fecha_caducidad", "codigo"]
        verbose_name = "lote"
        verbose_name_plural = "lotes"

    def __str__(self):
        cad = f" · cad {self.fecha_caducidad:%d/%b/%Y}" if self.fecha_caducidad else ""
        return f"{self.sku.codigo} L:{self.codigo}{cad}"
