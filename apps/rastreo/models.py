"""Rastreo público brandeado — el acceso del comprador final a su pedido.

Un `AccesoRastreo` es la llave pública de UN pedido: un token corto, único y
no enumerable (secrets.token_urlsafe) que viaja por WhatsApp/email. Quien tiene
el token ve la página brandeada del cliente; quien no, ve un 404 genérico.

Regla de oro: esta app es la cara del CLIENTE (Cervecería Colima), no de
Torre/WOP. Nada de datos sensibles del comprador ni del 3PL en lo público.
"""
from django.db import models


class AccesoRastreo(models.Model):
    """Token público de rastreo, uno por pedido (OneToOne).

    12 caracteres URL-safe generados con `secrets` (ver services._nuevo_token):
    ~2^71 combinaciones — no se enumera ni por fuerza bruta con el throttle
    por IP de las vistas públicas.
    """

    pedido = models.OneToOneField(
        "pedidos.Pedido", on_delete=models.CASCADE, related_name="acceso_rastreo",
    )
    token = models.CharField(max_length=12, unique=True, editable=False)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "acceso de rastreo"
        verbose_name_plural = "accesos de rastreo"

    def __str__(self):
        return f"{self.token} → {self.pedido.folio}"


class AccesoEtiqueta(models.Model):
    """Token público de la etiqueta impresa, uno por guía (OneToOne).

    El QR de la etiqueta codifica /r/e/<token>/: la página del REPARTIDOR
    (ubicación exacta + botón de auxilio). Mismo esquema no enumerable que
    AccesoRastreo (12 chars con secrets, ver services._nuevo_token_etiqueta).
    """

    guia = models.OneToOneField(
        "envios.Guia", on_delete=models.CASCADE, related_name="acceso_etiqueta",
    )
    token = models.CharField(max_length=12, unique=True, editable=False)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "acceso de etiqueta"
        verbose_name_plural = "accesos de etiqueta"

    def __str__(self):
        return f"{self.token} → {self.guia.numero or self.guia.pk}"
