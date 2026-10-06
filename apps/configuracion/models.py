"""Modelos de configuración de sistema: un modelo por ajuste, un renglón por valor."""
from django.db import models


class CorreoIncidencias(models.Model):
    """Un correo que recibe el aviso de cada incidencia que se abre (y de cada
    pedido detenido que se reanuda). Sin cliente: lista fija de Torre, recibe
    las de todos los clientes (se edita solo en el admin). Con cliente: recibe
    únicamente las de ese cliente (se edita en su ficha de Mesa)."""

    correo = models.EmailField()
    nombre = models.CharField(max_length=120, blank=True)
    cliente = models.ForeignKey(
        "core.Cliente", null=True, blank=True, on_delete=models.CASCADE, related_name="correos_incidencias",
        help_text="Vacío = recibe las incidencias de todos los clientes.",
    )
    activo = models.BooleanField(default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["cliente__nombre", "correo"]
        verbose_name = "correo de incidencias"
        verbose_name_plural = "correos de incidencias"
        constraints = [
            models.UniqueConstraint(fields=["correo", "cliente"], name="correoincidencias_unico_por_cliente"),
        ]

    def __str__(self):
        ambito = self.cliente.nombre if self.cliente_id else "todos los clientes"
        return f"{self.correo} · {ambito}"
