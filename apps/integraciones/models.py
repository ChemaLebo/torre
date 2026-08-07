"""Integraciones: tiendas Shopify por cliente, idempotencia de webhooks y cola de push.

Regla de un solo escritor (BLUEPRINT §2.1): Torre es el ÚNICO servicio con
credenciales de escritura de inventario en Shopify. Este módulo es esa pluma.
"""
from django.db import models


class Tienda(models.Model):
    """Tienda conectada de un cliente. Multi-tienda: un cliente puede tener N."""

    PLATAFORMA_SHOPIFY = "shopify"
    PLATAFORMAS = [(PLATAFORMA_SHOPIFY, "Shopify")]

    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="tiendas")
    plataforma = models.CharField(max_length=20, choices=PLATAFORMAS, default=PLATAFORMA_SHOPIFY)
    dominio = models.CharField(max_length=120, unique=True, help_text="p. ej. colima-mx.myshopify.com")
    token = models.CharField(
        max_length=200, blank=True,
        help_text="Access token de la custom app. Vacío = modo mock (dev).",
    )
    location_id = models.CharField(
        max_length=60, blank=True,
        help_text="Location de Shopify que operamos (id numérico o GID)",
    )
    activo = models.BooleanField(default=True)
    checkpoint_reconciliacion = models.DateTimeField(
        null=True, blank=True,
        help_text="Última reconciliación de pedidos (updated_at_min del siguiente polling)",
    )
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["cliente__nombre", "dominio"]
        verbose_name = "tienda"

    def __str__(self):
        return f"{self.dominio} · {self.cliente}"


class WebhookEvento(models.Model):
    """Evento de entrada de Shopify. `webhook_id` único = idempotencia de ingesta.

    Un reintento de Shopify (o un replay manual) con el mismo webhook_id es no-op.
    """

    ORIGEN_WEBHOOK = "webhook"
    ORIGEN_RECONCILIACION = "reconciliacion"
    ORIGEN_MANUAL = "manual"
    ORIGENES = [
        (ORIGEN_WEBHOOK, "Webhook"),
        (ORIGEN_RECONCILIACION, "Reconciliación"),
        (ORIGEN_MANUAL, "Manual"),
    ]

    tienda = models.ForeignKey(Tienda, on_delete=models.CASCADE, related_name="webhooks")
    webhook_id = models.CharField(max_length=120, unique=True)
    topic = models.CharField(max_length=60, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    procesado = models.BooleanField(default=False)
    origen = models.CharField(max_length=15, choices=ORIGENES, default=ORIGEN_WEBHOOK)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-ts"]
        verbose_name = "evento de webhook"
        verbose_name_plural = "eventos de webhook"

    def __str__(self):
        return f"{self.topic} [{self.webhook_id}] {'✓' if self.procesado else '·'}"


class SyncLog(models.Model):
    """Bitácora de sincronización por tienda y dirección. Alimenta la salud de sync
    del portal ("hace X min" — nunca la palabra 'tiempo real')."""

    DIRECCION_INGESTA = "ingesta"
    DIRECCION_PUSH = "push"
    DIRECCIONES = [(DIRECCION_INGESTA, "Ingesta"), (DIRECCION_PUSH, "Push")]

    RESULTADO_OK = "ok"
    RESULTADO_ERROR = "error"
    RESULTADOS = [(RESULTADO_OK, "OK"), (RESULTADO_ERROR, "Error")]

    tienda = models.ForeignKey(Tienda, on_delete=models.CASCADE, related_name="sync_logs")
    direccion = models.CharField(max_length=10, choices=DIRECCIONES)
    resultado = models.CharField(max_length=10, choices=RESULTADOS)
    detalle = models.TextField(blank=True)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-ts"]
        verbose_name = "log de sincronización"
        verbose_name_plural = "logs de sincronización"

    def __str__(self):
        return f"{self.tienda.dominio} {self.direccion} {self.resultado} {self.ts:%H:%M}"


class PushInventarioPendiente(models.Model):
    """Cola en BD del push de inventario: un renglón pendiente por SKU.

    Inventario encola tras CUALQUIER cambio de disponible; `push_inventario()`
    la drena. La unicidad por SKU hace el encolado idempotente (debounce natural).
    """

    sku = models.ForeignKey("catalogo.SKU", on_delete=models.CASCADE, related_name="pushes_pendientes")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado"]
        constraints = [
            models.UniqueConstraint(fields=["sku"], name="push_pendiente_sku_unico"),
        ]
        verbose_name = "push de inventario pendiente"
        verbose_name_plural = "pushes de inventario pendientes"

    def __str__(self):
        return f"push pendiente · {self.sku} · {self.creado:%Y-%m-%d %H:%M}"
