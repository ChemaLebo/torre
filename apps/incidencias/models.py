"""Incidencias — el módulo anti-Melonn: cada falla tiene folio, reloj y dueño.

Modelos según CONVENTIONS.md §incidencias:
- Incidencia: folio INC-AAAA-#### por año, tipos DAN|RET|RF|FAL|DIR|CAN|DES,
  relojes SLA calculados al abrir desde settings.TORRE.
- MensajeIncidencia: timeline visible al cliente; las notas internas son
  privadas pero quedan en el registro (auditables, exportables en disputa).
- Compensacion: COTIZADA→APROBADA→PAGADA (PAGADA exige referencia de pago).
- ReclamacionCarrier: PREPARADA→PRESENTADA→ACEPTADA/RECHAZADA→PAGADA.
"""
from django.db import models
from django.utils import timezone

from apps.core.services import registrar_evento


class MaquinaEstados(models.Model):
    """Base de máquina de estados: valida transición, estampa, guarda y audita.

    Cada subclase define ESTADOS, TRANSICIONES y ENTIDAD_AUDITORIA, y puede
    sobreescribir los hooks `_validar_transicion` / `_al_transicionar`.
    """

    class Meta:
        abstract = True

    ENTIDAD_AUDITORIA = ""
    TRANSICIONES = {}

    def _cliente_auditoria(self):
        """Cliente (tenant) al que se atribuye el evento de auditoría."""
        return None

    def _id_auditoria(self):
        return self.pk

    def _validar_transicion(self, anterior, nuevo):
        """Reglas extra de negocio antes de mutar estado. Lanza ValueError."""

    def _al_transicionar(self, anterior, nuevo, actor):
        """Estampa timestamps. Regresa lista de campos extra a persistir."""
        return []

    def transicionar(self, nuevo, actor=None, motivo=""):
        """Transición validada + guardado + evento de auditoría. Inválida → ValueError."""
        permitidos = self.TRANSICIONES.get(self.estado, set())
        if nuevo not in permitidos:
            raise ValueError(
                f"Transición inválida en {self._meta.verbose_name}: "
                f"{self.estado} → {nuevo}. Permitidas: {sorted(permitidos) or 'ninguna'}."
            )
        self._validar_transicion(self.estado, nuevo)
        anterior = self.estado
        self.estado = nuevo
        extras = self._al_transicionar(anterior, nuevo, actor) or []
        self.save(update_fields=["estado", *extras])
        registrar_evento(
            self.ENTIDAD_AUDITORIA,
            self._id_auditoria(),
            f"transicion_{nuevo.lower()}",
            actor=actor,
            cliente=self._cliente_auditoria(),
            delta={"de": anterior, "a": nuevo},
            motivo=motivo,
        )
        return self


class Incidencia(MaquinaEstados):
    """Falla operativa con folio, tipo, prioridad y relojes SLA."""

    # ── Tipos (catálogo único, BLUEPRINT §1.5) ──
    TIPO_DAN = "DAN"   # daño / rotura
    TIPO_RET = "RET"   # retraso
    TIPO_RF = "RF"     # retorno falso / no entregado
    TIPO_FAL = "FAL"   # pedido incompleto / faltante
    TIPO_DIR = "DIR"   # dirección
    TIPO_CAN = "CAN"   # cancelación tardía
    TIPO_DES = "DES"   # descuadre de inventario
    TIPOS = [
        (TIPO_DAN, "Daño / rotura"),
        (TIPO_RET, "Retraso"),
        (TIPO_RF, "Retorno falso / no entregado"),
        (TIPO_FAL, "Pedido incompleto"),
        (TIPO_DIR, "Dirección"),
        (TIPO_CAN, "Cancelación tardía"),
        (TIPO_DES, "Descuadre de inventario"),
    ]

    # ── Prioridades ──
    P1, P2, P3 = "P1", "P2", "P3"
    PRIORIDADES = [(P1, "P1 · crítica"), (P2, "P2 · normal"), (P3, "P3 · baja")]

    # ── Orígenes ──
    ORIGEN_AUTO = "auto"
    ORIGEN_MANUAL = "manual"
    ORIGEN_COMPRADOR = "comprador"
    ORIGEN_CLIENTE = "cliente"
    ORIGENES = [
        (ORIGEN_AUTO, "Automática (sistema)"),
        (ORIGEN_MANUAL, "Manual (Mesa)"),
        (ORIGEN_COMPRADOR, "Comprador final"),
        (ORIGEN_CLIENTE, "Cliente (portal)"),
    ]

    # ── Estados canónicos ──
    ABIERTA = "ABIERTA"
    EN_CURSO = "EN_CURSO"
    RESOLUCION_PROPUESTA = "RESOLUCION_PROPUESTA"
    RESUELTA = "RESUELTA"
    CERRADA = "CERRADA"
    ESTADOS = [
        (ABIERTA, "Abierta"),
        (EN_CURSO, "En curso"),
        (RESOLUCION_PROPUESTA, "Resolución propuesta"),
        (RESUELTA, "Resuelta"),
        (CERRADA, "Cerrada"),
    ]
    TRANSICIONES = {
        ABIERTA: {EN_CURSO, RESOLUCION_PROPUESTA, RESUELTA},
        EN_CURSO: {RESOLUCION_PROPUESTA, RESUELTA},
        RESOLUCION_PROPUESTA: {EN_CURSO, RESUELTA},
        RESUELTA: {EN_CURSO, CERRADA},  # EN_CURSO = reabierta (cliente rechaza)
        CERRADA: set(),
    }
    # "Abierta" para SLAs y para el flag pedido.incidencia_activa
    ESTADOS_ABIERTOS = (ABIERTA, EN_CURSO, RESOLUCION_PROPUESTA)

    ENTIDAD_AUDITORIA = "incidencia"

    cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT, related_name="incidencias")
    pedido = models.ForeignKey(
        "pedidos.Pedido", null=True, blank=True, on_delete=models.PROTECT, related_name="incidencias"
    )
    sku = models.ForeignKey(
        "catalogo.SKU", null=True, blank=True, on_delete=models.PROTECT, related_name="incidencias"
    )
    folio = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    tipo = models.CharField(max_length=3, choices=TIPOS)
    prioridad = models.CharField(max_length=2, choices=PRIORIDADES, default=P2)
    origen = models.CharField(max_length=10, choices=ORIGENES, default=ORIGEN_MANUAL)
    estado = models.CharField(max_length=24, choices=ESTADOS, default=ABIERTA, db_index=True)
    dueno = models.CharField(max_length=120, blank=True, help_text="Quién tiene la pelota")
    ts_apertura = models.DateTimeField(default=timezone.now, db_index=True)
    ts_primera_respuesta = models.DateTimeField(null=True, blank=True)
    ts_resolucion = models.DateTimeField(null=True, blank=True)
    ts_cierre = models.DateTimeField(null=True, blank=True)
    # Relojes SLA: calculados AL ABRIR con settings.TORRE (nunca números mágicos)
    sla_respuesta_limite = models.DateTimeField(null=True, blank=True)
    sla_resolucion_limite = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-ts_apertura"]
        verbose_name = "incidencia"
        verbose_name_plural = "incidencias"

    def __str__(self):
        return f"{self.folio} · {self.get_tipo_display()} · {self.estado}"

    # ── Folio INC-AAAA-#### autogenerado por año ──

    @classmethod
    def _generar_folio(cls):
        """Consecutivo por año (zero-pad 4). El orden lexicográfico del folio
        coincide con el numérico mientras el consecutivo no exceda 9999."""
        anio = timezone.localtime().year
        prefijo = f"INC-{anio}-"
        ultimo = (
            cls.objects.filter(folio__startswith=prefijo)
            .order_by("-folio")
            .values_list("folio", flat=True)
            .first()
        )
        consecutivo = int(ultimo.rsplit("-", 1)[1]) + 1 if ultimo else 1
        return f"{prefijo}{consecutivo:04d}"

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = self._generar_folio()
        super().save(*args, **kwargs)

    # ── Hooks de la máquina de estados ──

    def _cliente_auditoria(self):
        return self.cliente

    def _id_auditoria(self):
        return self.folio

    def _al_transicionar(self, anterior, nuevo, actor):
        extras = []
        if nuevo == self.RESUELTA and self.ts_resolucion is None:
            self.ts_resolucion = timezone.now()
            extras.append("ts_resolucion")
        if nuevo == self.EN_CURSO and anterior == self.RESUELTA:
            # Reabierta: el reloj de resolución vuelve a correr
            self.ts_resolucion = None
            extras.append("ts_resolucion")
        if nuevo == self.CERRADA and self.ts_cierre is None:
            self.ts_cierre = timezone.now()
            extras.append("ts_cierre")
        return extras

    # ── Propiedades de reloj para dashboards ──

    @property
    def minutos_para_sla_respuesta(self):
        """Minutos que restan del reloj de primera respuesta (negativo = vencido).

        None si ya hubo primera respuesta o no hay reloj definido.
        """
        if self.ts_primera_respuesta is not None or self.sla_respuesta_limite is None:
            return None
        return int((self.sla_respuesta_limite - timezone.now()).total_seconds() // 60)

    @property
    def vencida_respuesta(self):
        """True si el límite de primera respuesta pasó sin respuesta humana."""
        return (
            self.ts_primera_respuesta is None
            and self.sla_respuesta_limite is not None
            and timezone.now() > self.sla_respuesta_limite
        )

    @property
    def vencida_resolucion(self):
        """True si el límite de resolución pasó sin resolución."""
        return (
            self.ts_resolucion is None
            and self.sla_resolucion_limite is not None
            and timezone.now() > self.sla_resolucion_limite
        )

    @property
    def abierta(self):
        return self.estado in self.ESTADOS_ABIERTOS


class MensajeIncidencia(models.Model):
    """Timeline de la incidencia, visible al cliente en su portal.

    Regla de oro de transparencia: toda interacción consta en el timeline.
    Las notas internas (`interno=True`) NO se muestran en el portal, pero
    quedan en el registro: son auditables y exportables en disputa formal.
    """

    ROL_MESA = "mesa"
    ROL_CLIENTE = "cliente"
    ROL_COMPRADOR = "comprador"
    ROL_SISTEMA = "sistema"
    ROLES = [
        (ROL_MESA, "Mesa de Control"),
        (ROL_CLIENTE, "Cliente"),
        (ROL_COMPRADOR, "Comprador final"),
        (ROL_SISTEMA, "Sistema"),
    ]

    incidencia = models.ForeignKey(Incidencia, on_delete=models.CASCADE, related_name="mensajes")
    autor = models.CharField(max_length=120)
    rol_autor = models.CharField(max_length=10, choices=ROLES)
    texto = models.TextField()
    interno = models.BooleanField(
        default=False,
        help_text="Nota interna: no se muestra en el portal, pero queda en el registro.",
    )
    ts = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["ts"]
        verbose_name = "mensaje de incidencia"
        verbose_name_plural = "mensajes de incidencia"

    def __str__(self):
        privado = " (interno)" if self.interno else ""
        return f"{self.incidencia.folio} · {self.autor}{privado}"


class Compensacion(MaquinaEstados):
    """Compensación al cliente/comprador ligada a una incidencia.

    La resolución default del 3PL es reposición física (BLUEPRINT §1.5);
    reembolsos y cupones siguen el flujo pactado en onboarding.
    """

    TIPO_REPOSICION = "reposicion"
    TIPO_REEMBOLSO = "reembolso"
    TIPO_CUPON = "cupon"
    TIPOS = [
        (TIPO_REPOSICION, "Reposición física"),
        (TIPO_REEMBOLSO, "Reembolso"),
        (TIPO_CUPON, "Cupón"),
    ]

    COTIZADA = "COTIZADA"
    APROBADA = "APROBADA"
    PAGADA = "PAGADA"
    ESTADOS = [(COTIZADA, "Cotizada"), (APROBADA, "Aprobada"), (PAGADA, "Pagada")]
    TRANSICIONES = {
        COTIZADA: {APROBADA},
        APROBADA: {PAGADA},
        PAGADA: set(),
    }

    ENTIDAD_AUDITORIA = "compensacion"

    incidencia = models.ForeignKey(Incidencia, on_delete=models.PROTECT, related_name="compensaciones")
    tipo = models.CharField(max_length=12, choices=TIPOS)
    monto = models.DecimalField(max_digits=10, decimal_places=2, help_text="MXN")
    estado = models.CharField(max_length=10, choices=ESTADOS, default=COTIZADA, db_index=True)
    aprobo = models.CharField(max_length=120, blank=True)
    fecha_pago = models.DateField(null=True, blank=True)
    referencia_pago = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "compensación"
        verbose_name_plural = "compensaciones"

    def __str__(self):
        return f"{self.incidencia.folio} · {self.get_tipo_display()} ${self.monto} · {self.estado}"

    def _cliente_auditoria(self):
        return self.incidencia.cliente

    def _validar_transicion(self, anterior, nuevo):
        if nuevo == self.PAGADA and not self.referencia_pago:
            raise ValueError(
                "No se puede marcar PAGADA una compensación sin referencia_pago. "
                "Captura la referencia del pago primero."
            )

    def _al_transicionar(self, anterior, nuevo, actor):
        extras = []
        if nuevo == self.APROBADA and not self.aprobo and actor is not None:
            self.aprobo = getattr(actor, "username", None) or str(actor)
            extras.append("aprobo")
        if nuevo == self.PAGADA and self.fecha_pago is None:
            self.fecha_pago = timezone.localdate()
            extras.append("fecha_pago")
        return extras


class ReclamacionCarrier(MaquinaEstados):
    """Expediente de reclamación ante el carrier, ligado a una incidencia.

    La recuperación se presupuesta ≤15–20% y solo en extravío/robo: es
    reposición del fondo de garantía, nunca la fuente del compromiso.
    """

    PREPARADA = "PREPARADA"
    PRESENTADA = "PRESENTADA"
    ACEPTADA = "ACEPTADA"
    RECHAZADA = "RECHAZADA"
    PAGADA = "PAGADA"
    ESTADOS = [
        (PREPARADA, "Preparada"),
        (PRESENTADA, "Presentada"),
        (ACEPTADA, "Aceptada"),
        (RECHAZADA, "Rechazada"),
        (PAGADA, "Pagada"),
    ]
    TRANSICIONES = {
        PREPARADA: {PRESENTADA},
        PRESENTADA: {ACEPTADA, RECHAZADA},
        ACEPTADA: {PAGADA},
        RECHAZADA: set(),
        PAGADA: set(),
    }

    ENTIDAD_AUDITORIA = "reclamacion_carrier"

    incidencia = models.ForeignKey(Incidencia, on_delete=models.PROTECT, related_name="reclamaciones")
    carrier = models.CharField(max_length=40)
    monto_reclamado = models.DecimalField(max_digits=10, decimal_places=2, help_text="MXN")
    estado = models.CharField(max_length=10, choices=ESTADOS, default=PREPARADA, db_index=True)
    monto_recuperado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fecha_presentacion = models.DateField(null=True, blank=True)
    fecha_resolucion = models.DateField(null=True, blank=True)
    fecha_pago = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "reclamación a carrier"
        verbose_name_plural = "reclamaciones a carrier"

    def __str__(self):
        return f"{self.incidencia.folio} · {self.carrier} ${self.monto_reclamado} · {self.estado}"

    def _cliente_auditoria(self):
        return self.incidencia.cliente

    def _al_transicionar(self, anterior, nuevo, actor):
        extras = []
        hoy = timezone.localdate()
        if nuevo == self.PRESENTADA and self.fecha_presentacion is None:
            self.fecha_presentacion = hoy
            extras.append("fecha_presentacion")
        if nuevo in (self.ACEPTADA, self.RECHAZADA) and self.fecha_resolucion is None:
            self.fecha_resolucion = hoy
            extras.append("fecha_resolucion")
        if nuevo == self.PAGADA and self.fecha_pago is None:
            self.fecha_pago = hoy
            extras.append("fecha_pago")
        return extras
