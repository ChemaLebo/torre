"""render_manuales — pre-renderiza los SOPs (.md) a partials HTML de Torre.

Lee los manuales de la carpeta fuente (settings.TORRE["MANUALES_DIR"], default
BASE_DIR.parent / "manuales") y deja en templates/manuales/:
  - un partial <slug>.html por manual (solo el cuerpo, listo para {% include %}),
  - _indice.json con metadatos: slug, código, título, emoji, resumen, palabras,
    minutos de lectura, grupo, propiedades (fila estilo Notion) y TOC de h2.

El conversor de markdown es propio y pequeño (cero dependencias): escapa HTML
PRIMERO (seguridad) y luego aplica formato. Soporta #/##/###, **negritas**,
*cursivas*, `codigo`, listas - y 1. (con un nivel de anidado), checklists
"- [ ]" → checkbox deshabilitado, tablas |a|b|, callouts ⚠️, separadores ---,
ligas [texto](url), bloques ``` (los checklists imprimibles de los SOPs se
conservan tal cual en un <pre>) y párrafos. Los h2 llevan id para el TOC.

Los partials generados se COMMITEAN: las vistas y los tests no dependen de la
carpeta fuente, solo de templates/manuales/.
"""
import json
import re
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Emoji fijo por SOP (spec del módulo): identidad visual estable de cada manual.
EMOJIS = {
    "SOP-01": "🌅", "SOP-02": "🚚", "SOP-03": "📦", "SOP-04": "🗄️",
    "SOP-05": "🛒", "SOP-06": "📸", "SOP-07": "🚀", "SOP-08": "🏠",
    "SOP-09": "🔢", "SOP-10": "🚧", "SOP-11": "🦺", "SOP-12": "🚨",
    "SOP-13": "🧹", "README": "📖",
}

# Agrupación del índice por número de SOP (inclusive).
GRUPOS = [("Operación diaria", 1, 8), ("Control", 9, 10), ("Seguridad", 11, 13)]

# Encabezado consistente de los .md: fila |Campo|Valor| → etiqueta de la pill.
# El orden de esta lista es el orden de la fila de propiedades estilo Notion.
CAMPOS_PROPIEDADES = [
    ("dueno", "Dueño"),
    ("frecuencia / disparador", "Frecuencia"),
    ("tiempo estandar", "Tiempo estándar"),
    ("epp requerido", "EPP"),
    ("pantallas de torre", "Pantallas de Torre"),
]
CAMPO_VERSION = "codigo / version"

PALABRAS_POR_MINUTO = 200

# ── Regex del conversor ──
_RE_ITEM = re.compile(r"^(\s*)(?:([-*])|(\d+)\.)\s+(.+)$")
_RE_SEPARADOR_TABLA = re.compile(r"^\|[\s:|\-]+\|$")
_RE_HR = re.compile(r"^-{3,}$")
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_ITAL = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_RE_CHECK = re.compile(r"^\[( |x|X)\]\s+(.*)$")

# Placeholders internos (nunca aparecen en texto real).
_PH_AST = "\x00"     # \* escapado
_PH_PIPE = "\x01"    # \| escapado
_PH_CODE = "\x02"    # span de código extraído


def _escapar(texto):
    """Escapa HTML y llaves ANTES de cualquier formato.

    Las llaves se vuelven entidades para que el partial sea inerte como
    template de Django ({% include %} no debe interpretar nada del contenido).
    """
    return (
        texto.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
    )


def _sin_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )


def _slugificar(texto):
    """Ancla legible para los h2 del TOC: minúsculas, sin acentos, con guiones."""
    plano = _sin_acentos(texto).lower()
    plano = re.sub(r"[^a-z0-9]+", "-", plano)
    return plano.strip("-") or "seccion"


def texto_plano(texto):
    """Quita los marcadores de markdown para TOC, resumen y conteo de palabras."""
    texto = texto.replace("\\*", "*").replace("\\|", "|")
    texto = _RE_LINK.sub(r"\1", texto)
    return texto.replace("**", "").replace("`", "").replace("⚠️", "").strip(" *")


def _slug_archivo(nombre):
    """SOP-02-recepcion-descarga.md → sop-02-recepcion-descarga · README.md → readme."""
    return Path(nombre).stem.lower()


def _link_html(m):
    texto, destino = m.group(1), m.group(2)
    if destino.endswith(".md"):
        # Liga interna entre manuales → href relativo al índice (<slug>/), que
        # resuelve igual bajo /mesa/manuales/ que bajo /portal/manuales/.
        destino = f"{_slug_archivo(destino)}/"
    return f'<a href="{destino}">{texto}</a>'


def inline(texto):
    """Formato en línea sobre texto YA escapado: código, ligas, negritas, cursivas."""
    texto = texto.replace("\\*", _PH_AST).replace("\\|", _PH_PIPE)
    texto = _escapar(texto)

    # Los spans de código se extraen primero: dentro de un `code` no hay formato.
    codigos = []

    def _guardar_codigo(m):
        codigos.append(m.group(1))
        return f"{_PH_CODE}{len(codigos) - 1}{_PH_CODE}"

    texto = _RE_CODE.sub(_guardar_codigo, texto)
    texto = _RE_LINK.sub(_link_html, texto)
    texto = _RE_BOLD.sub(r"<strong>\1</strong>", texto)
    texto = _RE_ITAL.sub(r"<em>\1</em>", texto)
    for i, cod in enumerate(codigos):
        texto = texto.replace(f"{_PH_CODE}{i}{_PH_CODE}", f"<code>{cod}</code>")
    return texto.replace(_PH_AST, "*").replace(_PH_PIPE, "|")


# ─────────────────────────────────────────────────────────────────────────────
# Bloques
# ─────────────────────────────────────────────────────────────────────────────


def _celdas(fila):
    """Parte una fila |a|b| en celdas, respetando \\| escapado."""
    fila = fila.strip().replace("\\|", _PH_PIPE)
    fila = fila.strip("|")
    return [c.strip().replace(_PH_PIPE, "\\|") for c in fila.split("|")]


def _leer_tabla(lineas, i):
    """Consume las líneas |...| consecutivas desde i y regresa (html, i_final)."""
    filas = []
    while i < len(lineas) and lineas[i].strip().startswith("|"):
        filas.append(lineas[i].strip())
        i += 1
    encabezado, cuerpo = None, filas
    if len(filas) >= 2 and _RE_SEPARADOR_TABLA.match(filas[1].replace(" ", "")):
        encabezado, cuerpo = filas[0], filas[2:]

    partes = ['<div class="tabla-wrap"><table>']
    if encabezado is not None:
        ths = "".join(f"<th>{inline(c)}</th>" for c in _celdas(encabezado))
        partes.append(f"<thead><tr>{ths}</tr></thead>")
    partes.append("<tbody>")
    for fila in cuerpo:
        tds = "".join(f"<td>{inline(c)}</td>" for c in _celdas(fila))
        partes.append(f"<tr>{tds}</tr>")
    partes.append("</tbody></table></div>")
    return "".join(partes), i


def _abrir_li(texto):
    """<li> según el tipo: checklist → checkbox deshabilitado; ⚠️ → li de alerta."""
    m = _RE_CHECK.match(texto)
    if m:
        marcado = " checked" if m.group(1).lower() == "x" else ""
        return (
            f'<li class="check"><label><input type="checkbox" disabled{marcado}> '
            f"{inline(m.group(2))}</label>"
        )
    if "⚠️" in texto:
        return f'<li class="warn">{inline(texto)}'
    return f"<li>{inline(texto)}"


def _render_lista(items, pos, nivel):
    """Render recursivo de items (nivel, numero|None, texto) → (html, pos_final)."""
    ordenado = items[pos][1] is not None
    tag = "ol" if ordenado else "ul"
    inicio = items[pos][1]
    attrs = f' start="{inicio}"' if ordenado and inicio not in (None, 1) else ""
    partes = [f"<{tag}{attrs}>"]
    abierto = False
    while pos < len(items):
        item_nivel, _numero, texto = items[pos]
        if item_nivel < nivel:
            break
        if item_nivel > nivel:
            sub, pos = _render_lista(items, pos, item_nivel)
            partes.append(sub)
            continue
        if abierto:
            partes.append("</li>")
        partes.append(_abrir_li(texto))
        abierto = True
        pos += 1
    if abierto:
        partes.append("</li>")
    partes.append(f"</{tag}>")
    return "".join(partes), pos


def _leer_lista(lineas, i):
    """Consume los items de lista consecutivos desde i y regresa (html, i_final)."""
    items = []
    while i < len(lineas):
        m = _RE_ITEM.match(lineas[i])
        if not m:
            break
        indent = len(m.group(1))
        nivel = 0 if indent == 0 else 1  # los manuales anidan un solo nivel
        numero = int(m.group(3)) if m.group(3) else None
        items.append((nivel, numero, m.group(4).strip()))
        i += 1
    html, _ = _render_lista(items, 0, items[0][0])
    return html, i


def convertir(md):
    """Markdown → (titulo_h1, cuerpo_html, toc_h2, resumen_primer_parrafo)."""
    lineas = md.splitlines()
    html, toc = [], []
    titulo, resumen = None, None
    i, n = 0, len(lineas)
    while i < n:
        s = lineas[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("# ") and not s.startswith("## "):
            # El H1 no va al cuerpo: lo pinta el template con emoji y propiedades.
            if titulo is None:
                titulo = texto_plano(s[2:])
            else:
                html.append(f"<h2>{inline(s[2:])}</h2>")
            i += 1
        elif s.startswith("### "):
            html.append(f"<h3>{inline(s[4:])}</h3>")
            i += 1
        elif s.startswith("## "):
            plano = texto_plano(s[3:])
            ancla = _slugificar(plano)
            toc.append({"id": ancla, "titulo": plano})
            html.append(f'<h2 id="{ancla}">{inline(s[3:])}</h2>')
            i += 1
        elif s.startswith("```"):
            # Bloque preformateado (checklists imprimibles): se conserva tal
            # cual, con espaciado original, sin formato en línea.
            i += 1
            crudas = []
            while i < n and not lineas[i].strip().startswith("```"):
                crudas.append(_escapar(lineas[i]))
                i += 1
            i += 1  # consume el ``` de cierre
            contenido = "\n".join(crudas).strip("\n")
            html.append(f'<pre class="pre-manual">{contenido}</pre>')
        elif _RE_HR.match(s):
            html.append("<hr>")
            i += 1
        elif s.startswith("|"):
            bloque, i = _leer_tabla(lineas, i)
            html.append(bloque)
        elif _RE_ITEM.match(lineas[i]):
            bloque, i = _leer_lista(lineas, i)
            html.append(bloque)
        else:
            # Párrafo: junta líneas consecutivas que no abren otro bloque.
            parrafo = [s]
            i += 1
            while i < n:
                sig = lineas[i].strip()
                if (
                    not sig or sig.startswith(("#", "|")) or _RE_HR.match(sig)
                    or _RE_ITEM.match(lineas[i])
                ):
                    break
                parrafo.append(sig)
                i += 1
            contenido = "<br>".join(inline(p) for p in parrafo)
            if any("⚠️" in p for p in parrafo):
                html.append(f'<div class="callout warn">{contenido}</div>')
            else:
                html.append(f"<p>{contenido}</p>")
                if resumen is None:
                    resumen = texto_plano(" ".join(parrafo))
    return titulo, "\n".join(html), toc, resumen


# ─────────────────────────────────────────────────────────────────────────────
# Encabezado de propiedades (tabla |Campo|Valor| de cada SOP)
# ─────────────────────────────────────────────────────────────────────────────


def _clave_campo(celda):
    return _sin_acentos(texto_plano(celda)).lower().strip()


def extraer_propiedades(md):
    """Separa la tabla |Campo|Valor| del encabezado y regresa (props, version, resto).

    props es una lista ordenada [etiqueta, valor_html] (fila estilo Notion).
    Si el .md no trae esa tabla (README), regresa ([], "", md intacto).
    """
    lineas = md.splitlines()
    inicio = None
    for idx, linea in enumerate(lineas):
        s = linea.strip()
        if s.startswith("|"):
            if _clave_campo(_celdas(s)[0]) == "campo":
                inicio = idx
            break  # solo la PRIMERA tabla del documento puede ser el encabezado
    if inicio is None:
        return [], "", md

    fin = inicio
    while fin < len(lineas) and lineas[fin].strip().startswith("|"):
        fin += 1

    crudos = {}
    for fila in lineas[inicio + 2:fin]:  # brinca encabezado y separador
        celdas = _celdas(fila)
        if len(celdas) >= 2:
            crudos[_clave_campo(celdas[0])] = celdas[1]

    props = [
        [etiqueta, inline(crudos[clave])]
        for clave, etiqueta in CAMPOS_PROPIEDADES
        if crudos.get(clave)
    ]
    version = ""
    if crudos.get(CAMPO_VERSION):
        # "SOP-02 · v1.0 · Agosto 2026" → "v1.0 · Agosto 2026" (el código ya se ve).
        version = texto_plano(crudos[CAMPO_VERSION])
        version = re.sub(r"^SOP-\d+\s*·\s*", "", version)
    resto = "\n".join(lineas[:inicio] + lineas[fin:])
    return props, version, resto


def _codigo_de(stem):
    m = re.match(r"(SOP-\d{2})", stem)
    return m.group(1) if m else stem.upper()


def _grupo_de(codigo):
    m = re.match(r"SOP-(\d+)", codigo)
    if not m:
        return ""
    numero = int(m.group(1))
    for nombre, desde, hasta in GRUPOS:
        if desde <= numero <= hasta:
            return nombre
    return ""


def renderizar_manual(nombre_archivo, md):
    """Un .md completo → (slug, entrada_de_indice, cuerpo_html)."""
    slug = _slug_archivo(nombre_archivo)
    codigo = _codigo_de(Path(nombre_archivo).stem)
    props, version, resto = extraer_propiedades(md)
    titulo_h1, cuerpo, toc, resumen = convertir(resto)

    titulo = titulo_h1 or codigo
    # "SOP-02 · Recepción y descarga (polipasto)" → título sin el código.
    titulo = re.sub(rf"^{re.escape(codigo)}\s*·\s*", "", titulo)

    palabras = len(texto_plano(resto).split())
    entrada = {
        "slug": slug,
        "codigo": codigo,
        "titulo": titulo,
        "emoji": EMOJIS.get(codigo, "📄"),
        "resumen": resumen or "",
        "palabras": palabras,
        "minutos": max(1, round(palabras / PALABRAS_POR_MINUTO)),
        "grupo": _grupo_de(codigo),
        "version": version,
        "propiedades": props,
        "toc": toc,
    }
    return slug, entrada, cuerpo


class Command(BaseCommand):
    help = (
        "Pre-renderiza los manuales (.md) a partials HTML en templates/manuales/ "
        "+ _indice.json. Los archivos generados se commitean al repo."
    )

    def handle(self, *args, **options):
        base = Path(settings.BASE_DIR)
        # Fuente canónica: torre/manuales/ (viaja en el repo). El fallback a la
        # carpeta hermana existe solo por compatibilidad con el layout viejo.
        default = base / "manuales" if (base / "manuales").exists() else base.parent / "manuales"
        fuente = Path(settings.TORRE.get("MANUALES_DIR", default))
        destino = base / "templates" / "manuales"
        if not fuente.is_dir():
            raise CommandError(f"No existe la carpeta fuente de manuales: {fuente}")

        archivos = sorted(fuente.glob("*.md"))
        if not archivos:
            raise CommandError(f"No hay .md en {fuente}")

        destino.mkdir(parents=True, exist_ok=True)
        indice = []
        for ruta in archivos:
            slug, entrada, cuerpo = renderizar_manual(
                ruta.name, ruta.read_text(encoding="utf-8")
            )
            (destino / f"{slug}.html").write_text(cuerpo + "\n", encoding="utf-8")
            indice.append(entrada)
            self.stdout.write(f"  {entrada['emoji']} {entrada['codigo']} → {slug}.html")

        # SOPs en orden de código; la portada (README) al final.
        indice.sort(key=lambda e: (e["codigo"] == "README", e["codigo"]))
        with open(destino / "_indice.json", "w", encoding="utf-8") as f:
            json.dump(indice, f, ensure_ascii=False, indent=1)
            f.write("\n")

        self.stdout.write(self.style.SUCCESS(
            f"{len(indice)} manuales renderizados en {destino}"
        ))
