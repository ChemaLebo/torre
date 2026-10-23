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
from decimal import Decimal, InvalidOperation

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


def _decimal_o_none(crudo):
    try:
        return Decimal(str(crudo).strip()) if crudo not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def filtros_desde_get(definiciones, get, es_mesa=True):
    """Valores de los filtros extra según su tipo; los que no vienen toman su
    default (checkbox: False si el form ya se mandó, default si es la primera
    carga). Tipos: number | select | checkbox | text | multiselect (lista de
    valores elegidos; vacía = todos) | rango (par (mínimo, máximo) leído de
    <nombre>_min y <nombre>_max como Decimal, None lo que no se capturó).
    Un filtro con `solo_mesa` se ignora fuera de Mesa (queda en su default).
    `opciones` puede ser una lista o un callable sin argumentos (se evalúa al
    pintar la pantalla, no aquí)."""
    enviado = bool(get)
    obtener_lista = getattr(get, "getlist", None)
    valores = {}
    for f in definiciones:
        nombre, tipo = f["nombre"], f.get("tipo", "text")
        if tipo == "multiselect":
            if f.get("solo_mesa") and not es_mesa:
                crudos = []
            elif obtener_lista is not None:
                crudos = obtener_lista(nombre)
            else:
                crudos = get.get(nombre) or []
                crudos = [crudos] if isinstance(crudos, str) else list(crudos)
            valores[nombre] = [str(v).strip() for v in crudos if str(v).strip()] or list(f.get("default") or [])
            continue
        if tipo == "rango":
            if f.get("solo_mesa") and not es_mesa:
                valores[nombre] = (None, None)
            else:
                valores[nombre] = (_decimal_o_none(get.get(f"{nombre}_min")), _decimal_o_none(get.get(f"{nombre}_max")))
            continue
        fuera_de_mesa = bool(f.get("solo_mesa")) and not es_mesa
        crudo = None if fuera_de_mesa else get.get(nombre)
        if tipo == "checkbox":
            valores[nombre] = bool(f.get("default")) if (fuera_de_mesa or not enviado) else (crudo == "on")
        elif tipo == "number":
            try:
                valores[nombre] = int(crudo) if crudo not in (None, "") else f.get("default")
            except ValueError:
                valores[nombre] = f.get("default")
        else:
            valores[nombre] = str(crudo).strip() if crudo not in (None, "") else f.get("default", "")
    return valores


def opciones_de(filtro):
    """Opciones de un filtro select/multiselect: la lista tal cual o el callable evaluado."""
    opciones = filtro.get("opciones") or []
    return list(opciones()) if callable(opciones) else list(opciones)


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
