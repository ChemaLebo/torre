"""Cortes de facturación quincenales (Chema 2026-10-01): del 1 al 15 y del
16 al último día del mes (fin de mes real, para no pelearse con febrero).
Cada corte produce dos estados de cuenta por cliente (fulfillment y guías).
Un corte se identifica con la clave "AAAA-MM-N" (N = 1 o 2) en la URL de
Mesa → Finanzas."""
import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


@dataclass(frozen=True, order=True)
class Corte:
    """Una quincena de un mes: `numero` 1 = del 1 al 15, 2 = del 16 al fin."""

    anio: int
    mes: int
    numero: int

    @property
    def inicio(self):
        """Primer día del corte."""
        return date(self.anio, self.mes, 1 if self.numero == 1 else 16)

    @property
    def fin(self):
        """Último día del corte, inclusive."""
        if self.numero == 1:
            return date(self.anio, self.mes, 15)
        return date(self.anio, self.mes, calendar.monthrange(self.anio, self.mes)[1])

    @property
    def dias(self):
        """Días del corte (15, o 13 a 16 en el segundo)."""
        return (self.fin - self.inicio).days + 1

    @property
    def clave(self):
        """Identificador para URLs: "2026-10-1"."""
        return f"{self.anio}-{self.mes:02d}-{self.numero}"

    @property
    def etiqueta(self):
        """Nombre legible: "1ª quincena de octubre 2026"."""
        return f"{'1ª' if self.numero == 1 else '2ª'} quincena de {MESES[self.mes - 1]} {self.anio}"

    @property
    def etiqueta_corta(self):
        """Rango corto: "1–15 oct 2026", "16–31 oct 2026"."""
        return f"{self.inicio.day}–{self.fin.day} {MESES[self.mes - 1][:3]} {self.anio}"

    def limites(self):
        """(inicio, fin) aware en la zona local: 00:00 del primer día y 00:00
        del día siguiente al último (fin exclusivo), como reportes.base.limites."""
        inicio = timezone.make_aware(datetime.combine(self.inicio, time.min))
        fin = timezone.make_aware(datetime.combine(self.fin + timedelta(days=1), time.min))
        return inicio, fin

    def anterior(self):
        """El corte previo, cruzando de mes y de año."""
        if self.numero == 2:
            return Corte(self.anio, self.mes, 1)
        if self.mes == 1:
            return Corte(self.anio - 1, 12, 2)
        return Corte(self.anio, self.mes - 1, 2)

    def siguiente(self):
        """El corte que sigue, cruzando de mes y de año."""
        if self.numero == 1:
            return Corte(self.anio, self.mes, 2)
        if self.mes == 12:
            return Corte(self.anio + 1, 1, 1)
        return Corte(self.anio, self.mes + 1, 1)

    def cerrado(self, hoy=None):
        """True cuando el corte ya terminó (por fecha): lo que llegue después
        de su último día cae en el corte en que llega, nunca aquí."""
        return self.fin < (hoy or timezone.localdate())


def corte_de(fecha):
    """Corte al que pertenece una fecha (date, o datetime aware → fecha local)."""
    if isinstance(fecha, datetime):
        fecha = timezone.localtime(fecha).date() if timezone.is_aware(fecha) else fecha.date()
    return Corte(fecha.year, fecha.month, 1 if fecha.day <= 15 else 2)


def corte_actual():
    """El corte de hoy (fecha local)."""
    return corte_de(timezone.localdate())


def corte_desde_clave(texto):
    """Corte a partir de "AAAA-MM-N"; None si la clave no se entiende."""
    try:
        anio, mes, numero = (int(p) for p in (texto or "").strip().split("-"))
        if numero not in (1, 2):
            return None
        date(anio, mes, 1)
    except (ValueError, TypeError):
        return None
    return Corte(anio, mes, numero)
