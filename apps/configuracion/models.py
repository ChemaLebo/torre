"""Modelos de configuración de sistema: un modelo por ajuste, un renglón por valor."""
from django.core.exceptions import ValidationError
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


class TandaEtiquetas(models.Model):
    """Cuántas etiquetas de caja salen por tanda al imprimir una tarima en
    recepción (Chema 2026-10-13: el rollo se acaba a media tarima; el piso
    recarga y pulsa "Continuar impresión"). Un solo renglón, solo admin; sin
    renglón aplica `services.TANDA_ETIQUETAS_DEFAULT`."""

    cajas = models.PositiveIntegerField(default=50, help_text="Etiquetas de caja por tanda (la de la tarima no cuenta)")
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "tanda de etiquetas"
        verbose_name_plural = "tanda de etiquetas"

    def __str__(self):
        return f"{self.cajas} etiquetas por tanda"


class EtiquetasManejo(models.Model):
    """Cuántas etiquetas de manejo salen con la etiqueta interna 51×51 de
    cada caja que lo pide (Chema 2026-10-20): FRÁGIL cuando algún producto de
    la caja trae la bandera `fragil`, orientación ("flechas hacia arriba")
    cuando alguno trae `este_lado_arriba`. Un solo renglón, solo admin; sin
    renglón aplica `services.ETIQUETAS_MANEJO_DEFAULT`; 0 apaga ese tipo."""

    fragil = models.PositiveIntegerField(default=4, help_text="Etiquetas FRÁGIL por caja marcada (0 = no imprimir)")
    orientacion = models.PositiveIntegerField(default=3, help_text="Etiquetas de orientación por caja marcada (0 = no imprimir)")
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "etiquetas de manejo"
        verbose_name_plural = "etiquetas de manejo"

    def __str__(self):
        return f"{self.fragil} FRÁGIL · {self.orientacion} de orientación por caja"


class Origen(models.Model):
    """De dónde salen los envíos (Chema 2026-10-22). Hoy solo WOP (Local 380 E,
    CP 01780: el ORIGEN_DEFAULT de los adapters); pronto orígenes del cliente
    ("fulfilled by merchant": su inventario, Torre genera y cobra la guía). Las
    zonas de cobro (ZonaCP) cuelgan del origen: el mismo rango de CP destino
    puede ser local desde uno y foráneo desde otro. Por ahora alimenta zonas y
    reportes; el ruteo y el remitente que viaja al carrier siguen en settings.
    Un solo origen es `principal` (el que aplica mientras un pedido no diga
    otra cosa)."""

    nombre = models.CharField(max_length=80, unique=True)
    empresa = models.CharField(max_length=80, blank=True)
    calle = models.CharField(max_length=120, blank=True)
    numero = models.CharField(max_length=20, blank=True)
    colonia = models.CharField(max_length=80, blank=True)
    ciudad = models.CharField(max_length=80, blank=True)
    estado = models.CharField(max_length=5, blank=True, help_text="code_2_digits de envia (CX, JA, PU…)")
    cp = models.CharField(max_length=5, help_text="Código postal de 5 dígitos")
    telefono = models.CharField(max_length=20, blank=True)
    correo = models.EmailField(blank=True)
    principal = models.BooleanField(default=False, help_text="El origen de WOP: aplica cuando un pedido no dice otra cosa (solo uno)")
    activo = models.BooleanField(default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-principal", "nombre"]
        verbose_name = "origen de envíos"
        verbose_name_plural = "orígenes de envíos"
        constraints = [
            models.UniqueConstraint(fields=["principal"], condition=models.Q(principal=True), name="origen_principal_unico"),
        ]

    def __str__(self):
        return f"{self.nombre} · CP {self.cp}"

    def clean(self):
        if not (self.cp or "").isdigit() or len(self.cp) != 5:
            raise ValidationError({"cp": "El código postal lleva exactamente 5 dígitos (01780)."})


class ZonaCP(models.Model):
    """Rango inclusivo de CP DESTINO → zona de cobro, por origen (Chema
    2026-10-22; antes `config/zonas_cp.csv`, que queda de respaldo mientras
    esta tabla no tenga renglones). Un CP suelto = desde y hasta iguales; un
    hueco = dos rangos; los rangos activos de un mismo origen no se traslapan
    (clean). Lo que no cae en ningún rango es nacional (foránea). Las claves
    local | metro | nacional son las del tarifario de cada cliente
    (`envio_bloque`); la etiqueta visible es local / metropolitana / foránea.
    `ciudad` es solo una etiqueta para reportes (Querétaro, Guadalajara…)."""

    LOCAL = "local"
    METRO = "metro"
    NACIONAL = "nacional"
    ZONAS = [(LOCAL, "Local"), (METRO, "Metropolitana"), (NACIONAL, "Foránea")]

    origen = models.ForeignKey(Origen, on_delete=models.PROTECT, related_name="zonas")
    cp_desde = models.CharField(max_length=5, help_text="Primer CP del rango (5 dígitos)")
    cp_hasta = models.CharField(max_length=5, help_text="Último CP del rango, inclusive (un CP suelto: igual al primero)")
    zona = models.CharField(max_length=10, choices=ZONAS)
    ciudad = models.CharField(max_length=60, blank=True, help_text="Etiqueta para reportes: Querétaro, Guadalajara, Puebla…")
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["origen", "cp_desde", "cp_hasta"]
        verbose_name = "zona por código postal"
        verbose_name_plural = "zonas por código postal"

    def __str__(self):
        return f"{self.cp_desde}-{self.cp_hasta} → {self.get_zona_display()}" + (f" ({self.ciudad})" if self.ciudad else "")

    def clean(self):
        errores = {}
        for campo in ("cp_desde", "cp_hasta"):
            valor = getattr(self, campo) or ""
            if not valor.isdigit() or len(valor) != 5:
                errores[campo] = "Captura 5 dígitos (01251)."
        if errores:
            raise ValidationError(errores)
        if int(self.cp_desde) > int(self.cp_hasta):
            raise ValidationError({"cp_hasta": "El último CP del rango no puede ser menor que el primero."})
        if not self.activo or self.origen_id is None:
            return
        desde, hasta = int(self.cp_desde), int(self.cp_hasta)
        otros = ZonaCP.objects.filter(origen_id=self.origen_id, activo=True).exclude(pk=self.pk)
        for otro in otros:
            if int(otro.cp_desde) <= hasta and desde <= int(otro.cp_hasta):
                raise ValidationError(
                    f"Se traslapa con {otro.cp_desde}-{otro.cp_hasta} ({otro.get_zona_display()}) del mismo origen: "
                    "parte el rango o edita el otro."
                )
