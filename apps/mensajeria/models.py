"""Mensajería saliente de Torre: plantillas A/B/E y registro idempotente de envíos.

La anti-falla-crítica de Melonn vive aquí: cada mensaje que sale queda
registrado (NotificacionEnviada), es idempotente por evento, y el cliente
SIEMPRE se entera de lo que pasa con su marca.
"""
from django.conf import settings
from django.db import models

# Variables disponibles en los cuerpos de plantilla.
VARIABLES_PLANTILLA = ("{nombre}", "{folio}", "{guia}", "{link_rastreo}", "{fecha}", "{marca}", "{corte}")

# Cuerpos default (voz de marca premium, español mexicano cálido).
# A — confirmación honesta: menciona el corte y la fecha REAL de salida.
# B — en camino: SOLO se envía al RECOLECTADO, con guía y link de rastreo.
# E — retraso: primera persona de marca, disculpa de frente, JAMÁS culpa a la paquetería.
CUERPOS_DEFAULT = {
    "A": (
        "Hola {nombre}, te saluda el equipo de entregas de {marca}. "
        "Ya recibimos tu pedido {folio} y lo estamos preparando con mucho cuidado. "
        "Nuestro corte del día es a las {corte}, así que tu pedido sale de bodega el {fecha}. "
        "En cuanto vaya en camino te avisamos por aquí con tu número de guía. "
        "Gracias por tu compra: la vamos a cuidar como si fuera nuestra."
    ),
    "B": (
        "¡Buenas noticias, {nombre}! Tu pedido {folio} de {marca} ya va en camino: "
        "acaba de salir de nuestra bodega. "
        "Tu número de guía es {guia} y puedes seguir tu paquete paso a paso aquí: {link_rastreo} "
        "Si necesitas cualquier cosa, respóndenos por este mismo chat; "
        "del otro lado hay una persona real lista para ayudarte."
    ),
    "E": (
        "Hola {nombre}, te escribimos de {marca}. Queremos ser honestos contigo: "
        "tu pedido {folio} está tardando más de lo que nos gustaría, y te ofrecemos una disculpa. "
        "Ya le estamos dando seguimiento puntual para que llegue lo antes posible; "
        "la nueva fecha estimada de entrega es el {fecha}. "
        "Si tienes cualquier duda, respóndenos por aquí y con gusto te atendemos. "
        "Gracias por tu paciencia y por confiar en {marca}."
    ),
}

NOMBRES_DEFAULT = {
    "A": "Confirmación de pedido",
    "B": "Pedido en camino",
    "E": "Aviso de retraso",
}


class _ContextoSeguro(dict):
    """format_map sin explotar: variable faltante → cadena vacía.

    Un comprador jamás debe recibir un "{guia}" sin resolver en su mensaje.
    """

    def __missing__(self, clave):
        return ""


class PlantillaMensaje(models.Model):
    """Plantilla de mensaje al comprador final. cliente=None es la default global."""

    CLAVE_A = "A"
    CLAVE_B = "B"
    CLAVE_E = "E"
    CLAVES = [
        (CLAVE_A, "A — Confirmación de pedido"),
        (CLAVE_B, "B — En camino (solo al RECOLECTADO)"),
        (CLAVE_E, "E — Retraso"),
    ]

    clave = models.CharField(max_length=1, choices=CLAVES)
    cliente = models.ForeignKey(
        "core.Cliente", null=True, blank=True, on_delete=models.CASCADE,
        related_name="plantillas_mensaje",
        help_text="Vacío = plantilla default para todos los clientes",
    )
    nombre = models.CharField(max_length=120)
    cuerpo = models.TextField(
        help_text="Variables: {nombre}, {folio}, {guia}, {link_rastreo}, {fecha}, {marca}, {corte}",
    )
    aprobada_por_cliente = models.BooleanField(default=False)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "plantilla de mensaje"
        verbose_name_plural = "plantillas de mensaje"
        ordering = ["clave", "cliente__nombre"]
        constraints = [
            models.UniqueConstraint(fields=["clave", "cliente"], name="plantilla_unica_por_cliente"),
            models.UniqueConstraint(
                fields=["clave"], condition=models.Q(cliente__isnull=True),
                name="plantilla_default_unica",
            ),
        ]

    def render(self, contexto):
        """Sustituye las {variables} del cuerpo con el contexto dado."""
        return self.cuerpo.format_map(_ContextoSeguro(contexto or {}))

    @classmethod
    def default_en_memoria(cls, clave):
        """Instancia NO guardada con el cuerpo canónico — respaldo si no hay fila en BD."""
        return cls(clave=clave, nombre=NOMBRES_DEFAULT[clave], cuerpo=CUERPOS_DEFAULT[clave])

    def __str__(self):
        dueno = self.cliente.nombre if self.cliente_id else "default"
        return f"{self.clave} · {self.nombre} ({dueno})"


class NotificacionEnviada(models.Model):
    """Registro append-de-facto de cada mensaje que salió de Torre.

    Idempotencia de salida: la clave única f"{evento}:{canal}:{destinatario}"
    garantiza que reenviar el mismo evento es no-op (se regresa la existente).
    """

    CANAL_WHATSAPP = "whatsapp"
    CANAL_EMAIL = "email"
    CANAL_CONSOLA = "consola"
    CANALES = [
        (CANAL_WHATSAPP, "WhatsApp"),
        (CANAL_EMAIL, "Email"),
        (CANAL_CONSOLA, "Consola"),
    ]

    clave_idempotencia = models.CharField(max_length=200, unique=True)
    canal = models.CharField(max_length=10, choices=CANALES)
    destinatario = models.CharField(max_length=120)
    cuerpo = models.TextField()
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    # Trazabilidad (adicional al contrato mínimo)
    cliente = models.ForeignKey(
        "core.Cliente", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="notificaciones_enviadas",
    )
    plantilla_clave = models.CharField(max_length=20, blank=True)  # A | B | E | INC_NUEVA | DIGEST
    referencia = models.CharField(max_length=60, blank=True)       # PED-x / INC-x / DIGEST-fecha

    class Meta:
        verbose_name = "notificación enviada"
        verbose_name_plural = "notificaciones enviadas"
        ordering = ["-ts"]

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} {self.canal} → {self.destinatario} ({self.plantilla_clave or 'mensaje'})"


class SuscripcionPush(models.Model):
    """Suscripción Web Push de un usuario interno (PWA de Torre en su celular).

    El endpoint es único: si el navegador re-suscribe, se refresca la fila
    (update_or_create). Una suscripción muerta (404/410 del push service) se
    poda marcando activo=False — nunca se borra, para dejar rastro.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="suscripciones_push",
    )
    # Los push services reales usan URLs de ~150-300 chars; 500 acota el
    # payload (push.suscribir valida el mismo tope antes de guardar).
    endpoint = models.CharField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=200, blank=True)
    activo = models.BooleanField(default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "suscripción push"
        verbose_name_plural = "suscripciones push"
        ordering = ["-creado"]

    def __str__(self):
        estado = "activa" if self.activo else "muerta"
        return f"{self.usuario.username} · {estado} · {self.endpoint[:60]}"
