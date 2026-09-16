"""Reportes para clientes: la lógica compartida entre Mesa (todos los
clientes) y el portal (solo el suyo). Pedido de Colima, plan 2026-09-15
(DEUDA-TECNICA.md, "Reportería para clientes").

Cada reporte es un módulo de esta app con el mismo contrato:
- `CLAVE`, `TITULO`, `DESCRIPCION`: identidad y ayuda en pantalla.
- `CON_FECHAS`: si acepta rango ?desde=&hasta= (fechas inclusivas; default
  del 1° del mes a hoy).
- `FILTROS`: filtros extra [{nombre, etiqueta, tipo (number|select|checkbox),
  opciones, default}].
- `COLUMNAS`: [(encabezado, tipo)] con tipo en texto|entero|decimal|dinero|
  pct|horas|fecha|fechahora — decide el formato en pantalla; el CSV va crudo.
- `COLUMNAS_MESA`: columnas extra que solo ve Mesa (p. ej. costos reales).
- `generar(cliente, inicio, fin, filtros, es_mesa)` → {"filas": [[...]],
  "resumen": [(etiqueta, valor)], "grupos": [{"titulo", "columnas", "filas"}]}.
  Siempre recibe UN cliente; Mesa sin cliente elegido concatena los de todos
  con la columna "Cliente" al frente (views.render_reporte).
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

TIPOS_NUMERICOS = {"entero", "decimal", "dinero", "pct", "horas"}


def rango_desde_get(get):
    """(desde, hasta, válido) desde ?desde=AAAA-MM-DD&hasta=AAAA-MM-DD, fechas
    inclusivas; sin parámetros, del primer día del mes a hoy. Un valor que no
    se entiende regresa el default con válido=False."""
    hoy = timezone.localdate()
    desde, hasta, valido = hoy.replace(day=1), hoy, True
    for clave in ("desde", "hasta"):
        valor = (get.get(clave) or "").strip()
        if not valor:
            continue
        try:
            fecha = date.fromisoformat(valor)
        except ValueError:
            valido = False
            continue
        if clave == "desde":
            desde = fecha
        else:
            hasta = fecha
    if desde > hasta:
        desde, hasta = hasta, desde
    return desde, hasta, valido


def limites(desde, hasta):
    """[inicio, fin) aware que cubren los días locales `desde`..`hasta`."""
    inicio = timezone.make_aware(datetime.combine(desde, time.min))
    fin = timezone.make_aware(datetime.combine(hasta + timedelta(days=1), time.min))
    return inicio, fin


def filtros_desde_get(definiciones, get):
    """Valores de los filtros extra según su tipo; los que no vienen toman su
    default (checkbox: False si el form ya se mandó, default si es la primera
    carga)."""
    enviado = bool(get)
    valores = {}
    for f in definiciones:
        crudo = get.get(f["nombre"])
        tipo = f.get("tipo", "text")
        if tipo == "checkbox":
            valores[f["nombre"]] = (crudo == "on") if enviado else bool(f.get("default"))
        elif tipo == "number":
            try:
                valores[f["nombre"]] = int(crudo) if crudo not in (None, "") else f.get("default")
            except ValueError:
                valores[f["nombre"]] = f.get("default")
        else:
            valores[f["nombre"]] = crudo if crudo not in (None, "") else f.get("default", "")
    return valores


def horas(desde, hasta):
    """Horas entre dos datetimes con dos decimales; None si falta alguno."""
    if desde is None or hasta is None:
        return None
    return round((hasta - desde).total_seconds() / 3600, 2)


def promedio(valores):
    """Promedio a dos decimales de los valores no nulos; None si no hay."""
    limpios = [v for v in valores if v is not None]
    if not limpios:
        return None
    return round(sum(limpios) / len(limpios), 2)


def dinero(valor):
    """Decimal a dos decimales (acepta int/float/str/None)."""
    if valor is None:
        return None
    return Decimal(str(valor)).quantize(Decimal("0.01"))
