"""Pedidos — la máquina de estados canónica de Torre (BLUEPRINT §2.3).

Un pedido nace de Shopify (o manual), reserva stock al ingerir y avanza por
transiciones explícitas. Cada transición estampa su timestamp y queda en el
event log (core.EventoAuditoria): nada cambia de estado en silencio y está
prohibido el estado genérico "en proceso".
"""
from django.conf import settings
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
    # Cancelar con mercancía en proceso (EN_PICKING/EMPACADO/GUIA_GENERADA) va
    # directo a CANCELADO: lo pickeado entra como reingreso (OrdenEntrada tipo
    # reingreso) y el piso lo ubica por recepción. CANCELACION_PENDIENTE se
    # conserva para pedidos viejos que quedaron ahí antes de ese cambio.
    TRANSICIONES = {
        PENDIENTE: {EN_PICKING, CANCELADO},
        EN_PICKING: {EMPACADO, CANCELACION_PENDIENTE, CANCELADO},
        EMPACADO: {GUIA_GENERADA, CANCELACION_PENDIENTE, CANCELADO},
        # GUIA_GENERADA → EN_TRANSITO cubre el caso raro de guía indexada por el
        # carrier antes de nuestro escaneo de salida; RECOLECTADO sigue siendo el
        # paso autoritativo del flujo normal (escaneo + manifiesto).
        # GUIA_GENERADA → EMPACADO: guías canceladas antes de salir por cambio
        # de dirección (services.regresar_a_empaque); se compra guía nueva.
        GUIA_GENERADA: {RECOLECTADO, PARCIALMENTE_DESPACHADO, EN_TRANSITO, CANCELACION_PENDIENTE, CANCELADO, EMPACADO},
        # → CANCELADO desde la calle solo lo hace el cierre de una cancelación
        # tardía (Mesa decide el reingreso o resuelve la incidencia CAN).
        RECOLECTADO: {EN_TRANSITO, ENTREGADO, PARCIALMENTE_DESPACHADO, CANCELADO},
        EN_TRANSITO: {ENTREGADO, ENTREGA_PRESUNTA, RETORNADO, CANCELADO},
        ENTREGA_PRESUNTA: {ENTREGADO, RETORNADO, CANCELADO},
        # PARCIALMENTE_DESPACHADO = salieron algunas cajas y otras siguen en
        # bodega (manifiesto por caja); al salir la última → RECOLECTADO. El
        # tracking no lo mueve mientras queden cajas adentro. También es el
        # estado de espera del fulfillment parcial (2026-09-22): salió lo que
        # había y una línea sin inventario espera stock; al reservarse el
        # pedido vuelve a PENDIENTE (segunda ola, mismo folio).
        PARCIALMENTE_DESPACHADO: {RECOLECTADO, EN_TRANSITO, ENTREGADO, RETORNADO, CANCELADO, PENDIENTE},
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
        PARCIALMENTE_DESPACHADO: "ts_recolectado",  # la primera caja que sale ya "va en camino"
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
    # "Nombre" de la orden en Shopify ("#4074"): lo que usa servicio al cliente
    # (Chema 2026-09-25). El link al admin sigue con el ID; en pantalla va este.
    shopify_order_name = models.CharField(max_length=40, blank=True, default="")
    folio = models.CharField(max_length=12, unique=True, blank=True, editable=False)
    origen = models.CharField(max_length=15, choices=ORIGENES, default="webhook")
    # Canal de venta (de dónde vino la compra), distinto de `origen` (cómo llegó
    # a Torre): se deriva del source_name / tags de la orden de Shopify con
    # TORRE["CANAL_POR_SOURCE"] y ["CANAL_POR_TAG"]; manual para los de Mesa.
    CANAL_WEB = "web"
    CANAL_TIKTOK = "tiktok"
    CANAL_B2B = "b2b"
    CANAL_POS = "pos"
    CANAL_SOCIAL = "social"
    CANAL_SUSCRIPCION = "suscripcion"  # recurrentes de Recharge (2026-09-22)
    CANAL_MANUAL = "manual"
    CANAL_OTRO = "otro"
    CANALES = [
        (CANAL_WEB, "Tienda en línea"), (CANAL_TIKTOK, "TikTok Shop"), (CANAL_B2B, "B2B"),
        (CANAL_POS, "Punto de venta"), (CANAL_SOCIAL, "Redes sociales"),
        (CANAL_SUSCRIPCION, "Suscripción"), (CANAL_MANUAL, "Manual"), (CANAL_OTRO, "Otro"),
    ]
    canal = models.CharField(max_length=12, choices=CANALES, default=CANAL_WEB, db_index=True)
    canal_fuente = models.CharField(max_length=60, blank=True, help_text="source_name crudo de Shopify")
    comprador_nombre = models.CharField(max_length=120, blank=True)
    comprador_tel = models.CharField(max_length=20, blank=True)
    comprador_email = models.EmailField(blank=True)
    direccion = models.JSONField(default=dict, blank=True)
    # Dirección que Shopify tiene AHORA cuando la del pedido ya está congelada
    # (guía comprada o algo en la calle; Chema 2026-09-23): la de la guía no se
    # pisa; Mesa ve las dos y regresar_a_empaque aplica esta. null = sin diferencia.
    direccion_pendiente = models.JSONField(null=True, blank=True)
    # Fulfillment del pedido ENTERO en Shopify (gid) cuando no se fulfillea por
    # caja (sin plan de cajas, entrega en bodega, líneas no separables): de él
    # cuelgan los eventos de avance (integraciones.services.registrar_evento_fulfillment).
    shopify_fulfillment_id = models.CharField(max_length=80, blank=True, default="")
    cp = models.CharField("código postal", max_length=10, blank=True)
    es_local = models.BooleanField(default=False, help_text="CP de Colima (28xxx): entrega local propia")
    # Carta que le tocó en el reparto por porcentajes del cliente (vacío si el
    # cliente no reparte o una ReglaEnvio decidió antes). Se saca una sola vez
    # por pedido (envios.reparto.sacar_carta) y aquí queda para que todas las
    # llamadas a elegir_carrier (plan, cada paquete, corral de Salida) coincidan.
    reparto_carrier = models.CharField(max_length=40, blank=True)
    # Paquetería que Mesa forzó para ESTE pedido desde la incidencia "Sin
    # paquetería que cotice" (Chema 2026-09-24): un carrier, o "envia" = el
    # más barato de la lista de envia.com. Manda sobre reglas, reparto y la
    # integración del cliente (envios.services.carriers_del_pedido).
    carrier_forzado = models.CharField(max_length=40, blank=True)
    parcial_de_orden = models.BooleanField(
        default=False,
        help_text="La orden de Shopify se dividió entre locations: este pedido ampara solo NUESTRO ticket",
    )
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
    # Dueño del pedido en piso (sep-2026): se asigna al iniciar picking y el
    # pedido DESAPARECE para los demás operadores hasta la última foto de
    # cierre. Cambio de manos = transferencia con ACEPTACIÓN del receptor.
    asignado_a = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="pedidos_asignados",
        help_text="Operador de piso dueño: de iniciar picking a la última foto de cierre",
    )
    transferencia_a = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="pedidos_por_aceptar",
        help_text="Transferencia pendiente: el destinatario debe ACEPTAR para volverse dueño",
    )
    peso_esperado_gr = models.PositiveIntegerField(default=0)
    peso_real_gr = models.PositiveIntegerField(null=True, blank=True)
    # Cierre del inventario de un pedido que ya salió y se canceló o retornó:
    # vacío = Mesa no ha decidido; "reingresado" = hay OrdenEntrada tipo reingreso;
    # "no_recuperado" = la mercancía no volverá (queda en el expediente).
    REINGRESO_PENDIENTE = ""
    REINGRESADO = "reingresado"
    NO_RECUPERADO = "no_recuperado"
    REINGRESO_ESTADOS = [
        (REINGRESO_PENDIENTE, "Sin decidir"),
        (REINGRESADO, "Reingresado"),
        (NO_RECUPERADO, "Inventario no recuperado"),
    ]
    reingreso_estado = models.CharField(
        max_length=15, choices=REINGRESO_ESTADOS, default=REINGRESO_PENDIENTE, blank=True,
    )
    # Se canceló con el paquete ya en la calle: el estado sigue al paquete y la
    # cancelación vive en la incidencia CAN hasta que Mesa decide el reingreso
    # o resuelve la CAN; entonces el pedido pasa a CANCELADO.
    cancelacion_tardia = models.BooleanField(default=False)
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
    def lineas_por_surtir(self):
        """Líneas que la ola en curso sí surte: con reserva y con unidades que
        aún no salen (LineaPedido.pendiente > 0). Fuera quedan las faltantes
        (sin inventario, tag "Sin inventario") y lo que ya se fue en un
        manifiesto anterior. Lee `lineas.all()` para respetar el prefetch de
        `lineas__sku` de las vistas."""
        return [l for l in self.lineas.all() if l.pendiente > 0]

    @property
    def lineas_faltantes(self):
        """Líneas sin inventario que esta ola ignora (fulfillment parcial)."""
        return [l for l in self.lineas.all() if l.faltante]

    @property
    def tiene_faltantes(self):
        """True si alguna línea espera inventario (se surte en una segunda ola)."""
        return any(l.faltante for l in self.lineas.all())

    @property
    def tiene_despachadas(self):
        """True si algo del pedido ya salió en un manifiesto (primera ola en la calle)."""
        return any(l.cantidad_despachada for l in self.lineas.all())

    @property
    def pendiente_de_completar(self):
        """True si el pedido todavía no está entero en la calle: espera
        inventario (faltantes) o está en su segunda ola (ya salió una parte y
        otra sigue en bodega). El tracking de lo que ya salió no debe cerrar
        el pedido mientras esto sea True."""
        lineas = list(self.lineas.all())
        if any(l.faltante for l in lineas):
            return True
        return any(l.cantidad_despachada for l in lineas) and any(l.pendiente > 0 for l in lineas)

    @property
    def esperando_inventario(self):
        """True mientras el pedido, ya con una parte en la calle, espera stock
        de sus faltantes (PARCIALMENTE_DESPACHADO sin cajas por salir)."""
        if self.estado != self.PARCIALMENTE_DESPACHADO:
            return False
        if any(c.estado == "EMPACADO" for c in self.paquetes.all()):
            return False  # cajas de esta ola aún en bodega: es un manifiesto por caja
        return self.tiene_faltantes

    @property
    def lineas_completas(self):
        """True si toda línea POR SURTIR tiene su cantidad completa pickeada;
        las faltantes no cuentan (van con el tag "Sin inventario")."""
        return all(l.cantidad_pickeada >= l.cantidad for l in self.lineas_por_surtir)

    @property
    def cajas_cerradas_completas(self):
        """True si cada caja EMPACADO/DESPACHADO tiene su cierre con evidencia.

        Con plan de cajas el cierre se cuenta POR CAJA con `Paquete.ts_cierre`
        (lo estampa cerrar_caja()) — contar fotos del pedido dejaba que una
        foto duplicada de la caja 1 "cerrara" la caja 2 sin evidencia. Pedido
        sin plan de paquetes (o con plan aún sin empacar por caja) = 1 caja
        implícita: basta una foto de cierre ligada al pedido. El manifiesto
        usa este helper para excluir y avisar ("PED-x se queda: falta foto de
        caja cerrada").
        """
        from apps.core.models import EvidenciaFoto  # lazy: evita ciclos
        cajas = list(self.paquetes.filter(estado__in=["EMPACADO", "DESPACHADO"]))
        if cajas:
            return all(c.ts_cierre is not None for c in cajas)
        return EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pk), tipo="caja_cerrada",
        ).exists()

    @property
    def orden_shopify_legible(self):
        """Cómo se nombra la orden de Shopify en pantalla: su nombre ("#4074")
        o, si aún no se conoce, "#<id>"; "" para pedidos sin orden."""
        if self.shopify_order_name:
            return self.shopify_order_name
        return f"#{self.shopify_order_id}" if self.shopify_order_id else ""

    @property
    def direccion_congelada(self):
        """True cuando la dirección ya no se refresca sola desde Shopify: hay
        guía activa, algo despachado, o el pedido salió del carril de la mesa
        (guía generada en adelante, cancelado, retornado). Un cambio llega
        entonces a `direccion_pendiente` y Mesa decide."""
        if self.estado not in (self.PENDIENTE, self.EN_PICKING, self.EMPACADO):
            return True
        if self.tiene_despachadas:
            return True
        return any(g.es_activa for g in self.guias.all())

    @property
    def empaque_completo(self):
        """True cuando en la mesa de empaque ya no falta nada: cada caja
        EMPACADO/DESPACHADO con guía activa y su foto de cierre (ts_cierre);
        sin plan de cajas, una guía activa del pedido y su foto de cierre.

        Salida solo lista pedidos con esto en True: nada llega al corral sin
        guía y foto. Lo que falte se termina en el wizard de empaque, desde
        "Completar empaquetado" en Mi turno (Chema, 2026-09-21).
        """
        cajas = [c for c in self.paquetes.all() if c.estado in ("EMPACADO", "DESPACHADO")]
        if cajas:
            return all(c.ts_cierre is not None and c.guia_activa is not None for c in cajas)
        if not any(g.es_activa for g in self.guias.all()):
            return False
        return self.cajas_cerradas_completas


class LineaPedido(models.Model):
    """Renglón del pedido: SKU, cantidad pedida y avance de pick."""

    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="lineas")
    sku = models.ForeignKey("catalogo.SKU", on_delete=models.PROTECT, related_name="lineas_pedido")
    cantidad = models.PositiveIntegerField()
    cantidad_pickeada = models.PositiveIntegerField(default=0)
    # Precio de venta unitario REAL de la tienda (line_items[].price de Shopify,
    # con descuentos), para el reporte de ventas. Null en pedidos manuales y en
    # líneas agregadas por edición; las hijas de kit no llevan precio (lo lleva
    # el kit). Nunca se usa el precio del catálogo: no es venta real.
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    lote_asignado = models.ForeignKey(
        "catalogo.Lote", null=True, blank=True, on_delete=models.SET_NULL, related_name="lineas_pedido",
    )
    reservada = models.BooleanField(
        default=False, help_text="True si inventario.reservar apartó el stock de esta línea",
    )
    # Fulfillment parcial (Chema 2026-09-22): lo que ya salió de bodega en un
    # manifiesto. La segunda ola surte solo cantidad - cantidad_despachada, y
    # empacar / marcar_recolectado no confirman ni despachan dos veces del
    # kardex lo que se fue con la primera.
    cantidad_despachada = models.PositiveIntegerField(
        default=0, help_text="Unidades que ya salieron de bodega en un manifiesto anterior",
    )
    parte_de_kit = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="componentes",
        help_text="Línea kit a la que pertenece este componente (té dentro de la TeaBox)",
    )
    kit_caja = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Nº de caja del kit (1..cantidad) a la que pertenece esta hija — "
                  "el stepper 'TeaBox 1 de N' declara caja por caja",
    )
    nota_kit = models.CharField(
        max_length=300, blank=True, default="",
        help_text="Lo que el comprador eligió (texto de Appstle), para el packer",
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "línea de pedido"
        verbose_name_plural = "líneas de pedido"

    def __str__(self):
        return f"{self.pedido.folio} · {self.sku} × {self.cantidad}"

    @property
    def faltante(self):
        """True si la línea se queda sin surtir por falta de existencias: la
        ingesta no encontró stock (reservada=False) y el reintento aún no lo
        consigue. Picking, plan de cajas y empaque la ignoran y el piso la ve
        con el tag "Sin inventario" (Chema 2026-09-22). Un kit (nace
        reservada=True, sin stock propio) es faltante cuando alguna de sus
        hijas lo es: la caja del kit no viaja incompleta."""
        if self.sku.es_kit:
            return any(h.faltante for h in self.componentes.all())
        return not self.reservada

    @property
    def pendiente(self):
        """Unidades por surtir en la ola en curso: lo pedido menos lo que ya
        salió en un manifiesto (cantidad_despachada); 0 si es faltante."""
        if self.faltante:
            return 0
        return max(self.cantidad - self.cantidad_despachada, 0)
