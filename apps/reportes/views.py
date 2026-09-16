"""Vista y CSV genéricos de los reportes; Mesa y el portal las envuelven con
su decorador, su cliente y sus URLs (apps.mesa.views / apps.portal.views)."""
import csv
from decimal import Decimal

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from .base import TIPOS_NUMERICOS, filtros_desde_get, limites, rango_desde_get
from .registro import modulo, modulos


def indice(request, es_mesa, url_de):
    """Lista de reportes con su descripción; `url_de(clave)` arma el link."""
    return [
        {"clave": clave, "titulo": m.TITULO, "descripcion": m.DESCRIPCION, "url": url_de(clave)}
        for clave, m in modulos()
    ]


def _datos(request, clave, cliente, clientes, es_mesa):
    """(módulo, contexto de filtros, resultado). Mesa sin cliente = todos los
    activos concatenados con la columna Cliente al frente."""
    m = modulo(clave)
    if m is None:
        raise Http404("Reporte desconocido")
    desde, hasta, valido = rango_desde_get(request.GET)
    if not valido:
        messages.warning(request, "Alguna fecha no se entiende; se usa el mes en curso.")
    inicio, fin = limites(desde, hasta)
    filtros = filtros_desde_get(m.FILTROS, request.GET)
    columnas = list(m.COLUMNAS) + (list(getattr(m, "COLUMNAS_MESA", [])) if es_mesa else [])
    objetivo = [cliente] if cliente is not None else list(clientes)
    filas, resumen, grupos = [], {}, []
    for c in objetivo:
        r = m.generar(c, inicio, fin, filtros, es_mesa)
        prefijo = [] if cliente is not None else [c.nombre]
        filas += [prefijo + list(f) for f in r.get("filas", [])]
        for etiqueta, valor in r.get("resumen", []):
            if isinstance(valor, (int, float, Decimal)) and not isinstance(valor, bool):
                resumen[etiqueta] = resumen.get(etiqueta, 0) + valor
            elif cliente is not None:
                resumen[etiqueta] = valor
        for g in r.get("grupos", []):
            grupos.append({
                "titulo": g["titulo"] if cliente is not None else f"{c.nombre} · {g['titulo']}",
                "columnas": g["columnas"], "filas": g["filas"],
            })
    if cliente is None:
        columnas = [("Cliente", "texto")] + columnas
    return m, {
        "desde": desde, "hasta": hasta, "filtros": filtros, "columnas": columnas,
    }, {"filas": filas, "resumen": list(resumen.items()), "grupos": grupos}


def _celda(valor, tipo):
    """Valor formateado para pantalla según el tipo de la columna."""
    if valor is None or valor == "":
        return "—"
    if tipo == "fechahora":
        return timezone.localtime(valor).strftime("%d/%b %H:%M")
    if tipo == "fecha":
        return valor.strftime("%d/%b/%Y")
    if tipo == "dinero":
        return f"${Decimal(str(valor)):,.2f}"
    if tipo == "pct":
        return f"{float(valor):.1f}%"
    if tipo == "horas":
        return f"{float(valor):.1f} h"
    if tipo == "decimal":
        return f"{Decimal(str(valor)):,.2f}"
    if tipo == "entero":
        return f"{int(valor):,}"
    return str(valor)


def _formatear(columnas, filas):
    tipos = [t for _n, t in columnas]
    return [[(_celda(v, t), t in TIPOS_NUMERICOS) for v, t in zip(fila, tipos)] for fila in filas]


def render_reporte(request, clave, *, cliente, clientes, es_mesa, url_base, url_csv, url_indice, seccion):
    """Página del reporte: filtros, resumen, tabla principal y grupos."""
    m, ctx, resultado = _datos(request, clave, cliente, clientes, es_mesa)
    params = request.GET.urlencode()
    contexto = {
        "seccion": seccion, "es_mesa": es_mesa, "cliente": cliente, "clientes": clientes,
        "clave": clave, "titulo": m.TITULO, "descripcion": m.DESCRIPCION,
        "con_fechas": m.CON_FECHAS, "definiciones": m.FILTROS,
        "url_base": url_base, "url_indice": url_indice,
        "url_csv": url_csv + (f"?{params}" if params else ""),
        "encabezados": [n for n, _t in ctx["columnas"]],
        "filas": _formatear(ctx["columnas"], resultado["filas"]),
        "n_filas": len(resultado["filas"]),
        "resumen": resultado["resumen"],
        "grupos": [
            {"titulo": g["titulo"], "encabezados": [n for n, _t in g["columnas"]],
             "filas": _formatear(g["columnas"], g["filas"])}
            for g in resultado["grupos"]
        ],
        **ctx,
    }
    return render(request, "reportes/tabla.html", contexto)


def csv_reporte(request, clave, *, cliente, clientes, es_mesa):
    """El mismo reporte como CSV (BOM para Excel): tabla principal y, debajo,
    cada grupo con su título. Valores crudos (fechas ISO, números sin formato)."""
    _m, ctx, resultado = _datos(request, clave, cliente, clientes, es_mesa)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    sufijo = f"-{cliente.slug}" if cliente is not None else ""
    nombre = f"{clave}{sufijo}-{ctx['desde'].isoformat()}-{ctx['hasta'].isoformat()}.csv"
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre}"'
    respuesta.write("\ufeff")  # BOM: que Excel abra los acentos bien
    w = csv.writer(respuesta)
    w.writerow([n for n, _t in ctx["columnas"]])
    w.writerows([_crudo(v) for v in fila] for fila in resultado["filas"])
    for g in resultado["grupos"]:
        w.writerow([])
        w.writerow([g["titulo"]])
        w.writerow([n for n, _t in g["columnas"]])
        w.writerows([_crudo(v) for v in fila] for fila in g["filas"])
    return respuesta


def _crudo(valor):
    if valor is None:
        return ""
    if hasattr(valor, "tzinfo") and valor.tzinfo is not None:
        return timezone.localtime(valor).strftime("%Y-%m-%d %H:%M")
    return valor
