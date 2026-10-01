"""Modelos de finanzas: reembolsos de paquetería por guía."""
from django.db import models


class ReembolsoGuia(models.Model):
    """Dinero que la paquetería nos devolvió por UNA guía (Chema 2026-10-01)
    y que se le descuenta al cliente: la tarifa que pagó por esa guía, no lo
    que nos devolvió el carrier. Nace solo al cancelar una guía en Torre
    (origen "cancelacion": el carrier ya no la cobra), al cobrar una
    reclamación o a mano desde Mesa → Pedido. La FECHA decide el corte en que
    se descuenta: si cae en el mismo corte en que se cobró la guía se neta en
    su fila; si la guía se cobró en un corte anterior, aparece como línea
    negativa "Reembolsos de cortes anteriores" en el corte en que llegó. Un
    corte pasado nunca cambia por algo que llegó después."""

    ORIGEN_CANCELACION = "cancelacion"
    ORIGEN_RECLAMACION = "reclamacion"
    ORIGEN_AJUSTE = "ajuste"
    ORIGENES = [
        (ORIGEN_CANCELACION, "Guía cancelada"),
        (ORIGEN_RECLAMACION, "Reclamación pagada"),
        (ORIGEN_AJUSTE, "Ajuste manual"),
    ]

    guia = models.ForeignKey("envios.Guia", on_delete=models.PROTECT, related_name="reembolsos")
    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="reembolsos_guia")
    monto = models.DecimalField(max_digits=10, decimal_places=2, help_text="MXN sin IVA: lo que se le descuenta al cliente")
    fecha = models.DateTimeField(db_index=True, help_text="Cuándo nos reembolsó la paquetería (o se canceló la guía): decide el corte")
    origen = models.CharField(max_length=12, choices=ORIGENES, default=ORIGEN_AJUSTE)
    nota = models.CharField(max_length=200, blank=True, default="")
    incidencia = models.ForeignKey(
        "incidencias.Incidencia", null=True, blank=True, on_delete=models.SET_NULL, related_name="reembolsos_guia",
    )
    registrado_por = models.CharField(max_length=80, blank=True, default="")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["fecha", "pk"]
        verbose_name = "reembolso de guía"
        verbose_name_plural = "reembolsos de guía"

    def __str__(self):
        return f"{self.guia.numero} · ${self.monto} · {self.get_origen_display()}"
