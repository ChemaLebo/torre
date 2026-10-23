"""Gráficas de los reportes en SVG generado por el servidor (Chema
2026-10-22): sin librería, se imprimen, funcionan en el portal y se prueban con
el test client. Cada grupo de un reporte puede traer `grafica`:

- {"tipo": "barras", "etiqueta": i, "valor": j, "serie": "Guías"}: barras
  horizontales, una por fila (categorías: zona, ciudad, CP, rango…), valor al
  final de la barra. Una sola serie: el título del grupo la nombra.
- {"tipo": "columnas", "etiqueta": i, "series": [(j, "Pedidos"), (k, "Guías")]}:
  columnas por fila (días, meses, cubetas) y, con varias series, UN panel por
  serie con su propio eje (small multiples: nunca dos ejes en una gráfica).

Reglas de la skill de dataviz: marcas delgadas (<= 24 px) con punta redondeada
solo en el extremo de datos, 2 px de aire entre marcas, rejilla a un paso de la
superficie, texto siempre con los tokens de texto (nunca del color de la serie),
valores etiquetados en las puntas (la tabla de al lado es la vista completa),
`<title>` por marca como tooltip nativo y paleta categórica validada en claro y
oscuro contra las superficies de Torre (`static/css/wop.css`, `.grafica`).
"""
import html
import math
from decimal import Decimal

ANCHO = 720
ALTO_FILA = 22  # grosor de barra 18 + 4 de aire (gap de superficie)
GROSOR = 18
RADIO = 4
MAX_BARRAS = 30
MARGEN_IZQ_BARRAS = 150
PANEL_ALTO = 150
MARGEN = {"arriba": 14, "abajo": 26, "izq": 44, "der": 12}


def _num(valor):
    """float del valor de una celda (Decimal, int, float, str numérica); None si no es número."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return float(Decimal(str(valor)))
    except Exception:  # noqa: BLE001 — texto en una columna numérica: no se grafica
        return None


def compacto(valor):
    """1,284 · 12.9K · 4.2M · 12.50 (dos decimales solo si no es entero y es chico)."""
    if valor is None:
        return ""
    v = float(valor)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 10_000:
        return f"{v / 1_000:.1f}K"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}"


def _escala(maximo):
    """(tope, [ticks]) con números redondos: 0 / 1K / 2K…; tope >= máximo."""
    if maximo <= 0:
        return 1, [0, 1]
    bruto = maximo / 3
    potencia = 10 ** math.floor(math.log10(bruto))
    for paso in (1, 2, 2.5, 5, 10):
        if bruto <= paso * potencia:
            paso_final = paso * potencia
            break
    tope = math.ceil(maximo / paso_final) * paso_final
    ticks = [i * paso_final for i in range(int(round(tope / paso_final)) + 1)]
    return tope, ticks


def _texto(x, y, texto, *, clase="", ancla="start"):
    return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{ancla}" class="{clase}">{html.escape(str(texto))}</text>'


def _barra_horizontal(x, y, w, h, r=RADIO):
    """Rect con punta redondeada solo a la derecha (el extremo de datos); cuadrado en la base."""
    if w <= 0:
        return ""
    r = min(r, w, h / 2)
    return (f'<path d="M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - 2 * r:.1f} '
            f'a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z"/>')


def _columna(x, y, w, h, r=RADIO):
    """Rect con punta redondeada solo arriba (extremo de datos); cuadrado en la base."""
    if h <= 0:
        return ""
    r = min(r, h, w / 2)
    return (f'<path d="M{x:.1f},{y + h:.1f} v-{h - r:.1f} a{r},{r} 0 0 1 {r},-{r} h{w - 2 * r:.1f} '
            f'a{r},{r} 0 0 1 {r},{r} v{h - r:.1f} z"/>')


def _svg(alto, cuerpo, titulo):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ANCHO} {alto}" width="100%" height="{alto}" '
            f'role="img" aria-label="{html.escape(titulo)}" class="grafica-svg">{cuerpo}</svg>')


def barras(filas, etiqueta, valor, titulo, serie=""):
    """Barras horizontales: una por fila, en el orden que trae el grupo (el
    módulo ya las ordena); más de MAX_BARRAS se recortan (la tabla las tiene)."""
    datos = [(str(f[etiqueta]), _num(f[valor])) for f in filas[:MAX_BARRAS]]
    datos = [(e, v) for e, v in datos if v is not None]
    if not datos:
        return ""
    maximo = max(v for _e, v in datos)
    tope, _ticks = _escala(maximo)
    x0, ancho_util = MARGEN_IZQ_BARRAS, ANCHO - MARGEN_IZQ_BARRAS - 70
    alto = MARGEN["arriba"] + len(datos) * ALTO_FILA + 8
    partes = [f'<line x1="{x0}" y1="{MARGEN["arriba"]}" x2="{x0}" y2="{alto - 8}" class="eje"/>']
    for i, (e, v) in enumerate(datos):
        y = MARGEN["arriba"] + i * ALTO_FILA + (ALTO_FILA - GROSOR) / 2
        w = ancho_util * v / tope if tope else 0
        nombre = e if len(e) <= 22 else e[:21] + "…"
        partes.append(
            f'<g class="marca"><title>{html.escape(e)}: {html.escape(compacto(v))}{(" " + html.escape(serie)) if serie else ""}</title>'
            f'<rect x="0" y="{y - 2:.1f}" width="{ANCHO}" height="{GROSOR + 4}" class="zona-hover"/>'
            f'<g class="serie-1">{_barra_horizontal(x0, y, w, GROSOR)}</g>'
            + _texto(x0 - 8, y + GROSOR - 5, nombre, clase="etiqueta", ancla="end")
            + _texto(x0 + w + 6, y + GROSOR - 5, compacto(v), clase="valor")
            + "</g>"
        )
    return _svg(alto, "".join(partes), titulo)


def _panel(filas, etiqueta, indice, nombre, slot, y0, titulo_visible):
    """Un panel de columnas (una serie) con su propio eje y rejilla."""
    datos = [(str(f[etiqueta]), _num(f[indice]) or 0.0) for f in filas]
    maximo = max((v for _e, v in datos), default=0)
    tope, ticks = _escala(maximo)
    x0, x1 = MARGEN["izq"], ANCHO - MARGEN["der"]
    y_base = y0 + PANEL_ALTO - MARGEN["abajo"]
    alto_util = PANEL_ALTO - MARGEN["abajo"] - MARGEN["arriba"] - (14 if titulo_visible else 0)
    y_arriba = y_base - alto_util
    n = len(datos)
    banda = (x1 - x0) / n if n else 0
    grosor = min(24, max(2, banda - 2))
    partes = []
    if titulo_visible:
        partes.append(f'<g class="leyenda"><rect x="{x0}" y="{y0 + 2}" width="10" height="10" rx="2" class="serie-{slot}"/>'
                      + _texto(x0 + 15, y0 + 11, nombre, clase="etiqueta") + "</g>")
    for t in ticks:
        y = y_base - (alto_util * t / tope if tope else 0)
        partes.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="rejilla"/>')
        partes.append(_texto(x0 - 6, y + 4, compacto(t), clase="tick", ancla="end"))
    cada = max(1, math.ceil(n / 8))
    for i, (e, v) in enumerate(datos):
        x = x0 + i * banda + (banda - grosor) / 2
        h = alto_util * v / tope if tope else 0
        partes.append(
            f'<g class="marca"><title>{html.escape(e)}: {html.escape(compacto(v))} {html.escape(nombre)}</title>'
            f'<rect x="{x0 + i * banda:.1f}" y="{y_arriba}" width="{banda:.1f}" height="{alto_util + MARGEN["abajo"]}" class="zona-hover"/>'
            f'<g class="serie-{slot}">{_columna(x, y_base - h, grosor, h)}</g></g>'
        )
        if i % cada == 0 or i == n - 1:
            partes.append(_texto(x0 + i * banda + banda / 2, y_base + 16, e, clase="tick", ancla="middle"))
    partes.append(f'<line x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}" class="eje"/>')
    if datos:
        i_max = max(range(n), key=lambda k: datos[k][1])
        v = datos[i_max][1]
        if v > 0:
            partes.append(_texto(x0 + i_max * banda + banda / 2, y_base - alto_util * v / tope - 4, compacto(v), clase="valor", ancla="middle"))
    return "".join(partes)


def columnas(filas, etiqueta, series, titulo):
    """Columnas por fila; varias series = un panel por serie, cada uno con su eje."""
    filas = [f for f in filas if f[etiqueta] not in (None, "")]
    if not filas or not series:
        return ""
    alto = PANEL_ALTO * len(series)
    cuerpo = "".join(
        _panel(filas, etiqueta, indice, nombre, slot, k * PANEL_ALTO, titulo_visible=len(series) > 1)
        for k, (slot, (indice, nombre)) in enumerate(zip(range(1, 9), series))
    )
    return _svg(alto, cuerpo, titulo)


def svg(grupo):
    """SVG del grupo según su `grafica`; "" si no pide gráfica o no hay datos."""
    spec = grupo.get("grafica")
    if not spec or not grupo.get("filas"):
        return ""
    titulo = grupo.get("titulo", "")
    try:
        if spec["tipo"] == "barras":
            return barras(grupo["filas"], spec["etiqueta"], spec["valor"], titulo, spec.get("serie", ""))
        if spec["tipo"] == "columnas":
            return columnas(grupo["filas"], spec["etiqueta"], list(spec["series"])[:8], titulo)
    except (KeyError, IndexError, TypeError):
        return ""
    return ""
