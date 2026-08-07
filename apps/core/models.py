from django.conf import settings
from django.db import models


class Cliente(models.Model):
    """Tenant: cada marca que operamos (ancla: Cervecería Colima)."""

    nombre = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    razon_social = models.CharField(max_length=200, blank=True)
    rfc = models.CharField(max_length=13, blank=True)
    contacto_nombre = models.CharField(max_length=120, blank=True)
    contacto_whatsapp = models.CharField(max_length=20, blank=True)
    # Config por tenant
    buffer_stock = models.PositiveIntegerField(default=0, help_text="Buffer restado del disponible publicado")
    carrier_preferente = models.CharField(max_length=40, default="paquetexpress")
    naked_packing_local = models.BooleanField(default=True)
    guia_de_voz = models.TextField(blank=True, help_text="Reglas de tono para mensajes al comprador final")
    umbral_visto_bueno_mxn = models.DecimalField(max_digits=8, decimal_places=2, default=2000)
    branding = models.JSONField(
        default=dict, blank=True,
        help_text="Identidad para páginas públicas de rastreo: color_primario, color_fondo, logo_url, nombre_publico, whatsapp_soporte, dominio_tienda",
    )
    tarifario = models.JSONField(
        default=dict, blank=True,
        help_text="Override del tarifario (settings.TORRE['TARIFARIO_DEFAULT']): "
                  "almacenaje_mes, alistamiento_pedido, empaque_pedido, bloque_kg, "
                  "envio_bloque {local, metro, nacional}. Vacío = tarifario default.",
    )
    activo = models.BooleanField(default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class PerfilUsuario(models.Model):
    """Liga un usuario a su rol. Los usuarios de portal quedan amarrados a UN cliente."""

    ROL_PORTAL = "portal"
    ROL_PISO = "piso"
    ROL_MESA = "mesa"
    ROLES = [(ROL_PORTAL, "Cliente (portal)"), (ROL_PISO, "Operador de piso"), (ROL_MESA, "Mesa de Control")]

    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil")
    rol = models.CharField(max_length=10, choices=ROLES)
    cliente = models.ForeignKey(
        Cliente, null=True, blank=True, on_delete=models.CASCADE, related_name="usuarios",
        help_text="Obligatorio para rol portal; vacío para piso/mesa",
    )
    pin = models.CharField(max_length=6, blank=True, help_text="PIN de firma para ajustes (piso/mesa)")

    def __str__(self):
        return f"{self.usuario.username} · {self.get_rol_display()}"


class EventoAuditoria(models.Model):
    """Event log append-only: la base de la transparencia anti-Melonn.

    El rol de BD en producción solo tiene INSERT/SELECT. Nadie edita historia.
    """

    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_tipo = models.CharField(max_length=10, default="sistema")  # usuario | sistema | webhook
    actor_id = models.CharField(max_length=120, blank=True)
    cliente = models.ForeignKey(Cliente, null=True, blank=True, on_delete=models.SET_NULL)
    entidad = models.CharField(max_length=60, db_index=True)
    entidad_id = models.CharField(max_length=60, db_index=True)
    accion = models.CharField(max_length=60)
    delta = models.JSONField(default=dict, blank=True)
    motivo = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-ts"]
        verbose_name = "evento de auditoría"
        verbose_name_plural = "eventos de auditoría"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("EventoAuditoria es append-only: no se edita historia.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("EventoAuditoria es append-only: no se borra historia.")

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} {self.entidad}#{self.entidad_id} {self.accion}"


def _ruta_evidencia(instancia, nombre):
    return f"evidencia/{instancia.entidad}/{instancia.entidad_id}/{nombre}"


class EvidenciaFoto(models.Model):
    """Foto de evidencia ligada a cualquier entidad (pedido, ASN, incidencia, conteo).

    La captura vive en la app que ejecuta la transición; el archivo es storage,
    el vínculo es este registro. Retención mínima 12 meses; si `congelada`,
    pertenece a un expediente de reclamación y no se purga.
    """

    entidad = models.CharField(max_length=30, db_index=True)   # pedido | asn | incidencia | conteo | entrega_local
    entidad_id = models.CharField(max_length=60, db_index=True)
    tipo = models.CharField(max_length=30, blank=True)          # contenido | caja_cerrada | llegada | dano | pod
    archivo = models.FileField(upload_to=_ruta_evidencia)
    hash_sha256 = models.CharField(max_length=64, blank=True)
    tomada_por = models.CharField(max_length=120, blank=True)
    ts = models.DateTimeField(auto_now_add=True)
    congelada = models.BooleanField(default=False)

    class Meta:
        ordering = ["-ts"]

    def save(self, *args, **kwargs):
        if self.archivo and not self.hash_sha256:
            import hashlib
            h = hashlib.sha256()
            for chunk in self.archivo.chunks():
                h.update(chunk)
            self.hash_sha256 = h.hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entidad}#{self.entidad_id} {self.tipo}"
