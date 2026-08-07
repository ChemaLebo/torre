"""Envíos: guías y reglas de selección de carrier.

La `Guia` es el espejo interno del tracking del carrier (operado vía envia.com).
El pedido lo mueve `services.poll_tracking` a partir de estados normalizados.
Regla dura del BLUEPRINT §1.4: el RECOLECTADO del pedido nace del manifiesto
físico (escaneo de salida), nunca del escaneo del chofer.
"""
from django.db import models
from django.utils import timezone

from apps.core.services import registrar_evento


class Guia(models.Model):
    """Guía de envío ligada a un pedido, con máquina de estados de tracking."""

    GUIA_CREADA = "GUIA_CREADA"
    RECOLECTADO = "RECOLECTADO"
    EN_TRANSITO = "EN_TRANSITO"
    EN_RUTA = "EN_RUTA"
    ENTREGADO = "ENTREGADO"
    INTENTO_FALLIDO = "INTENTO_FALLIDO"
    RETENIDO = "RETENIDO"
    RETORNO = "RETORNO"
    EXCEPCION = "EXCEPCION"

    ESTADOS = [
        (GUIA_CREADA, "Guía creada"),
        (RECOLECTADO, "Recolectado por el carrier"),
        (EN_TRANSITO, "En tránsito"),
        (EN_RUTA, "En ruta de entrega"),
        (ENTREGADO, "Entregado"),
        (INTENTO_FALLIDO, "Intento de entrega fallido"),
        (RETENIDO, "Retenido"),
        (RETORNO, "Retorno al remitente"),
        (EXCEPCION, "Excepción"),
    ]

    # El tracking real es ruidoso: los carriers se saltan estados y rebotan
    # entre EN_TRANSITO/EN_RUTA. Solo ENTREGADO y RETORNO son terminales.
    TRANSICIONES = {
        GUIA_CREADA: {RECOLECTADO, EN_TRANSITO, EN_RUTA, ENTREGADO, INTENTO_FALLIDO, RETENIDO, RETORNO, EXCEPCION},
        RECOLECTADO: {EN_TRANSITO, EN_RUTA, ENTREGADO, INTENTO_FALLIDO, RETENIDO, RETORNO, EXCEPCION},
        EN_TRANSITO: {EN_RUTA, ENTREGADO, INTENTO_FALLIDO, RETENIDO, RETORNO, EXCEPCION},
        EN_RUTA: {EN_TRANSITO, ENTREGADO, INTENTO_FALLIDO, RETENIDO, RETORNO, EXCEPCION},
        INTENTO_FALLIDO: {EN_TRANSITO, EN_RUTA, ENTREGADO, RETENIDO, RETORNO, EXCEPCION},
        RETENIDO: {EN_TRANSITO, EN_RUTA, ENTREGADO, INTENTO_FALLIDO, RETORNO, EXCEPCION},
        EXCEPCION: {EN_TRANSITO, EN_RUTA, ENTREGADO, INTENTO_FALLIDO, RETENIDO, RETORNO},
        ENTREGADO: set(),
        RETORNO: set(),
    }

    ESTADOS_TERMINALES = frozenset({ENTREGADO, RETORNO})
    # Solo un RETORNO desactiva la guía (permite reexpedir con guía nueva).
    # Una EXCEPCION sigue viva: la Mesa la resuelve antes de regenerar nada.
    ESTADOS_INACTIVOS = frozenset({RETORNO})

    pedido = models.ForeignKey("pedidos.Pedido", on_delete=models.PROTECT, related_name="guias")
    paquete = models.ForeignKey(
        "envios.Paquete", null=True, blank=True, on_delete=models.SET_NULL, related_name="guias",
        help_text="Paquete al que ampara esta guía; null en guías previas a la división de envíos",
    )
    carrier = models.CharField(max_length=40)
    servicio = models.CharField(max_length=60, blank=True)
    numero = models.CharField(max_length=64, blank=True, db_index=True)
    costo_cotizado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    costo_preferencial = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, help_text="Tasa preferencial envia.com"
    )
    etiqueta_url = models.CharField(max_length=500, blank=True)
    estado = models.CharField(max_length=20, choices=ESTADOS, default=GUIA_CREADA, db_index=True)
    ultimo_evento = models.CharField(max_length=300, blank=True)
    ts_ultimo_movimiento = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "guía"
        verbose_name_plural = "guías"

    def __str__(self):
        return f"{self.carrier} {self.numero} · {self.estado}"

    @property
    def es_activa(self):
        """La guía vigente del pedido. Solo un retorno permite generar otra."""
        return self.estado not in self.ESTADOS_INACTIVOS

    def transicionar(self, nuevo, actor=None, motivo=""):
        """Valida la transición, estampa el movimiento y registra auditoría."""
        permitidos = self.TRANSICIONES.get(self.estado, set())
        if nuevo not in permitidos:
            raise ValueError(f"Transición inválida de guía {self.numero}: {self.estado} → {nuevo}")
        anterior = self.estado
        self.estado = nuevo
        self.ts_ultimo_movimiento = timezone.now()
        self.save(update_fields=["estado", "ts_ultimo_movimiento"])
        registrar_evento(
            "guia", self.pk, f"guia_{nuevo.lower()}",
            actor=actor, cliente=self.pedido.cliente,
            delta={"de": anterior, "a": nuevo, "numero": self.numero},
            motivo=motivo,
        )
        return self


class ReglaEnvio(models.Model):
    """Regla de selección de carrier. `cliente=None` → global. Menor prioridad gana.

    Condiciones soportadas: `{"es_local": true}`, `{"cp_prefijo": "28"}`,
    `{}` (aplica siempre). Ejemplo canónico: Colima → paquetexpress vía envia.
    """

    cliente = models.ForeignKey(
        "core.Cliente", null=True, blank=True, on_delete=models.PROTECT, related_name="reglas_envio"
    )
    prioridad = models.PositiveIntegerField(default=100, help_text="Menor número = se evalúa primero")
    condicion = models.JSONField(
        default=dict, blank=True,
        help_text='{"es_local": true} / {"cp_prefijo": "28"} / {} (aplica siempre)',
    )
    carrier = models.CharField(max_length=40)
    servicio = models.CharField(max_length=60)

    class Meta:
        ordering = ["prioridad", "id"]
        verbose_name = "regla de envío"
        verbose_name_plural = "reglas de envío"

    def __str__(self):
        alcance = self.cliente.nombre if self.cliente_id else "global"
        return f"[{self.prioridad}] {alcance} {self.condicion or '{}'} → {self.carrier}/{self.servicio}"

    def aplica_a(self, pedido):
        """Evalúa la condición JSON contra el pedido. Condición vacía aplica siempre."""
        cond = self.condicion or {}
        if "es_local" in cond and bool(pedido.es_local) != bool(cond["es_local"]):
            return False
        if "cp_prefijo" in cond and not str(pedido.cp or "").startswith(str(cond["cp_prefijo"])):
            return False
        return True


class Paquete(models.Model):
    """Un bulto físico del pedido tras la planificación de envío.

    La división existe por dos reglas de negocio (BLUEPRINT/CONVENTIONS-ENVIOS):
    ningún bulto pasa de settings.TORRE["MAX_PESO_ENVIO_KG"], y se divide cuando
    el total de N guías chicas sale más barato que una grande (medido cotizando
    particiones reales, nunca con reglas fijas). El porqué queda visible para
    pickers/packers (ahorro_plan_mxn) — regla explícita de Diego.
    """

    PLANEADO = "PLANEADO"
    EN_EMPAQUE = "EN_EMPAQUE"
    EMPACADO = "EMPACADO"
    DESPACHADO = "DESPACHADO"
    ESTADOS = [
        (PLANEADO, "Planeado"),
        (EN_EMPAQUE, "En empaque"),
        (EMPACADO, "Empacado"),
        (DESPACHADO, "Despachado"),
    ]
    TRANSICIONES = {
        PLANEADO: {EN_EMPAQUE, EMPACADO},
        EN_EMPAQUE: {EMPACADO},
        EMPACADO: {DESPACHADO},
        DESPACHADO: set(),
    }

    pedido = models.ForeignKey("pedidos.Pedido", on_delete=models.CASCADE, related_name="paquetes")
    numero = models.PositiveIntegerField(help_text="1..N dentro del pedido")
    peso_kg = models.DecimalField(max_digits=6, decimal_places=2)
    largo_cm = models.PositiveIntegerField(default=40)
    ancho_cm = models.PositiveIntegerField(default=30)
    alto_cm = models.PositiveIntegerField(default=26)
    carrier = models.CharField(max_length=40)
    servicio = models.CharField(max_length=60, blank=True)
    precio_cotizado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fuera_de_meta = models.BooleanField(
        default=False, help_text="La mejor tarifa excede TORRE['TARIFA_OBJETIVO_MXN']"
    )
    ahorro_plan_mxn = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Ahorro del plan completo vs enviar todo en un solo bulto (mismo valor en cada paquete del plan)",
    )
    estado = models.CharField(max_length=12, choices=ESTADOS, default=PLANEADO, db_index=True)
    peso_real_gr = models.PositiveIntegerField(null=True, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pedido_id", "numero"]
        unique_together = [("pedido", "numero")]
        verbose_name = "paquete"

    def __str__(self):
        return f"{self.pedido.folio} · paquete {self.numero} ({self.peso_kg} kg, {self.carrier})"

    @property
    def guia_activa(self):
        return next((g for g in self.guias.all() if g.es_activa), None)

    def transicionar(self, nuevo, actor=None, motivo=""):
        permitidos = self.TRANSICIONES.get(self.estado, set())
        if nuevo not in permitidos:
            raise ValueError(f"Transición inválida de paquete: {self.estado} → {nuevo}")
        anterior = self.estado
        self.estado = nuevo
        self.save(update_fields=["estado"])
        registrar_evento(
            "paquete", self.pk, f"paquete_{nuevo.lower()}",
            actor=actor, cliente=self.pedido.cliente,
            delta={"de": anterior, "a": nuevo, "pedido": self.pedido.folio, "numero": self.numero},
            motivo=motivo,
        )
        return self


class PaqueteLinea(models.Model):
    """Qué unidades de qué línea del pedido van dentro de cada paquete."""

    paquete = models.ForeignKey(Paquete, on_delete=models.CASCADE, related_name="lineas")
    linea_pedido = models.ForeignKey(
        "pedidos.LineaPedido", on_delete=models.CASCADE, related_name="en_paquetes"
    )
    cantidad = models.PositiveIntegerField(
        help_text="Unidades de venta, o subunidades si fraccion_de > 1"
    )
    fraccion_de = models.PositiveIntegerField(
        default=1,
        help_text=">1 = la unidad de venta se reempacó: cantidad cuenta subunidades "
                  "(ej. cantidad=1, fraccion_de=2 → media caja)",
    )

    @property
    def texto_para_piso(self):
        """Cómo lo lee el packer: '1/2 de Caja 24 (reempacada)' o '2 × Six'."""
        sku = self.linea_pedido.sku
        nombre = sku.descripcion or sku.codigo
        if self.fraccion_de > 1:
            return f"{self.cantidad}/{self.fraccion_de} de {nombre} (REEMPACADA en caja del cliente)"
        return f"{self.cantidad} × {nombre}"

    class Meta:
        verbose_name = "línea de paquete"
        verbose_name_plural = "líneas de paquete"

    def __str__(self):
        return f"{self.paquete} ← {self.cantidad}× línea {self.linea_pedido_id}"


class TrabajoImpresion(models.Model):
    """Cola de impresión remota: Torre (VPS) encola, el agente de bodega imprime.

    Con `TORRE_MODO_IMPRESION=relay`, `apps.piso.etiquetas.imprimir_etiqueta`
    guarda aquí el PDF de la etiqueta en vez de correr `lp` (la térmica ya no
    vive junto al servidor). El agente (`deploy/agente_impresion.py`) hace
    polling autenticado a la API, imprime en la bodega y confirma. Tras
    `settings.TORRE["IMPRESION_MAX_INTENTOS"]` fallas el trabajo pasa a ERROR
    y queda visible para Mesa vía el evento de auditoría.
    """

    PENDIENTE = "PENDIENTE"
    IMPRESO = "IMPRESO"
    ERROR = "ERROR"
    ESTADOS = [
        (PENDIENTE, "Pendiente"),
        (IMPRESO, "Impreso"),
        (ERROR, "Error"),
    ]
    TRANSICIONES = {
        PENDIENTE: {IMPRESO, ERROR},
        IMPRESO: set(),
        ERROR: set(),
    }

    guia = models.ForeignKey(
        "envios.Guia", on_delete=models.PROTECT, related_name="trabajos_impresion"
    )
    pdf = models.FileField(upload_to="etiquetas/%Y%m/")
    estado = models.CharField(max_length=10, choices=ESTADOS, default=PENDIENTE, db_index=True)
    intentos = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    creado = models.DateTimeField(auto_now_add=True, db_index=True)
    ts_impreso = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["creado"]
        verbose_name = "trabajo de impresión"
        verbose_name_plural = "trabajos de impresión"

    def __str__(self):
        return f"Impresión #{self.pk} · {self.guia.numero or self.guia.pedido.folio} · {self.estado}"

    def transicionar(self, nuevo, actor=None, motivo=""):
        """Valida la transición, estampa ts_impreso si aplica y registra auditoría."""
        permitidos = self.TRANSICIONES.get(self.estado, set())
        if nuevo not in permitidos:
            raise ValueError(
                f"Transición inválida de trabajo de impresión: {self.estado} → {nuevo}"
            )
        anterior = self.estado
        self.estado = nuevo
        campos = ["estado"]
        if nuevo == self.IMPRESO:
            self.ts_impreso = timezone.now()
            campos.append("ts_impreso")
        self.save(update_fields=campos)
        registrar_evento(
            "trabajo_impresion", self.pk, f"impresion_{nuevo.lower()}",
            actor=actor, cliente=self.guia.pedido.cliente,
            delta={
                "de": anterior, "a": nuevo, "folio": self.guia.pedido.folio,
                "guia": self.guia.numero, "intentos": self.intentos, "error": self.error,
            },
            motivo=motivo,
        )
        return self


class CotizacionCache(models.Model):
    """Caché de cotizaciones por lane (CP, peso): evita repegarle a la API.

    Los resultados negativos también se cachean (ok=False = ese carrier no
    cotiza ese lane/peso, p. ej. puntopost >10 kg o sin cobertura).
    """

    cp_destino = models.CharField(max_length=5, db_index=True)
    peso_kg = models.DecimalField(max_digits=6, decimal_places=1, help_text="Redondeado a 0.5")
    carrier = models.CharField(max_length=40)
    servicio = models.CharField(max_length=60, blank=True)
    precio = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    estimado_entrega = models.CharField(max_length=60, blank=True)
    ok = models.BooleanField(default=True)
    ts = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("cp_destino", "peso_kg", "carrier")]
        verbose_name = "cotización en caché"
        verbose_name_plural = "cotizaciones en caché"

    def __str__(self):
        precio = f"${self.precio}" if self.ok else "no cotiza"
        return f"{self.cp_destino} {self.peso_kg}kg {self.carrier}: {precio}"
