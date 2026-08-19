"""Catálogo maestro: SKUs por cliente, ubicaciones del Local 380 E y lotes.

El catálogo es la referencia estable del inventario: aquí no hay cantidades,
solo identidad. Las cantidades viven en `inventario.Saldo` (kardex aparte).
"""
from django.db import models


class SKU(models.Model):
    """Producto de un cliente. El código es único POR cliente (multi-tenant)."""

    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="skus")
    codigo = models.CharField(max_length=60)
    codigo_barras = models.CharField(max_length=64, blank=True)
    descripcion = models.CharField(max_length=200)
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
