"""Inventario: saldos por estado, kardex append-only, recepciones (ASN), conteos y ajustes.

Modelo de estados del stock (BLUEPRINT §2.2, ledger):

    en_putaway → ubicado_vendible → (reservado) → en_empaque → despachado
    + cuarentena (dañado / retorno en revisión)

`reservado` es una CAPA sobre `ubicado_vendible`: la unidad reservada sigue
contada en su saldo vendible (así el push a Shopify empuja on_hand físico y
Shopify deriva available sin doble descuento). Por eso:

    disponible = suma(ubicado_vendible) − suma(reservado) − buffer del cliente

NADIE toca Saldo/Movimiento directo: todo pasa por `inventario.services` para
que el kardex, la auditoría y el push a Shopify siempre cuadren.
"""
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def siguiente_folio(modelo, prefijo, ancho=4):
    """Siguiente folio consecutivo `PREFIJO-####` para un modelo con campo `folio`."""
    numeros = []
    for folio in modelo.objects.filter(folio__startswith=f"{prefijo}-").values_list("folio", flat=True):
        sufijo = folio.rsplit("-", 1)[-1]
        if sufijo.isdigit():
            numeros.append(int(sufijo))
    return f"{prefijo}-{max(numeros, default=0) + 1:0{ancho}d}"


class Saldo(models.Model):
    """Cantidad actual de un SKU en una ubicación/lote/estado. Foto del presente;
    la historia vive en Movimiento (kardex)."""

    EN_PUTAWAY = "en_putaway"
    UBICADO_VENDIBLE = "ubicado_vendible"
    RESERVADO = "reservado"
    EN_EMPAQUE = "en_empaque"
    CUARENTENA = "cuarentena"
    ESTADOS = [
        (EN_PUTAWAY, "En put-away (recibido sin ubicar)"),
        (UBICADO_VENDIBLE, "Ubicado vendible"),
        (RESERVADO, "Reservado (capa sobre vendible)"),
        (EN_EMPAQUE, "En empaque"),
        (CUARENTENA, "Cuarentena"),
    ]

    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="saldos")
    ubicacion = models.ForeignKey("catalogo.Ubicacion", on_delete=models.PROTECT, related_name="saldos")
    lote = models.ForeignKey("catalogo.Lote", null=True, blank=True, on_delete=models.PROTECT, related_name="saldos")
    estado = models.CharField(max_length=20, choices=ESTADOS)
    cantidad = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("sku", "ubicacion", "lote", "estado")]
        ordering = ["sku", "ubicacion"]
        verbose_name = "saldo"
        verbose_name_plural = "saldos"

    def __str__(self):
        lote = f" L:{self.lote.codigo}" if self.lote_id else ""
        return f"{self.sku.codigo} @{self.ubicacion.codigo}{lote} [{self.estado}] = {self.cantidad}"


class Movimiento(models.Model):
    """Kardex append-only. Nadie edita historia: se corrige con otro movimiento."""

    RECEPCION = "recepcion"
    PUTAWAY = "putaway"
    RESERVA = "reserva"
    PICK = "pick"
    SALIDA = "salida"
    AJUSTE = "ajuste"
    RETORNO = "retorno"
    MERMA = "merma"
    CONTEO = "conteo"
    TIPOS = [
        (RECEPCION, "Recepción"),
        (PUTAWAY, "Put-away"),
        (RESERVA, "Reserva"),
        (PICK, "Pick"),
        (SALIDA, "Salida"),
        (AJUSTE, "Ajuste"),
        (RETORNO, "Retorno"),
        (MERMA, "Merma"),
        (CONTEO, "Conteo"),
    ]

    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="movimientos")
    lote = models.ForeignKey("catalogo.Lote", null=True, blank=True, on_delete=models.PROTECT, related_name="movimientos")
    tipo = models.CharField(max_length=12, choices=TIPOS)
    delta = models.IntegerField(help_text="Cantidad con signo: negativo = sale físico")
    estado_origen = models.CharField(max_length=20, blank=True)
    estado_destino = models.CharField(max_length=20, blank=True)
    referencia = models.CharField(max_length=40, blank=True, help_text="PED-x / ASN-x / AJU-x / CON-x")
    actor = models.CharField(max_length=120, blank=True)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-ts", "-id"]
        indexes = [models.Index(fields=["sku", "ts"])]
        verbose_name = "movimiento (kardex)"
        verbose_name_plural = "movimientos (kardex)"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("El kardex es append-only: un movimiento no se edita, se compensa con otro.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("El kardex es append-only: un movimiento no se borra.")

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} {self.sku.codigo} {self.tipo} {self.delta:+d} ({self.referencia})"


class OrdenEntrada(models.Model):
    """ASN: anuncio de mercancía entrante. El reloj SLA de recepción corre de
    fin de descarga (ts_descarga_fin) a todo ubicado y vendible (ts_vendible)."""

    ANUNCIADA = "ANUNCIADA"
    EN_RECEPCION = "EN_RECEPCION"
    RECIBIDA = "RECIBIDA"
    CERRADA = "CERRADA"
    ESTADOS = [
        (ANUNCIADA, "Anunciada"),
        (EN_RECEPCION, "En recepción"),
        (RECIBIDA, "Recibida"),
        (CERRADA, "Cerrada (todo vendible)"),
    ]
    TRANSICIONES = {
        ANUNCIADA: {EN_RECEPCION},
        EN_RECEPCION: {RECIBIDA},
        RECIBIDA: {CERRADA},
        CERRADA: set(),
    }

    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="ordenes_entrada")
    folio = models.CharField(max_length=20, unique=True, blank=True)
    estado = models.CharField(max_length=15, choices=ESTADOS, default=ANUNCIADA)
    fecha_compromiso = models.DateField(null=True, blank=True, help_text="Cita de llegada (mar/jue)")
    tarimas = models.PositiveSmallIntegerField(default=0, help_text="Tarimas anunciadas por el cliente")
    tarimas_recibidas = models.PositiveSmallIntegerField(default=0)
    ts_descarga_fin = models.DateTimeField(null=True, blank=True)
    ts_vendible = models.DateTimeField(null=True, blank=True, help_text="Cuándo quedó todo ubicado")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "orden de entrada (ASN)"
        verbose_name_plural = "órdenes de entrada (ASN)"

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = siguiente_folio(OrdenEntrada, "ASN")
        super().save(*args, **kwargs)

    def transicionar(self, nuevo, actor=None, motivo=""):
        """Valida la transición, estampa relojes y registra el evento de auditoría."""
        permitidos = self.TRANSICIONES.get(self.estado, set())
        if nuevo not in permitidos:
            raise ValueError(f"Transición inválida en {self.folio}: {self.estado} → {nuevo}.")
        anterior = self.estado
        self.estado = nuevo
        ahora = timezone.now()
        if nuevo == self.RECIBIDA and self.ts_descarga_fin is None:
            self.ts_descarga_fin = ahora
        if nuevo == self.CERRADA and self.ts_vendible is None:
            self.ts_vendible = ahora
        self.save()
        from apps.core.services import registrar_evento
        registrar_evento(
            "asn", self.folio, f"transicion:{anterior}->{nuevo}",
            actor=actor, cliente=self.cliente, delta={"de": anterior, "a": nuevo}, motivo=motivo,
        )
        return self

    # ── Reloj SLA de recepción (settings.TORRE) ──
    @property
    def sla_limite(self):
        """Momento límite para quedar vendible; None si la descarga no ha terminado."""
        if self.ts_descarga_fin is None:
            return None
        horas = settings.TORRE["SLA_RECEPCION_HORAS_CONTRACTUAL"]
        return self.ts_descarga_fin + timedelta(hours=horas)

    @property
    def sla_restante(self):
        """Tiempo que queda del reloj (negativo si ya venció); None si no ha iniciado."""
        limite = self.sla_limite
        if limite is None:
            return None
        fin = self.ts_vendible or timezone.now()
        return limite - fin

    @property
    def sla_vencido(self):
        restante = self.sla_restante
        return restante is not None and restante < timedelta(0)

    def __str__(self):
        return f"{self.folio} · {self.cliente} · {self.get_estado_display()}"


class LineaASN(models.Model):
    """Línea de una orden de entrada: lo anunciado contra lo que realmente llegó."""

    orden = models.ForeignKey(OrdenEntrada, on_delete=models.CASCADE, related_name="lineas")
    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="lineas_asn")
    cantidad_anunciada = models.PositiveIntegerField()
    cantidad_recibida = models.PositiveIntegerField(default=0)
    cantidad_danada = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["orden", "id"]
        verbose_name = "línea de ASN"
        verbose_name_plural = "líneas de ASN"

    @property
    def completa(self):
        return self.cantidad_recibida + self.cantidad_danada >= self.cantidad_anunciada

    def __str__(self):
        return f"{self.orden.folio} · {self.sku.codigo} ({self.cantidad_recibida}/{self.cantidad_anunciada})"


class Conteo(models.Model):
    """Conteo ciego de un SKU. El esperado lo calcula el sistema, no el contador."""

    folio = models.CharField(max_length=20, unique=True, blank=True)
    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="conteos")
    contador = models.CharField(max_length=120)
    esperado = models.PositiveIntegerField()
    contado = models.PositiveIntegerField()
    ts = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-ts"]
        verbose_name = "conteo"
        verbose_name_plural = "conteos"

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = siguiente_folio(Conteo, "CON")
        super().save(*args, **kwargs)

    @property
    def diferencia(self):
        return self.contado - self.esperado

    def __str__(self):
        return f"{self.folio} · {self.sku.codigo}: {self.contado}/{self.esperado} ({self.diferencia:+d})"


class Ajuste(models.Model):
    """Ajuste de inventario con doble firma obligatoria (dos PINs, dos personas).

    Ningún ajuste sin autor y causa: motivo de catálogo cerrado desde la semana 1.
    """

    MOTIVO_CONTEO = "conteo"
    MOTIVO_DANO = "dano_bodega"
    MOTIVO_CADUCIDAD = "caducidad"
    MOTIVO_MERMA = "merma_operativa"
    MOTIVO_ERROR_RECEPCION = "error_recepcion"
    MOTIVO_ROBO = "robo_extravio"
    MOTIVO_RECONCILIACION = "reconciliacion"
    MOTIVOS = [
        (MOTIVO_CONTEO, "Diferencia detectada en conteo"),
        (MOTIVO_DANO, "Daño en bodega"),
        (MOTIVO_CADUCIDAD, "Producto caducado"),
        (MOTIVO_MERMA, "Merma operativa"),
        (MOTIVO_ERROR_RECEPCION, "Corrección de recepción"),
        (MOTIVO_ROBO, "Robo o extravío"),
        (MOTIVO_RECONCILIACION, "Reconciliación con cliente"),
    ]

    folio = models.CharField(max_length=20, unique=True, blank=True)
    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="ajustes")
    lote = models.ForeignKey("catalogo.Lote", null=True, blank=True, on_delete=models.PROTECT, related_name="ajustes")
    delta = models.IntegerField(help_text="Con signo: negativo quita, positivo agrega")
    motivo = models.CharField(max_length=20, choices=MOTIVOS)
    autorizo_1 = models.CharField(max_length=120, help_text="Primera firma (username)")
    autorizo_2 = models.CharField(max_length=120, help_text="Segunda firma (username, distinta)")
    conteo = models.ForeignKey(Conteo, null=True, blank=True, on_delete=models.PROTECT, related_name="ajustes")
    incidencia_ref = models.CharField(max_length=20, blank=True, help_text="Folio INC-AAAA-#### si aplica")
    ts = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-ts"]
        verbose_name = "ajuste"
        verbose_name_plural = "ajustes"

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = siguiente_folio(Ajuste, "AJU")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.folio} · {self.sku.codigo} {self.delta:+d} ({self.get_motivo_display()})"


class TareaConteo(models.Model):
    """Tarea del conteo cíclico diario (3 SKUs a las 9:00, jefe de bodega).

    La crea el command `conteo_ciclico`; el piso la resuelve con
    `services.registrar_conteo`, que la marca completada.
    """

    PENDIENTE = "pendiente"
    COMPLETADA = "completada"
    ESTADOS = [(PENDIENTE, "Pendiente"), (COMPLETADA, "Completada")]

    fecha = models.DateField(default=timezone.localdate, db_index=True)
    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="tareas_conteo")
    estado = models.CharField(max_length=12, choices=ESTADOS, default=PENDIENTE)
    conteo = models.ForeignKey(Conteo, null=True, blank=True, on_delete=models.PROTECT, related_name="tareas")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("fecha", "sku")]
        ordering = ["-fecha", "sku__codigo"]
        verbose_name = "tarea de conteo"
        verbose_name_plural = "tareas de conteo"

    def __str__(self):
        return f"{self.fecha} · {self.sku.codigo} · {self.get_estado_display()}"
