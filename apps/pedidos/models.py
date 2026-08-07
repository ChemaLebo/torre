"""Pedidos — la máquina de estados canónica de Torre (BLUEPRINT §2.3).

Un pedido nace de Shopify (o manual), reserva stock al ingerir y avanza por
transiciones explícitas. Cada transición estampa su timestamp y queda en el
event log (core.EventoAuditoria): nada cambia de estado en silencio y está
prohibido el estado genérico "en proceso".
"""
from django.db import IntegrityError, models, transaction

from apps.core.services import registrar_evento


class Pedido(models.Model):
    """Pedido de venta: la entidad que Karina ve en el portal y el piso mueve con escaneos."""

    # ── Estados canónicos ──
    PENDIENTE = "PENDIENTE"
    EN_PICKING = "EN_PICKING"
    EMPACADO = "EMPACADO"
    GUIA_GENERADA = "GUIA_GENERADA"
    RECOLECTADO = "RECOLECTADO"
    EN_TRANSITO = "EN_TRANSITO"
    ENTREGADO = "ENTREGADO"
    ENTREGA_PRESUNTA = "ENTREGA_PRESUNTA"
    PARCIALMENTE_DESPACHADO = "PARCIALMENTE_DESPACHADO"
    CANCELACION_PENDIENTE = "CANCELACION_PENDIENTE"
    CANCELADO = "CANCELADO"
    RETORNADO = "RETORNADO"

    ESTADOS = [
        (PENDIENTE, "Pendiente"),
        (EN_PICKING, "En picking"),
        (EMPACADO, "Empacado"),
        (GUIA_GENERADA, "Guía generada"),
        (RECOLECTADO, "Recolectado"),
        (EN_TRANSITO, "En tránsito"),
        (ENTREGADO, "Entregado"),
        (ENTREGA_PRESUNTA, "Entrega presunta"),
        (PARCIALMENTE_DESPACHADO, "Parcialmente despachado"),
        (CANCELACION_PENDIENTE, "Cancelación pendiente"),
        (CANCELADO, "Cancelado"),
        (RETORNADO, "Retornado"),
    ]

    # Tabla explícita de transiciones permitidas. Todo lo demás truena con ValueError.
    TRANSICIONES = {
        PENDIENTE: {EN_PICKING, CANCELADO},
        EN_PICKING: {EMPACADO, CANCELACION_PENDIENTE},
        EMPACADO: {GUIA_GENERADA, CANCELACION_PENDIENTE},
        # GUIA_GENERADA → EN_TRANSITO cubre el caso raro de guía indexada por el
        # carrier antes de nuestro escaneo de salida; RECOLECTADO sigue siendo el
        # paso autoritativo del flujo normal (escaneo + manifiesto).
        GUIA_GENERADA: {RECOLECTADO, PARCIALMENTE_DESPACHADO, EN_TRANSITO, CANCELACION_PENDIENTE},
        RECOLECTADO: {EN_TRANSITO, ENTREGADO, PARCIALMENTE_DESPACHADO},
        EN_TRANSITO: {ENTREGADO, ENTREGA_PRESUNTA, RETORNADO},
        ENTREGA_PRESUNTA: {ENTREGADO, RETORNADO},
        PARCIALMENTE_DESPACHADO: {EN_TRANSITO, ENTREGADO, RETORNADO},
        CANCELACION_PENDIENTE: {CANCELADO},
        ENTREGADO: set(),
        CANCELADO: set(),
        RETORNADO: set(),
    }

    # Estado destino → campo timestamp que se estampa al entrar (solo la primera vez).
    TIMESTAMPS_TRANSICION = {
        EN_PICKING: "ts_picking",
        EMPACADO: "ts_empacado",
        GUIA_GENERADA: "ts_guia",
        RECOLECTADO: "ts_recolectado",
        EN_TRANSITO: "ts_en_transito",
        ENTREGADO: "ts_entregado",
    }

    ORIGENES = [
        ("webhook", "Webhook"),
        ("reconciliacion", "Reconciliación"),
        ("manual", "Manual"),
    ]

    tienda = models.ForeignKey(
        "integraciones.Tienda", null=True, blank=True, on_delete=models.PROTECT,
        related_name="pedidos", help_text="Vacío solo para pedidos manuales sin tienda",
    )
    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="pedidos")
    shopify_order_id = models.CharField(max_length=40, blank=True, default="")
    folio = models.CharField(max_length=12, unique=True, blank=True, editable=False)
    origen = models.CharField(max_length=15, choices=ORIGENES, default="webhook")
    comprador_nombre = models.CharField(max_length=120, blank=True)
    comprador_tel = models.CharField(max_length=20, blank=True)
    comprador_email = models.EmailField(blank=True)
    direccion = models.JSONField(default=dict, blank=True)
    cp = models.CharField("código postal", max_length=10, blank=True)
    es_local = models.BooleanField(default=False, help_text="CP de Colima (28xxx): entrega local propia")
    valor_declarado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    nota_regalo = models.TextField(blank=True)
    estado = models.CharField(max_length=30, choices=ESTADOS, default=PENDIENTE, db_index=True)
    corte_vigente_al_ingreso = models.TimeField(
        null=True, blank=True,
        help_text="Corte contractual vigente al ingerir: evidencia auditable del SLA de salida",
    )
    # Timestamps por transición (se estampan en transicionar, solo la primera vez)
    ts_picking = models.DateTimeField(null=True, blank=True)
    ts_empacado = models.DateTimeField(null=True, blank=True)
    ts_guia = models.DateTimeField(null=True, blank=True)
    ts_recolectado = models.DateTimeField(null=True, blank=True)
    ts_en_transito = models.DateTimeField(null=True, blank=True)
    ts_entregado = models.DateTimeField(null=True, blank=True)
    # Flag ortogonal: un pedido EN_TRANSITO puede tener incidencia; no es estado terminal.
    incidencia_activa = models.BooleanField(default=False)
    peso_esperado_gr = models.PositiveIntegerField(default=0)
    peso_real_gr = models.PositiveIntegerField(null=True, blank=True)
    creado = models.DateTimeField(auto_now_add=True, db_index=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado"]
        unique_together = [("tienda", "shopify_order_id")]
        verbose_name = "pedido"
        verbose_name_plural = "pedidos"

    def __str__(self):
        return f"{self.folio} · {self.get_estado_display()}"

    # ── Folio PED-##### autoincremental ──

    @staticmethod
    def _siguiente_folio():
        # Solo folios PED-*: un folio ajeno (import, demo) en la última fila
        # no debe descarrilar la secuencia.
        ultimo = (
            Pedido.objects.filter(folio__startswith="PED-")
            .order_by("-folio")
            .values_list("folio", flat=True)
            .first()
        )
        numero = 0
        if ultimo:
            try:
                numero = int(str(ultimo).split("-")[-1])
            except (TypeError, ValueError):
                numero = 0
        return f"PED-{numero + 1:05d}"

    def save(self, *args, **kwargs):
        if self.folio:
            return super().save(*args, **kwargs)
        # Reintento corto por si dos ingestas simultáneas calculan el mismo folio.
        for intento in range(5):
            self.folio = self._siguiente_folio()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if intento == 4:
                    raise
                self.folio = ""

    # ── Máquina de estados ──

    def transicionar(self, nuevo, actor=None, motivo=""):
        """Valida la transición, estampa timestamp, guarda y registra evento.

        Transición inválida → ValueError con mensaje claro. Único camino
        legítimo para cambiar `estado`.
        """
        estados_validos = {clave for clave, _ in self.ESTADOS}
        if nuevo not in estados_validos:
            raise ValueError(f"Estado desconocido: {nuevo}.")
        permitidos = self.TRANSICIONES.get(self.estado, set())
        if nuevo not in permitidos:
            raise ValueError(
                f"Transición inválida: el pedido {self.folio or self.pk} no puede pasar "
                f"de {self.get_estado_display()} a {dict(self.ESTADOS)[nuevo]}."
            )
        anterior = self.estado
        self.estado = nuevo
        campos = ["estado", "actualizado"]
        campo_ts = self.TIMESTAMPS_TRANSICION.get(nuevo)
        if campo_ts and getattr(self, campo_ts) is None:
            from django.utils import timezone
            setattr(self, campo_ts, timezone.now())
            campos.append(campo_ts)
        self.save(update_fields=campos)
        registrar_evento(
            "pedido", self.pk, "cambio_estado",
            actor=actor, cliente=self.cliente,
            delta={"de": anterior, "a": nuevo}, motivo=motivo,
        )
        return self

    # ── Ayudas de dominio ──

    @property
    def lineas_completas(self):
        """True si toda línea tiene su cantidad completa pickeada."""
        return all(l.cantidad_pickeada >= l.cantidad for l in self.lineas.all())

    @property
    def cajas_cerradas_completas(self):
        """True si cada caja EMPACADO/DESPACHADO tiene su cierre con evidencia.

        Con plan de cajas el cierre se cuenta POR CAJA vía el evento
        `caja_cerrada_con_evidencia` (entidad "paquete") que estampa
        cerrar_caja() — contar fotos del pedido dejaba que una foto duplicada
        de la caja 1 "cerrara" la caja 2 sin evidencia. Pedido sin plan de
        paquetes (o con plan aún sin empacar por caja) = 1 caja implícita:
        basta una foto de cierre ligada al pedido. El manifiesto usa este
        helper para excluir y avisar ("PED-x se queda: falta foto de caja
        cerrada").
        """
        from apps.core.models import EventoAuditoria, EvidenciaFoto  # lazy: evita ciclos
        cajas = list(
            self.paquetes.filter(estado__in=["EMPACADO", "DESPACHADO"])
            .values_list("pk", flat=True)
        )
        if cajas:
            cerradas = (
                EventoAuditoria.objects.filter(
                    entidad="paquete",
                    entidad_id__in=[str(pk) for pk in cajas],
                    accion="caja_cerrada_con_evidencia",
                )
                .values("entidad_id").distinct().count()
            )
            return cerradas >= len(cajas)
        return EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pk), tipo="caja_cerrada",
        ).exists()


class LineaPedido(models.Model):
    """Renglón del pedido: SKU, cantidad pedida y avance de pick."""

    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="lineas")
    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="lineas_pedido")
    cantidad = models.PositiveIntegerField()
    cantidad_pickeada = models.PositiveIntegerField(default=0)
    lote_asignado = models.ForeignKey(
        "catalogo.Lote", null=True, blank=True, on_delete=models.SET_NULL, related_name="lineas_pedido",
    )
    reservada = models.BooleanField(
        default=False, help_text="True si inventario.reservar apartó el stock de esta línea",
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "línea de pedido"
        verbose_name_plural = "líneas de pedido"

    def __str__(self):
        return f"{self.pedido.folio} · {self.sku} × {self.cantidad}"
