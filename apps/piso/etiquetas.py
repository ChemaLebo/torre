"""Etiquetas en PDF para las térmicas de bodega: la de envío (10×15, Aiyin
BY-480BT) y, desde 2026-10-13, las de recepción (51×51 mm, Munbyn ITPP130B):
caja (QR `P|barras|lote`, dañada con banda) y tarima (QR `U|TAR-nn`),
impresas solas al terminar el conteo, tarima por tarima y en orden inverso
al escaneo, en trabajos del tamaño de tanda configurado.

`generar_pdf_etiqueta` dibuja la misma etiqueta 10×15 de piso/etiqueta.html
pero en PDF de alto contraste para térmica 203 dpi: Code128 del número de guía
y QR a la página pública del repartidor. `imprimir_etiqueta` la manda directo a
la cola CUPS del servidor con `lp` — el packer imprime desde la tablet con un
botón, sin pelearse con diálogos de impresión.

Desde 2026-10-20 (Chema) la etiqueta INTERNA de un pedido foráneo es la de
51×51 en la Munbyn (`generar_pdf_etiqueta_51`): folio, PAQUETE n DE m,
paquetería y el QR del repartidor; sin dirección ni peso. En el MISMO trabajo
salen las etiquetas de manejo que pida la caja: FRÁGIL (copa ISO 780) si algún
producto trae `fragil`, orientación (flechas ISO 780) si alguno trae
`este_lado_arriba`, tantas como diga `configuracion.etiquetas_manejo()`.
La 10×15 de siempre queda para pedidos locales (`Pedido.es_local`). Salida
imprime etiquetas de manejo sueltas con `generar_pdf_manejo`.

Con `TORRE_MODO_IMPRESION=relay` (Torre en un VPS, la térmica en bodega) la
etiqueta no va a `lp`: se encola en `envios.TrabajoImpresion` y el agente de
bodega (deploy/agente_impresion.py) la baja, la imprime y confirma.
"""
import contextlib
import os
import re
import subprocess
import tempfile
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128, qrencoder
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from apps.core.services import registrar_evento

# ── Geometría de la etiqueta (4×6" = 100×150 mm, térmica 203 dpi) ────────────
ANCHO = 100 * mm
ALTO = 150 * mm
MARGEN = 4 * mm
ANCHO_UTIL = ANCHO - 2 * MARGEN

# Code128: a 203 dpi el punto mide 0.125 mm; con módulos por debajo de ~2
# puntos la térmica ya no imprime barras confiables → mejor solo texto grande.
BARRA_MODULO_MIN = 0.24 * mm
BARRA_MODULO_MAX = 0.50 * mm
BARRA_ALTO = 13 * mm
QR_LADO = 38 * mm  # ~38×38 mm, escaneable con guante y a la primera

NEGRITA = "Helvetica-Bold"
NORMAL = "Helvetica"
MONO = "Courier"
MONO_NEGRITA = "Courier-Bold"

MEDIA_LP = "media=Custom.100x150mm"


# ─────────────────────────────────────────────────────────────────────────────
# Texto: recorte y envoltura medidos con la métrica real de la fuente
# ─────────────────────────────────────────────────────────────────────────────


def _recortar(texto, fuente, tamano, ancho_max):
    """Recorta con … para que el texto quepa en ancho_max (en puntos)."""
    texto = str(texto)
    if stringWidth(texto, fuente, tamano) <= ancho_max:
        return texto
    while texto and stringWidth(texto + "…", fuente, tamano) > ancho_max:
        texto = texto[:-1]
    return texto + "…"


def _envolver(texto, fuente, tamano, ancho_max, max_lineas=2):
    """Parte por palabras en hasta max_lineas que quepan; la última se recorta."""
    lineas, actual = [], ""
    for palabra in str(texto).split():
        candidata = f"{actual} {palabra}".strip()
        if not actual or stringWidth(candidata, fuente, tamano) <= ancho_max:
            actual = candidata
        else:
            lineas.append(actual)
            actual = palabra
    if actual:
        lineas.append(actual)
    if len(lineas) > max_lineas:
        resto = " ".join(lineas[max_lineas - 1:])
        lineas = lineas[:max_lineas - 1] + [_recortar(resto, fuente, tamano, ancho_max)]
    return lineas


def _envolver_crudo(texto, fuente, tamano, ancho_max, max_lineas=3):
    """Envoltura por caracteres (URLs y cadenas sin espacios)."""
    lineas, actual = [], ""
    for caracter in str(texto):
        if actual and stringWidth(actual + caracter, fuente, tamano) > ancho_max:
            lineas.append(actual)
            actual = ""
            if len(lineas) == max_lineas:
                return lineas
        actual += caracter
    if actual:
        lineas.append(actual)
    return lineas


# ─────────────────────────────────────────────────────────────────────────────
# Datos del bulto (espejo de la vista piso:etiqueta)
# ─────────────────────────────────────────────────────────────────────────────


def _paquete_n_de_m(guia):
    """(N, M) del bulto; guías legacy sin paquete amparan el pedido completo."""
    if guia.paquete is not None:
        return guia.paquete.numero, guia.pedido.paquetes.count() or 1
    return 1, 1


def _peso_kg(guia):
    """Peso del bulto: la báscula de la caja si ya se pesó (Chema 2026-10-20,
    PED-00716: la etiqueta decía el plan de 14.18 kg y la báscula marcó 18.9),
    si no el del plan; legacy → peso real (o esperado) del pedido."""
    if guia.paquete is not None:
        if guia.paquete.peso_real_gr:
            return (Decimal(guia.paquete.peso_real_gr) / 1000).quantize(Decimal("0.01"))
        return guia.paquete.peso_kg
    gramos = guia.pedido.peso_real_gr or guia.pedido.peso_esperado_gr
    return (Decimal(gramos) / 1000).quantize(Decimal("0.01")) if gramos else None


# ─────────────────────────────────────────────────────────────────────────────
# Piezas del dibujo
# ─────────────────────────────────────────────────────────────────────────────


def _separador(lienzo, y):
    """Línea divisoria gruesa; regresa el cursor ya debajo de ella."""
    lienzo.setLineWidth(1.5)
    lienzo.line(MARGEN, y, ANCHO - MARGEN, y)
    return y - 6


def _code128_ajustado(numero):
    """Code128 a ancho casi completo de la etiqueta.

    None cuando no hay número o cuando meterlo al ancho exigiría módulos más
    angostos que lo que la térmica imprime bien (número muy largo → solo texto).
    """
    if not numero:
        return None
    sonda = code128.Code128(numero, barHeight=BARRA_ALTO, barWidth=1, quiet=0)
    modulo = ANCHO_UTIL / sonda.width  # sonda.width == módulos × 1 pt
    if modulo < BARRA_MODULO_MIN:
        return None
    return code128.Code128(
        numero, barHeight=BARRA_ALTO, barWidth=min(modulo, BARRA_MODULO_MAX), quiet=0
    )


def _dibujar_qr(lienzo, url, x, y):
    """QR de ~38×38 mm (nivel M) con la URL pública del repartidor."""
    widget = QrCodeWidget(url, barLevel="M")
    x1, y1, x2, y2 = widget.getBounds()
    dibujo = Drawing(
        QR_LADO, QR_LADO,
        transform=[QR_LADO / (x2 - x1), 0, 0, QR_LADO / (y2 - y1), 0, 0],
    )
    dibujo.add(widget)
    renderPDF.draw(dibujo, lienzo, x, y)
    lienzo.setLineWidth(0.5)
    lienzo.rect(x, y, QR_LADO, QR_LADO, stroke=1, fill=0)


# ─────────────────────────────────────────────────────────────────────────────
# Servicios
# ─────────────────────────────────────────────────────────────────────────────


def marcas_de_manejo(guia):
    """(fragil, este_lado_arriba) de la caja de la guía: True si alguna pieza
    de la caja (o del pedido, sin caja) lleva la bandera en su SKU."""
    if guia.paquete_id:
        skus = [pl.linea_pedido.sku for pl in guia.paquete.lineas.select_related("linea_pedido__sku")]
    else:
        skus = [l.sku for l in guia.pedido.lineas.select_related("sku")]
    return any(s.fragil for s in skus), any(s.este_lado_arriba for s in skus)


BANDA_LINEA = 22  # alto de cada renglón de la banda de manejo (pt)
BANDA_FUENTE = 17


def _flecha_arriba(lienzo, x, y_base, alto):
    """Flecha hacia arriba dibujada (Helvetica no trae "↑"): asta + punta, en blanco."""
    lienzo.setLineWidth(2.2)
    lienzo.line(x, y_base, x, y_base + alto)
    punta = lienzo.beginPath()
    punta.moveTo(x - 4.5, y_base + alto - 5)
    punta.lineTo(x, y_base + alto + 1)
    punta.lineTo(x + 4.5, y_base + alto - 5)
    punta.close()
    lienzo.drawPath(punta, stroke=0, fill=1)


def _banda_manejo(lienzo, y, fragil, arriba):
    """Banda negra con FRÁGIL y/o ESTE LADO ARRIBA en blanco, grande (Chema
    2026-09-30: MUCHO más visible), un renglón por aviso; regresa el y nuevo."""
    renglones = [t for t, aplica in (("FRÁGIL", fragil), ("ESTE LADO ARRIBA", arriba)) if aplica]
    if not renglones:
        return y
    alto = BANDA_LINEA * len(renglones) + 4
    y -= alto + 3
    lienzo.rect(MARGEN, y, ANCHO_UTIL, alto, stroke=0, fill=1)
    lienzo.setFillColorRGB(1, 1, 1)
    lienzo.setStrokeColorRGB(1, 1, 1)
    lienzo.setFont(NEGRITA, BANDA_FUENTE)
    for indice, texto in enumerate(renglones):
        y_linea = y + alto - 2 - BANDA_LINEA * (indice + 1) + 5
        if texto == "ESTE LADO ARRIBA":
            ancho_texto = stringWidth(texto, NEGRITA, BANDA_FUENTE)
            x_texto = ANCHO / 2 - ancho_texto / 2 + 7
            lienzo.drawString(x_texto, y_linea, texto)
            _flecha_arriba(lienzo, x_texto - 11, y_linea - 2, BANDA_FUENTE - 2)
        else:
            lienzo.drawCentredString(ANCHO / 2, y_linea, texto)
    lienzo.setFillColorRGB(0, 0, 0)
    lienzo.setStrokeColorRGB(0, 0, 0)
    return y


def generar_pdf_etiqueta(guia):
    """PDF de UNA página 100×150 mm listo para la térmica. Regresa los bytes.

    Blanco y negro de alto contraste; mismo contenido que piso/etiqueta.html:
    banda de manejo, folio y paquete N de M enormes (es la etiqueta interna),
    carrier + Code128 chicos, destinatario compacto, referencias y QR a la
    página del repartidor con peso y folio al lado. JAMÁS imprime
    valor declarado ni teléfono del comprador.
    """
    from apps.rastreo.services import (  # lazy por contrato
        obtener_o_crear_token_etiqueta,
        referencias_direccion,
        url_publica_etiqueta,
    )

    pedido = guia.pedido
    direccion = pedido.direccion or {}
    paquete_n, paquete_m = _paquete_n_de_m(guia)  # sin nombre del cliente (Chema 2026-10-01)
    cp = pedido.cp or str(direccion.get("zip") or "")
    referencias = referencias_direccion(direccion)
    obtener_o_crear_token_etiqueta(guia)  # el QR necesita el token vivo
    qr_url = url_publica_etiqueta(guia)
    peso_kg = _peso_kg(guia)

    buffer = BytesIO()
    lienzo = canvas.Canvas(buffer, pagesize=(ANCHO, ALTO), pageCompression=0)
    lienzo.setTitle(f"Etiqueta {guia.numero or pedido.folio}")
    lienzo.setFillColorRGB(0, 0, 0)
    lienzo.setStrokeColorRGB(0, 0, 0)

    y = ALTO - MARGEN  # cursor: se dibuja de arriba hacia abajo

    # Jerarquía (Chema 2026-10-01): es la etiqueta INTERNA, así que lo grande
    # es lo interno: 1) banda de manejo, 2) folio, 3) paquete N de M. El CP y
    # el número del carrier ya vienen en la guía del carrier: van chicos.

    # 1) Banda de manejo arriba del todo, solo si alguna pieza lo pide.
    fragil, arriba = marcas_de_manejo(guia)
    y = _banda_manejo(lienzo, y + 3, fragil, arriba) if (fragil or arriba) else y

    # 2) Folio enorme y PAQUETE N DE M enorme, centrados.
    y -= 36
    tamano_folio = 36
    while tamano_folio > 18 and stringWidth(pedido.folio, MONO_NEGRITA, tamano_folio) > ANCHO_UTIL:
        tamano_folio -= 1
    lienzo.setFont(MONO_NEGRITA, tamano_folio)
    lienzo.drawCentredString(ANCHO / 2, y, pedido.folio)
    y -= 30
    etiqueta_paquete = f"PAQUETE {paquete_n} DE {paquete_m}"
    tamano_paquete = 28
    while tamano_paquete > 14 and stringWidth(etiqueta_paquete, NEGRITA, tamano_paquete) > ANCHO_UTIL:
        tamano_paquete -= 1
    lienzo.setFont(NEGRITA, tamano_paquete)
    lienzo.drawCentredString(ANCHO / 2, y, etiqueta_paquete)
    y = _separador(lienzo, y - 8)

    # 3) Carrier + número de guía: chico (ya viene en la guía del carrier),
    #    con un Code128 bajo para escanearlo en Salida si hace falta.
    carrier = str(guia.carrier or "").upper()
    if guia.servicio:
        carrier += f" · {str(guia.servicio).upper()}"
    numero = (guia.numero or "").strip()
    y -= 9
    lienzo.setFont(NEGRITA, 8)
    lienzo.drawString(MARGEN, y, _recortar(carrier, NEGRITA, 8, ANCHO_UTIL * 0.36))
    lienzo.setFont(MONO_NEGRITA, 9)
    lienzo.drawRightString(ANCHO - MARGEN, y, _recortar(numero or "SIN NÚMERO", MONO_NEGRITA, 9, ANCHO_UTIL * 0.62))
    barras = _code128_ajustado(numero)
    if barras is not None:
        barras.barHeight = 7 * mm
        y -= 3 + 7 * mm
        barras.drawOn(lienzo, MARGEN + (ANCHO_UTIL - barras.width) / 2, y)
    y = _separador(lienzo, y - 4)

    # 4) Destinatario compacto: nombre, dirección y CP a tamaño normal.
    y -= 10
    lienzo.setFont(NEGRITA, 10)
    lienzo.drawString(MARGEN, y, _recortar(pedido.comprador_nombre or "—", NEGRITA, 10, ANCHO_UTIL))
    lineas_direccion = []
    if direccion.get("address1"):
        lineas_direccion += _envolver(direccion["address1"], NORMAL, 8.5, ANCHO_UTIL, max_lineas=2)
    if direccion.get("address2"):
        lineas_direccion += _envolver(direccion["address2"], NORMAL, 8.5, ANCHO_UTIL, max_lineas=1)
    ciudad = ", ".join(
        parte
        for parte in (
            str(direccion.get("city") or "").strip(),
            str(direccion.get("province") or "").strip(),
        )
        if parte
    )
    if ciudad:
        lineas_direccion += _envolver(f"{ciudad} · CP {cp or '—'}", NORMAL, 8.5, ANCHO_UTIL, max_lineas=1)
    else:
        lineas_direccion.append(f"CP {cp or '—'}")
    lienzo.setFont(NORMAL, 8.5)
    for linea in lineas_direccion:
        y -= 10
        lienzo.drawString(MARGEN, y, linea)
    y = _separador(lienzo, y - 5)
    # 4) Referencias destacadas: banda negra con texto blanco (o manda al QR).
    # Cuántos renglones de referencias caben sin que el QR pise la banda:
    # lo que queda arriba del pie menos el QR y el marco de la banda.
    y_pie = MARGEN + 14
    caben = max(1, min(3, int((y - y_pie - QR_LADO - 8 - 16) / 12)))
    if referencias:
        lineas_refs = []
        for referencia in referencias:
            lineas_refs += _envolver(referencia, NEGRITA, 10, ANCHO_UTIL - 4 * mm, max_lineas=2)
        lineas_refs = lineas_refs[:caben]
    else:
        lineas_refs = ["Sin referencias — usa el QR"]
    alto_banda = 16 + 12 * len(lineas_refs)  # 6 pt de aire bajo el último renglón
    y -= alto_banda
    lienzo.rect(MARGEN, y, ANCHO_UTIL, alto_banda, stroke=0, fill=1)
    lienzo.setFillColorRGB(1, 1, 1)
    lienzo.setFont(NEGRITA, 7)
    lienzo.drawString(MARGEN + 2 * mm, y + alto_banda - 10, "REFERENCIAS")
    lienzo.setFont(NEGRITA, 10)
    for indice, linea in enumerate(lineas_refs):
        lienzo.drawString(MARGEN + 2 * mm, y + alto_banda - 22 - 12 * indice, linea)
    lienzo.setFillColorRGB(0, 0, 0)
    y -= 8

    # 5) QR a la página del repartidor + leyenda (nunca invade el pie).
    y_qr = max(y - QR_LADO, y_pie)
    _dibujar_qr(lienzo, qr_url, MARGEN, y_qr)
    x_leyenda = MARGEN + QR_LADO + 4 * mm
    ancho_leyenda = ANCHO - MARGEN - x_leyenda
    # Lado derecho del QR (Chema 2026-10-01: que no quede vacío): peso grande,
    # folio y la leyenda corta del repartidor con su URL chica.
    y_texto = y_qr + QR_LADO - 16
    if peso_kg is not None:
        lienzo.setFont(NEGRITA, 18)
        lienzo.drawString(x_leyenda, y_texto, f"{peso_kg} kg")
        y_texto -= 16
    lienzo.setFont(MONO_NEGRITA, 11)
    lienzo.drawString(x_leyenda, y_texto, _recortar(pedido.folio, MONO_NEGRITA, 11, ancho_leyenda))
    y_texto -= 16
    lienzo.setFont(NEGRITA, 9)
    for linea in _envolver("Escanea para ayuda al repartidor", NEGRITA, 9, ancho_leyenda, max_lineas=2):
        lienzo.drawString(x_leyenda, y_texto, linea)
        y_texto -= 11
    lienzo.setFont(MONO, 6)
    y_texto -= 1
    for linea in _envolver_crudo(qr_url, MONO, 6, ancho_leyenda, max_lineas=3):
        lienzo.drawString(x_leyenda, y_texto, linea)
        y_texto -= 7

    # 6) Pie: peso + manejo + folio chico. JAMÁS valor declarado ni teléfono.
    lienzo.setLineWidth(1.5)
    lienzo.line(MARGEN, MARGEN + 12, ANCHO - MARGEN, MARGEN + 12)
    lienzo.setFont(NEGRITA, 9)
    if peso_kg is not None:
        lienzo.drawString(MARGEN, MARGEN + 3, f"{peso_kg} kg")
    pie = " · ".join(t for t, aplica in (("FRÁGIL", fragil), ("ESTE LADO ARRIBA", arriba)) if aplica)
    if pie:
        lienzo.drawCentredString(ANCHO / 2, MARGEN + 3, pie)
    lienzo.setFont(MONO, 8)
    lienzo.drawRightString(ANCHO - MARGEN, MARGEN + 3, pedido.folio)

    lienzo.showPage()
    lienzo.save()
    return buffer.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Etiquetas de recepción 51×51 mm (Chema 2026-10-13): caja y tarima
# ─────────────────────────────────────────────────────────────────────────────
# Calcadas del HTML de Chema (etiqueta-caja-51mm.html): arriba marca y SKU,
# producto en Clash, presentación; abajo LOTE y CONS. PREF. a la izquierda y
# el QR a la derecha con módulos de 7 puntos de la térmica (0.875 mm) para
# que lea a la primera; la dañada lleva una banda negra. La tarima: nombre en
# grande y QR `U|TAR-nn`. Fuentes TTF en static/fonts (ver LICENCIAS).

MEDIA_ETIQUETAS = "Custom.51x51mm"
ETIQUETA_LADO = 51 * mm
ETIQUETA_PAD_ARRIBA, ETIQUETA_PAD_DER, ETIQUETA_PAD_ABAJO, ETIQUETA_PAD_IZQ = 2.6 * mm, 3 * mm, 2 * mm, 3 * mm
ETIQUETA_ANCHO_UTIL = ETIQUETA_LADO - ETIQUETA_PAD_IZQ - ETIQUETA_PAD_DER
PUNTO_TERMICA = 25.4 / 203 * mm  # un punto de la térmica de 203 dpi
NOMBRE_PT_MAX, NOMBRE_PT_MIN, NOMBRE_MAX_CARACTERES = 17.5, 10.0, 40
FUENTES_DIR = Path(settings.BASE_DIR) / "static" / "fonts"
FUENTES_ETIQUETA = {"Clash-700": "Clash-700.ttf", "InterV-700": "InterV-700.ttf", "InterV-800": "InterV-800.ttf"}
RESPALDO_FUENTE = {"Clash-700": "Helvetica-Bold", "InterV-700": "Helvetica-Bold", "InterV-800": "Helvetica-Bold"}
_fuentes_registradas = {}


def _fuente(nombre):
    """La fuente TTF registrada en reportlab (una vez por proceso); si el
    archivo no está, Helvetica-Bold para que la etiqueta salga igual."""
    if not _fuentes_registradas:
        for clave, archivo in FUENTES_ETIQUETA.items():
            ruta = FUENTES_DIR / archivo
            try:
                pdfmetrics.registerFont(TTFont(clave, str(ruta)))
                _fuentes_registradas[clave] = clave
            except Exception:  # archivo ausente o corrupto: respaldo
                _fuentes_registradas[clave] = RESPALDO_FUENTE[clave]
    return _fuentes_registradas[nombre]


def _ancho_em(texto, fuente, pt, em=0.0):
    """Ancho de un texto con letter-spacing en em, como lo mide el navegador."""
    return stringWidth(texto, fuente, pt) + em * pt * max(len(texto) - 1, 0)


def _encoger(texto, fuente, pt, em, maximo, minimo):
    """Baja el tamaño de 0.25 en 0.25 pt hasta que el texto quepa (fit() del HTML)."""
    while _ancho_em(texto, fuente, pt, em) > maximo and pt > minimo:
        pt -= 0.25
    return pt


def _texto_em(lienzo, x, y, s, fuente, pt, em=0.0, derecha=False):
    """Texto con letter-spacing (vive en el objeto de texto, no en el lienzo)."""
    if derecha:
        x -= _ancho_em(s, fuente, pt, em)
    t = lienzo.beginText(x, y)
    t.setFont(fuente, pt)
    t.setCharSpace(em * pt)
    t.textOut(s)
    lienzo.drawText(t)


def _qr_termica(valor, puntos_por_modulo, silencio=4):
    """QR nivel M con módulos de N puntos exactos de la térmica: (Drawing, lado)."""
    qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    qr.addData(valor)
    qr.make()
    lado = (qr.getModuleCount() + 2 * silencio) * puntos_por_modulo * PUNTO_TERMICA
    widget = QrCodeWidget(valor, barLevel="M", barBorder=silencio, barWidth=lado, barHeight=lado)
    dibujo = Drawing(lado, lado)
    dibujo.add(widget)
    return dibujo, lado


def _partir_nombre(texto, fuente, ancho):
    """Regla de nombres largos (Chema 2026-10-13): se encoge de 17.5 a 10 pt;
    si a 10 pt no cabe, dos renglones de 10 pt partidos por el espacio más
    cercano a la mitad; pasando 40 caracteres el segundo renglón se corta con
    puntos suspensivos. Regresa (tamaño, [renglones])."""
    pt = _encoger(texto, fuente, NOMBRE_PT_MAX, -0.01, ancho, NOMBRE_PT_MIN)
    if _ancho_em(texto, fuente, pt, -0.01) <= ancho:
        return pt, [texto]
    texto = texto[:NOMBRE_MAX_CARACTERES].rstrip() + ("…" if len(texto) > NOMBRE_MAX_CARACTERES else "")
    espacios = [i for i, c in enumerate(texto) if c == " "]
    if not espacios:
        return NOMBRE_PT_MIN, [_recortar(texto, fuente, NOMBRE_PT_MIN, ancho)]
    corte = min(espacios, key=lambda i: abs(i - len(texto) / 2))
    primero, segundo = texto[:corte].strip(), texto[corte + 1:].strip()
    return NOMBRE_PT_MIN, [_recortar(primero, fuente, NOMBRE_PT_MIN, ancho), _recortar(segundo, fuente, NOMBRE_PT_MIN, ancho)]


def dibujar_etiqueta_caja(lienzo, *, marca, sku, producto, presentacion, lote, caducidad, barras, qr, danada=False):
    """Una etiqueta de caja 51×51 en el lienzo (sin showPage)."""
    clash, inter, inter_negra = _fuente("Clash-700"), _fuente("InterV-700"), _fuente("InterV-800")
    lienzo.setFillGray(0)
    lienzo.setStrokeGray(0)
    x0, x1 = ETIQUETA_PAD_IZQ, ETIQUETA_LADO - ETIQUETA_PAD_DER
    y = ETIQUETA_LADO - ETIQUETA_PAD_ARRIBA

    marca, sku, producto, presentacion = (str(marca or "").upper(), str(sku or "").upper(), str(producto or "").upper(), str(presentacion or "").upper())
    pt = _encoger(marca + "  " + sku, inter, 5.4, 0.14, ETIQUETA_ANCHO_UTIL, 4)
    y -= pt
    _texto_em(lienzo, x0, y, marca, inter, pt, 0.14)
    _texto_em(lienzo, x1, y, sku, inter, pt, 0.14, derecha=True)
    pt, renglones = _partir_nombre(producto, clash, ETIQUETA_ANCHO_UTIL)
    for renglon in renglones:
        y -= 1.1 * mm + pt * 0.95
        _texto_em(lienzo, x0, y + pt * 0.12, renglon, clash, pt, -0.01)
    if presentacion:
        pt = _encoger(presentacion, inter, 6.9, 0.10, ETIQUETA_ANCHO_UTIL, 5)
        y -= 1 * mm + pt
        _texto_em(lienzo, x0, y, _recortar(presentacion, inter, pt, ETIQUETA_ANCHO_UTIL / (1 + 0.10)), inter, pt, 0.10)
    y -= 1.4 * mm
    lienzo.setLineWidth(0.55 * mm)
    lienzo.line(x0, y, x1, y)
    y_zona = y - 0.55 * mm

    # QR a la derecha: la zona de silencio aprovecha el margen de la etiqueta.
    dibujo, lado_qr = _qr_termica(qr, 7)
    x_qr = ETIQUETA_LADO - lado_qr + 1.2 * mm
    y_qr = ETIQUETA_PAD_ABAJO + (y_zona - ETIQUETA_PAD_ABAJO - lado_qr) / 2
    renderPDF.draw(dibujo, lienzo, x_qr, y_qr)
    ancho_izq = x_qr + 4 * 7 * PUNTO_TERMICA - x0 - 1.5 * mm

    # Columna izquierda: LOTE / valor, CONS. PREF. / valor y el código de barras en chico.
    y = y_zona - 1.2 * mm
    filas = [("LOTE", str(lote or "—"))]
    if caducidad:
        filas.append(("CONS. PREF.", caducidad.strftime("%d/%m/%Y")))
    for clave, valor in filas:
        y -= 6.4
        _texto_em(lienzo, x0, y, clave, inter, 6.4, 0.12)
        vpt = _encoger(valor, inter_negra, 9.5, 0.01, ancho_izq, 6)
        y -= 0.6 * mm + vpt
        _texto_em(lienzo, x0, y, valor, inter_negra, vpt, 0.01)
        y -= 1.6 * mm
    y -= 6.0 - 0.8 * mm
    _texto_em(lienzo, x0, y, _recortar(str(barras or ""), inter, 6.0, ancho_izq), inter, 6.0, 0.08)
    if danada:
        alto = 4.2 * mm
        yb = max(ETIQUETA_PAD_ABAJO, min(ETIQUETA_PAD_ABAJO + 0.2 * mm, y - 1 * mm - alto))
        lienzo.rect(x0, yb, ancho_izq, alto, stroke=0, fill=1)
        lienzo.setFillGray(1)
        texto = "DAÑADA"
        spt = _encoger(texto, inter_negra, 9, 0.18, ancho_izq - 2 * mm, 5)
        _texto_em(lienzo, x0 + (ancho_izq - _ancho_em(texto, inter_negra, spt, 0.18)) / 2, yb + (alto - spt * 0.72) / 2, texto, inter_negra, spt, 0.18)
        lienzo.setFillGray(0)


def dibujar_etiqueta_tarima(lienzo, nombre, qr, titulo="TORRE · TARIMA"):
    """Una etiqueta de tarima o posición 51×51: el título (TORRE · TARIMA o
    TORRE · POSICIÓN), el nombre en grande y el QR `U|nombre`."""
    clash, inter = _fuente("Clash-700"), _fuente("InterV-700")
    lienzo.setFillGray(0)
    x0 = ETIQUETA_PAD_IZQ
    y = ETIQUETA_LADO - ETIQUETA_PAD_ARRIBA - 5.4
    _texto_em(lienzo, x0, y, titulo, inter, 5.4, 0.14)
    pt = _encoger(str(nombre).upper(), clash, 30, -0.01, ETIQUETA_ANCHO_UTIL, 10)
    y -= 1.4 * mm + pt * 0.95
    _texto_em(lienzo, x0, y + pt * 0.12, str(nombre).upper(), clash, pt, -0.01)
    y -= 1.5 * mm
    dibujo, lado_qr = _qr_termica(qr, 7)
    renderPDF.draw(dibujo, lienzo, (ETIQUETA_LADO - lado_qr) / 2, ETIQUETA_PAD_ABAJO + (y - ETIQUETA_PAD_ABAJO - lado_qr) / 2)


# ─────────────────────────────────────────────────────────────────────────────
# Etiqueta interna 51×51 + etiquetas de manejo (Chema 2026-10-20)
# ─────────────────────────────────────────────────────────────────────────────
# Prototipos aprobados el 2026-10-20: la interna lleva marca y paquetería
# arriba, folio y PAQUETE n DE m en Clash, una línea y el QR del repartidor
# con la leyenda a la derecha (módulos de 6 puntos = 0.75 mm, ~22 mm
# visibles). FRÁGIL y orientación son los símbolos 1 y 13 de ISO 780 en
# silueta negra con marco de 1.6 mm. Sin Code128: Salida resuelve por QR,
# número de guía o folio.

QR_PUNTOS_INTERNA = 6  # 0.75 mm por módulo: la URL del repartidor es más larga que P|barras|lote
LEYENDA_INTERNA_51 = "Escanea la ubicación de entrega"  # opción B (Chema 2026-10-20)
MARCA_ETIQUETA = "WOP.fulfillment"  # tal cual: WOP mayúsculas, fulfillment minúsculas
MANEJO_FRAGIL = "fragil"
MANEJO_ORIENTACION = "orientacion"
TIPOS_MANEJO = (MANEJO_FRAGIL, MANEJO_ORIENTACION)


def _envolver_mayus(texto, fuente, pt, em, ancho):
    """Renglones en mayúsculas que caben en `ancho` con letter-spacing `em`."""
    palabras, renglones, actual = texto.upper().split(), [], ""
    for palabra in palabras:
        prueba = (actual + " " + palabra).strip()
        if _ancho_em(prueba, fuente, pt, em) <= ancho or not actual:
            actual = prueba
        else:
            renglones.append(actual)
            actual = palabra
    if actual:
        renglones.append(actual)
    return renglones


def _marco_manejo(lienzo):
    """Marco negro de 1.6 mm de las etiquetas de manejo; regresa (borde, ancho útil)."""
    borde = 1.6 * mm
    lienzo.setLineWidth(borde)
    lienzo.rect(borde, borde, ETIQUETA_LADO - 2 * borde, ETIQUETA_LADO - 2 * borde, stroke=1, fill=0)
    return borde, ETIQUETA_LADO - 2 * (borde + 2.5 * mm)


def _marca_al_pie(lienzo, borde, marca):
    inter = _fuente("InterV-700")
    _texto_em(lienzo, (ETIQUETA_LADO - _ancho_em(marca, inter, 5, 0.14)) / 2, borde + 2 * mm, marca, inter, 5, 0.14)


def dibujar_interna_51(lienzo, *, folio, paquete_n, paquete_m, carrier, qr_url, leyenda=LEYENDA_INTERNA_51, marca=MARCA_ETIQUETA):
    """La etiqueta interna 51×51 de una caja: identidad de la caja y el QR
    del repartidor. Sin dirección, nombre, referencias, número de guía ni peso."""
    clash, inter, inter_negra = _fuente("Clash-700"), _fuente("InterV-700"), _fuente("InterV-800")
    lienzo.setFillGray(0)
    lienzo.setStrokeGray(0)
    x0, x1 = ETIQUETA_PAD_IZQ, ETIQUETA_LADO - ETIQUETA_PAD_DER
    y = ETIQUETA_LADO - ETIQUETA_PAD_ARRIBA

    # 1) Marca a la izquierda, paquetería a la derecha.
    carrier_txt = str(carrier or "").upper()
    pt = _encoger(marca + "    " + carrier_txt, inter, 5.6, 0.14, ETIQUETA_ANCHO_UTIL, 4)
    y -= pt
    _texto_em(lienzo, x0, y, marca, inter, pt, 0.14)
    _texto_em(lienzo, x1, y, carrier_txt, inter_negra, pt, 0.14, derecha=True)

    # 2) Folio en grande (Clash), a todo el ancho.
    pt = _encoger(folio, clash, 24, -0.01, ETIQUETA_ANCHO_UTIL, 12)
    y -= 1.3 * mm + pt * 0.95
    _texto_em(lienzo, x0, y + pt * 0.12, folio, clash, pt, -0.01)

    # 3) PAQUETE n DE m y la línea.
    paquete = f"PAQUETE {paquete_n} DE {paquete_m}"
    pt = _encoger(paquete, clash, 15, 0.03, ETIQUETA_ANCHO_UTIL, 8)
    y -= 1.0 * mm + pt * 0.95
    _texto_em(lienzo, x0, y + pt * 0.12, paquete, clash, pt, 0.03)
    y -= 1.4 * mm
    lienzo.setLineWidth(0.55 * mm)
    lienzo.line(x0, y, x1, y)
    y_zona = y - 0.55 * mm

    # 4) QR a la izquierda (zona de silencio sobre el margen), leyenda a la derecha.
    dibujo, lado_qr = _qr_termica(qr_url, QR_PUNTOS_INTERNA)
    silencio = 4 * QR_PUNTOS_INTERNA * PUNTO_TERMICA
    x_qr = x0 - silencio + 0.4 * mm
    visible = lado_qr - 2 * silencio
    # Centrado en la zona baja; la zona de silencio nunca pisa la línea.
    centrado = ETIQUETA_PAD_ABAJO + (y_zona - ETIQUETA_PAD_ABAJO - visible) / 2 - silencio
    y_qr = max(min(centrado, y_zona - 0.3 * mm - lado_qr), ETIQUETA_PAD_ABAJO - silencio + 0.4 * mm)
    renderPDF.draw(dibujo, lienzo, x_qr, y_qr)
    x_texto = x_qr + lado_qr - silencio + 2.2 * mm
    ancho_texto = x1 - x_texto
    pt = 6.6
    renglones = _envolver_mayus(leyenda, inter_negra, pt, 0.06, ancho_texto)
    while len(renglones) > 4 and pt > 5:
        pt -= 0.25
        renglones = _envolver_mayus(leyenda, inter_negra, pt, 0.06, ancho_texto)
    alto = len(renglones) * pt * 1.25
    y_centro = y_qr + silencio + visible / 2
    yt = y_centro + alto / 2 - pt
    for renglon in renglones:
        _texto_em(lienzo, x_texto, yt, renglon, inter_negra, pt, 0.06)
        yt -= pt * 1.25


def _copa(lienzo, cx, y_base, alto):
    """Copa de ISO 780 (símbolo 1, "frágil, manejar con cuidado") en silueta
    negra, sin grieta (Chema 2026-10-20); pie, tallo y cuerpo al alto dado."""
    ancho = alto * 0.62
    pie_alto, tallo_alto = alto * 0.07, alto * 0.30
    cuerpo_alto = alto - pie_alto - tallo_alto
    pie = lienzo.beginPath()
    pie.moveTo(cx - ancho * 0.42, y_base)
    pie.lineTo(cx + ancho * 0.42, y_base)
    pie.lineTo(cx + ancho * 0.12, y_base + pie_alto)
    pie.lineTo(cx - ancho * 0.12, y_base + pie_alto)
    pie.close()
    lienzo.drawPath(pie, stroke=0, fill=1)
    lienzo.rect(cx - ancho * 0.06, y_base + pie_alto, ancho * 0.12, tallo_alto + 0.3 * mm, stroke=0, fill=1)
    y0 = y_base + pie_alto + tallo_alto
    y1 = y_base + alto
    cuerpo = lienzo.beginPath()
    cuerpo.moveTo(cx - ancho / 2, y1)
    cuerpo.lineTo(cx + ancho / 2, y1)
    cuerpo.lineTo(cx + ancho / 2, y1 - cuerpo_alto * 0.38)
    cuerpo.curveTo(cx + ancho / 2, y0 + cuerpo_alto * 0.12, cx + ancho * 0.22, y0, cx, y0)
    cuerpo.curveTo(cx - ancho * 0.22, y0, cx - ancho / 2, y0 + cuerpo_alto * 0.12, cx - ancho / 2, y1 - cuerpo_alto * 0.38)
    cuerpo.close()
    lienzo.drawPath(cuerpo, stroke=0, fill=1)


def dibujar_fragil_51(lienzo, marca=MARCA_ETIQUETA):
    """Etiqueta FRÁGIL 51×51: marco, copa ISO 780, FRÁGIL en Clash, MANEJAR CON CUIDADO y la marca al pie."""
    clash, inter = _fuente("Clash-700"), _fuente("InterV-700")
    lienzo.setFillGray(0)
    lienzo.setStrokeGray(0)
    borde, ancho = _marco_manejo(lienzo)
    alto_copa = 15 * mm
    y_copa = ETIQUETA_LADO - borde - 2.6 * mm - alto_copa
    _copa(lienzo, ETIQUETA_LADO / 2, y_copa, alto_copa)
    texto = "FRÁGIL"
    pt = _encoger(texto, clash, 40, -0.02, ancho, 20)
    y = y_copa - 2.2 * mm - pt * 0.95
    _texto_em(lienzo, (ETIQUETA_LADO - _ancho_em(texto, clash, pt, -0.02)) / 2, y + pt * 0.12, texto, clash, pt, -0.02)
    sub = "MANEJAR CON CUIDADO"
    spt = _encoger(sub, inter, 7.5, 0.16, ancho, 5)
    y -= 2.2 * mm + spt
    _texto_em(lienzo, (ETIQUETA_LADO - _ancho_em(sub, inter, spt, 0.16)) / 2, y, sub, inter, spt, 0.16)
    _marca_al_pie(lienzo, borde, marca)


def _flecha(lienzo, x, y_base, alto, ancho):
    """Flecha negra hacia arriba: asta gruesa + punta triangular."""
    asta = ancho * 0.42
    lienzo.rect(x - asta / 2, y_base, asta, alto * 0.58, stroke=0, fill=1)
    punta = lienzo.beginPath()
    punta.moveTo(x - ancho / 2, y_base + alto * 0.55)
    punta.lineTo(x + ancho / 2, y_base + alto * 0.55)
    punta.lineTo(x, y_base + alto)
    punta.close()
    lienzo.drawPath(punta, stroke=0, fill=1)


def dibujar_orientacion_51(lienzo, marca=MARCA_ETIQUETA):
    """Etiqueta de orientación 51×51: ISO 780 símbolo 13 (dos flechas
    verticales sobre una barra), FLECHAS / HACIA ARRIBA en Clash y NO VOLTEAR
    LA CAJA (Chema 2026-10-20: la caja viaja con la orientación de las flechas)."""
    clash, inter = _fuente("Clash-700"), _fuente("InterV-700")
    lienzo.setFillGray(0)
    lienzo.setStrokeGray(0)
    borde, ancho = _marco_manejo(lienzo)
    alto_flecha, ancho_flecha, sep = 14.5 * mm, 9.5 * mm, 9 * mm
    y_tope = ETIQUETA_LADO - borde - 2.8 * mm
    barra_alto, barra_gap = 2 * mm, 1.4 * mm
    y_barra = y_tope - alto_flecha - barra_gap - barra_alto
    y_base = y_barra + barra_alto + barra_gap
    for x in (ETIQUETA_LADO / 2 - sep, ETIQUETA_LADO / 2 + sep):
        _flecha(lienzo, x, y_base, alto_flecha, ancho_flecha)
    lienzo.rect(ETIQUETA_LADO / 2 - sep - ancho_flecha / 2, y_barra, 2 * sep + ancho_flecha, barra_alto, stroke=0, fill=1)
    y = y_barra - 2.2 * mm
    for texto in ("FLECHAS", "HACIA ARRIBA"):
        pt = _encoger(texto, clash, 22, 0.03, ancho, 10)
        y -= pt * 0.95
        _texto_em(lienzo, (ETIQUETA_LADO - _ancho_em(texto, clash, pt, 0.03)) / 2, y + pt * 0.12, texto, clash, pt, 0.03)
        y -= 0.8 * mm
    sub = "NO VOLTEAR LA CAJA"
    spt = _encoger(sub, inter, 7, 0.16, ancho, 5)
    y -= 1.0 * mm + spt
    _texto_em(lienzo, (ETIQUETA_LADO - _ancho_em(sub, inter, spt, 0.16)) / 2, y, sub, inter, spt, 0.16)
    _marca_al_pie(lienzo, borde, marca)


def _lienzo_51(titulo):
    buffer = BytesIO()
    lienzo = canvas.Canvas(buffer, pagesize=(ETIQUETA_LADO, ETIQUETA_LADO), pageCompression=0)
    lienzo.setTitle(titulo)
    return buffer, lienzo


def _paginas_manejo(lienzo, fragil, orientacion):
    """Agrega las páginas de manejo que pida la caja, tantas como diga la configuración."""
    from apps.configuracion.services import etiquetas_manejo  # lazy por contrato

    n_fragil, n_orientacion = etiquetas_manejo()
    paginas = 0
    for _ in range(n_fragil if fragil else 0):
        dibujar_fragil_51(lienzo)
        lienzo.showPage()
        paginas += 1
    for _ in range(n_orientacion if orientacion else 0):
        dibujar_orientacion_51(lienzo)
        lienzo.showPage()
        paginas += 1
    return paginas


def generar_pdf_etiqueta_51(guia):
    """PDF 51×51 de la etiqueta INTERNA de la caja de esta guía más sus
    etiquetas de manejo (misma impresión: toda reimpresión saca todas).
    Regresa los bytes."""
    from apps.rastreo.services import obtener_o_crear_token_etiqueta, url_publica_etiqueta  # lazy por contrato

    pedido = guia.pedido
    paquete_n, paquete_m = _paquete_n_de_m(guia)
    obtener_o_crear_token_etiqueta(guia)  # el QR necesita el token vivo
    buffer, lienzo = _lienzo_51(f"Etiqueta interna {guia.numero or pedido.folio}")
    dibujar_interna_51(
        lienzo, folio=pedido.folio, paquete_n=paquete_n, paquete_m=paquete_m,
        carrier=guia.carrier, qr_url=url_publica_etiqueta(guia),
    )
    lienzo.showPage()
    fragil, arriba = marcas_de_manejo(guia)
    _paginas_manejo(lienzo, fragil, arriba)
    lienzo.save()
    return buffer.getvalue()


def generar_pdf_manejo(tipo, cantidad):
    """PDF con `cantidad` etiquetas de manejo sueltas de un tipo (Salida:
    botones "Imprimir etiquetas FRÁGIL" / "de orientación"). Regresa los bytes."""
    if tipo not in TIPOS_MANEJO:
        raise ValueError("Tipo de etiqueta de manejo desconocido: usa FRÁGIL u orientación.")
    try:
        cantidad = int(cantidad)
    except (TypeError, ValueError):
        cantidad = 0
    if not 1 <= cantidad <= 50:
        raise ValueError("Dime cuántas etiquetas imprimir, de 1 a 50.")
    buffer, lienzo = _lienzo_51("Etiquetas de manejo · Torre")
    dibujar = dibujar_fragil_51 if tipo == MANEJO_FRAGIL else dibujar_orientacion_51
    for _ in range(cantidad):
        dibujar(lienzo)
        lienzo.showPage()
    lienzo.save()
    return buffer.getvalue()


def generar_pdf_etiquetas(items):
    """PDF de una página 51×51 por item: {"tipo": "tarima", "nombre", "qr"} o
    {"tipo": "caja", marca, sku, producto, presentacion, lote, caducidad,
    barras, qr, danada}. Regresa los bytes."""
    buffer = BytesIO()
    lienzo = canvas.Canvas(buffer, pagesize=(ETIQUETA_LADO, ETIQUETA_LADO), pageCompression=0)
    lienzo.setTitle("Etiquetas de recepción · Torre")
    for item in items:
        if item["tipo"] == "tarima":
            dibujar_etiqueta_tarima(lienzo, item["nombre"], item["qr"], item.get("titulo", "TORRE · TARIMA"))
        else:
            dibujar_etiqueta_caja(lienzo, **{k: v for k, v in item.items() if k != "tipo"})
        lienzo.showPage()
    lienzo.save()
    return buffer.getvalue()


def paginas_pdf(pdf):
    """Cuántas páginas trae un PDF de reportlab sin comprimir (para `paginas`)."""
    return len(re.findall(rb"/Type\s*/Page\b", pdf))


def encolar_impresion(pdf, *, tipo, impresora, media, referencia, cliente, nombre, guia=None):
    """Manda un PDF a la térmica que corresponde según TORRE_MODO_IMPRESION:
    "relay" lo deja en TrabajoImpresion para el agente de bodega (regresa el
    trabajo); "local" corre `lp` aquí mismo con la cola de
    TORRE_IMPRESORA_ETIQUETAS (guías) o TORRE_IMPRESORA_ETIQUETAS_51
    (etiquetas) y regresa None. Falla con ValueError y qué hacer."""
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps

    if getattr(settings, "TORRE_MODO_IMPRESION", "local") == "relay":
        return TrabajoImpresion.objects.create(
            guia=guia, cliente=cliente, tipo=tipo, impresora=impresora, media=media, referencia=referencia[:60],
            paginas=max(paginas_pdf(pdf), 1), pdf=ContentFile(pdf, name=nombre),
        )
    variable = "TORRE_IMPRESORA_ETIQUETAS_51" if impresora == TrabajoImpresion.IMPRESORA_ETIQUETAS else "TORRE_IMPRESORA_ETIQUETAS"
    cola = os.environ.get(variable, "").strip()
    if not cola:
        raise ValueError(
            f"No hay impresora configurada para esto. Pon el nombre de su cola CUPS en torre/.env como {variable}."
        )
    with tempfile.NamedTemporaryFile(suffix=".pdf", prefix="torre-etiquetas-", delete=False) as archivo:
        archivo.write(pdf)
        ruta = archivo.name
    try:
        resultado = subprocess.run(["lp", "-d", cola, "-o", f"media={media}", ruta], capture_output=True, text=True, timeout=15, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"La impresora '{cola}' no respondió ({exc.__class__.__name__}). Revisa que esté encendida y conectada.") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(ruta)
    if resultado.returncode != 0:
        detalle = " ".join((resultado.stderr or resultado.stdout or "").split())[:160]
        raise ValueError(f"La impresora '{cola}' rechazó el trabajo: {detalle or 'sin detalle'}.")
    return None


def _item_caja(escaneo):
    """El item de etiqueta de una caja contada: datos del producto, el lote y
    su caducidad (la del catálogo, si no la de la línea) y el QR `P|barras|lote`."""
    from apps.catalogo.models import Lote  # lazy: modelo de otra app
    from apps.inventario.services import qr_producto  # lazy por contrato

    linea = escaneo.linea
    sku = linea.sku
    lote = (linea.lote_codigo or "").strip()
    caducidad = linea.fecha_caducidad
    if lote:
        conocido = Lote.objects.filter(sku=sku, codigo=lote).first()
        if conocido is not None and conocido.fecha_caducidad:
            caducidad = conocido.fecha_caducidad
    return {
        "tipo": "caja", "marca": sku.cliente.nombre, "sku": sku.codigo, "producto": sku.descripcion,
        "presentacion": sku.variante, "lote": lote, "caducidad": caducidad,
        "barras": sku.codigo_barras or sku.codigo, "qr": qr_producto(sku, lote), "danada": escaneo.danada,
    }


def _encolar_tanda(orden, tarima, escaneos, con_tarima, actor, desde, total):
    """Un trabajo de impresión con (opcional) la etiqueta de la tarima y las
    cajas dadas, en ese orden; las marca impresas y audita."""
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps
    from apps.inventario.services import marcar_impresas, qr_posicion  # lazy por contrato

    items = []
    if con_tarima:
        items.append({"tipo": "tarima", "nombre": tarima.codigo, "qr": qr_posicion(tarima)})
    items.extend(_item_caja(e) for e in escaneos)
    hasta = desde + len(escaneos) - 1
    pdf = generar_pdf_etiquetas(items)
    trabajo = encolar_impresion(
        pdf, tipo=TrabajoImpresion.TIPO_PRODUCTO, impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS,
        media=MEDIA_ETIQUETAS, referencia=f"{orden.folio} {tarima.codigo} etiquetas {desde}-{hasta}", cliente=orden.cliente,
        nombre=f"etiquetas-{orden.folio}-{tarima.codigo}-{desde}-{hasta}.pdf",
    )
    marcar_impresas(escaneos)
    registrar_evento(
        "asn", orden.folio, "etiquetas_impresas", actor=actor, cliente=orden.cliente,
        delta={"tarima": tarima.codigo, "desde": desde, "hasta": hasta, "total": total, "cajas": len(escaneos),
               "con_tarima": con_tarima, "trabajo": trabajo.pk if trabajo else None},
        motivo=f"{tarima.codigo}: etiquetas {desde} a {hasta} de {total}" + (" con la de la tarima" if con_tarima else "") + ".",
    )
    return {"tarima": tarima, "desde": desde, "hasta": hasta, "total": total, "con_tarima": con_tarima, "trabajo": trabajo}


def _imprimir_tarima(orden, tarima, escaneos, con_tarima, actor, total):
    """Las cajas dadas (ya en orden LIFO) en trabajos del tamaño de tanda
    configurado, la etiqueta de la tarima solo en el primero."""
    from apps.configuracion.services import tanda_etiquetas  # lazy por contrato

    tamano = tanda_etiquetas()
    tandas, desde = [], total - len(escaneos) + 1
    for i in range(0, len(escaneos), tamano):
        tandas.append(_encolar_tanda(orden, tarima, escaneos[i:i + tamano], con_tarima and i == 0, actor, desde + i, total))
    return tandas


def imprimir_etiquetas_orden(orden, actor):
    """Terminar conteo (Chema 2026-10-13): se imprimen solas TODAS las
    etiquetas, tarima por tarima en orden de primer uso: la etiqueta de la
    tarima y luego sus cajas en orden inverso al escaneo. Solo las cajas sin
    etiqueta (un segundo Terminar conteo imprime las nuevas) y la etiqueta de
    la tarima solo si ninguna caja suya la tenía. Regresa [tandas]."""
    from apps.inventario.services import escaneos_lifo, lleva_etiqueta, tarimas_de_orden  # lazy por contrato

    tandas = []
    for t in tarimas_de_orden(orden):
        lifo = [e for e in escaneos_lifo(orden, t["tarima"]) if lleva_etiqueta(e.linea)]  # sin lote no lleva etiqueta
        pendientes = [e for e in lifo if e.impresa_ts is None]
        if not pendientes:
            continue
        # LIFO: las últimas escaneadas van primero; las pendientes conservan su posición.
        con_tarima = len(pendientes) == len(lifo)
        tandas.extend(_imprimir_tarima(orden, t["tarima"], pendientes, con_tarima, actor, len(lifo)))
    return tandas


def reimprimir_tarima(orden, tarima, actor):
    """Todas las etiquetas de la tarima otra vez (la de la tarima y sus cajas
    en orden LIFO), por si el rollo se cortó o se perdieron."""
    from apps.inventario.services import escaneos_lifo, lleva_etiqueta  # lazy por contrato

    lifo = [e for e in escaneos_lifo(orden, tarima) if lleva_etiqueta(e.linea)]  # sin lote no lleva etiqueta
    if not lifo:
        raise ValueError(f"No hay cajas con lote contadas en {tarima.codigo}: nada que imprimir.")
    return _imprimir_tarima(orden, tarima, lifo, True, actor, len(lifo))


def imprimir_etiquetas_cajas(escaneos, actor):
    """Etiquetas de cajas sueltas al momento (las que faltaron en el
    reetiquetado): un trabajo con una página por caja. Las cajas sin lote no
    llevan etiqueta: se descartan; sin nada que imprimir regresa None."""
    from apps.inventario.services import lleva_etiqueta  # lazy por contrato

    escaneos = [e for e in escaneos if lleva_etiqueta(e.linea)]
    if not escaneos:
        return None
    orden, tarima = escaneos[0].orden, escaneos[0].tarima
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps
    from apps.inventario.services import marcar_impresas  # lazy por contrato

    pdf = generar_pdf_etiquetas([_item_caja(e) for e in escaneos])
    trabajo = encolar_impresion(
        pdf, tipo=TrabajoImpresion.TIPO_PRODUCTO, impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS,
        media=MEDIA_ETIQUETAS, referencia=f"{orden.folio} {tarima.codigo} faltaron {len(escaneos)}",
        cliente=orden.cliente, nombre=f"etiquetas-{orden.folio}-{tarima.codigo}-faltaron-{escaneos[0].pk}.pdf",
    )
    marcar_impresas(escaneos)
    registrar_evento(
        "asn", orden.folio, "etiquetas_impresas", actor=actor, cliente=orden.cliente,
        delta={"tarima": tarima.codigo, "cajas": len(escaneos), "faltaron": True, "trabajo": trabajo.pk if trabajo else None},
        motivo=f"{tarima.codigo}: {len(escaneos)} etiqueta(s) adicionales por cajas que faltaron.",
    )
    return trabajo


# La etiqueta del carrier baja del CDN de Envia: timeout corto, el caller ya
# es best-effort (un fallo se reintenta desde Salida, jamás revierte la guía).
TIMEOUT_ETIQUETA_CARRIER_S = 20


def _pdf_etiqueta(guia):
    """Bytes del PDF a imprimir para esta guía.

    ENVIA_MODO=full (guías reales): SIEMPRE la etiqueta OFICIAL del carrier
    (guia.etiqueta_url) — Estafeta/PuntoPost rutean con SUS códigos de barras;
    la etiqueta dibujada de Torre no sirve en su red. Si no baja, se falla con
    instrucciones: imprimir una etiqueta inservible es peor que reintentar.
    Modos mock/dev: la etiqueta dibujada (no existe PDF real que bajar).
    Decisión 2026-08-10 (tech lead + founder): solo la del carrier, sin
    segunda hoja.
    """
    # Etiqueta persistida (proveedores que la entregan en línea, ej. 99minutos
    # directo): es la oficial — se imprime tal cual, en cualquier modo.
    if getattr(guia, "etiqueta_pdf", None):
        try:
            with guia.etiqueta_pdf.open("rb") as archivo:
                pdf = archivo.read()
            if pdf.startswith(b"%PDF"):
                return pdf
        except (OSError, ValueError):
            pass  # archivo perdido en disco: se cae al camino por URL/dibujada
    if getattr(settings, "ENVIA_MODO", "cotizar") != "full":
        return generar_pdf_etiqueta(guia)
    url = (guia.etiqueta_url or "").strip()
    # https-only: la etiqueta lleva datos del comprador y Envia siempre sirve https.
    if not url.lower().startswith("https://"):
        raise ValueError(
            f"La guía {guia.numero} no trae etiqueta del carrier. "
            "Regenera la guía desde Salida o avisa a Mesa de Control."
        )
    try:
        respuesta = requests.get(url, timeout=TIMEOUT_ETIQUETA_CARRIER_S)
        respuesta.raise_for_status()
    except requests.RequestException as exc:
        raise ValueError(
            f"No pude bajar la etiqueta del carrier para {guia.numero} ({exc}). "
            "Reintenta la impresión desde Salida en un momento."
        )
    pdf = respuesta.content
    if not pdf.startswith(b"%PDF"):
        raise ValueError(
            f"Lo que regresó el carrier para {guia.numero} no es un PDF. "
            "Avisa a Mesa de Control para revisar la guía con Envia."
        )
    return pdf


def _encolar_etiqueta(guia, interna=False):
    """Modo relay: guarda el PDF en un TrabajoImpresion; el agente de bodega imprime.

    No toca `lp` ni necesita impresora en el servidor. El trabajo queda
    PENDIENTE y el agente lo recoge por la API en el siguiente poll (segundos).
    interna=True encola la etiqueta DIBUJADA de Torre (QR del repartidor) en
    vez de la oficial del carrier.
    """
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps

    pedido = guia.pedido
    pdf = generar_pdf_etiqueta(guia) if interna else _pdf_etiqueta(guia)
    sufijo = "-interna" if interna else ""
    trabajo = TrabajoImpresion.objects.create(
        guia=guia, cliente=pedido.cliente, referencia=pedido.folio, paginas=max(paginas_pdf(pdf), 1),
        pdf=ContentFile(pdf, name=f"etiqueta-{pedido.folio}-g{guia.pk}{sufijo}.pdf"),
    )
    registrar_evento(
        "etiqueta", guia.numero, "encolada_impresion",
        delta={"folio": pedido.folio, "trabajo": trabajo.pk, "interna": interna},
    )
    cual = "interna " if interna else ""
    return f"Etiqueta {cual}de {pedido.folio} en cola de impresión — sale en segundos en la bodega."


def _imprimir_interna_51(guia):
    """La etiqueta interna 51×51 (más sus etiquetas de manejo) de un pedido
    foráneo va a la térmica de etiquetas: encolada para el agente en relay,
    `lp` con TORRE_IMPRESORA_ETIQUETAS_51 en local (encolar_impresion decide).
    Regresa el mensaje de éxito; una falla es ValueError con qué hacer."""
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps

    pedido = guia.pedido
    pdf = generar_pdf_etiqueta_51(guia)
    trabajo = encolar_impresion(
        pdf, tipo=TrabajoImpresion.TIPO_INTERNA, impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS,
        media=MEDIA_ETIQUETAS, referencia=pedido.folio, cliente=pedido.cliente,
        nombre=f"etiqueta-{pedido.folio}-g{guia.pk}-interna51.pdf", guia=guia,
    )
    registrar_evento(
        "etiqueta", guia.numero, "encolada_impresion" if trabajo is not None else "impresa_en_bodega",
        delta={"folio": pedido.folio, "interna": True, "formato": "51", "paginas": max(paginas_pdf(pdf), 1),
               "trabajo": trabajo.pk if trabajo is not None else None},
    )
    if trabajo is not None:
        return f"Etiqueta interna de {pedido.folio} en cola de impresión — sale en segundos en la bodega."
    return f"Etiqueta interna de {pedido.folio} enviada a la impresora de etiquetas."


def imprimir_etiqueta(guia, interna=False):
    """Imprime la etiqueta según TORRE_MODO_IMPRESION. Regresa el mensaje de éxito.

    "local" (default): genera el PDF y lo manda a la térmica con `lp` — la cola
    CUPS viene de TORRE_IMPRESORA_ETIQUETAS (torre/.env). "relay": la encola
    para el agente de bodega. Cualquier falla se traduce a ValueError con qué
    pasó y qué hacer, para el flash del piso.

    interna=True imprime la etiqueta DIBUJADA de Torre bajo demanda: la de
    51×51 en la térmica de etiquetas para pedidos foráneos (_imprimir_interna_51),
    la de 10×15 en la de guías para pedidos locales. El default sigue la
    decisión 2026-08-10: la etiqueta del carrier — sus códigos son los que
    rutean en su red.
    """
    if interna and not guia.pedido.es_local:
        return _imprimir_interna_51(guia)
    if getattr(settings, "TORRE_MODO_IMPRESION", "local") == "relay":
        return _encolar_etiqueta(guia, interna=interna)
    cola = os.environ.get("TORRE_IMPRESORA_ETIQUETAS", "").strip()
    if not cola:
        raise ValueError(
            "No hay impresora configurada. Conecta la térmica, agrégala en el sistema "
            "y pon su nombre de cola en torre/.env como TORRE_IMPRESORA_ETIQUETAS — "
            "mientras, usa el botón Imprimir del navegador."
        )
    pedido = guia.pedido
    pdf = generar_pdf_etiqueta(guia) if interna else _pdf_etiqueta(guia)
    with tempfile.NamedTemporaryFile(
        suffix=".pdf", prefix=f"etiqueta-{pedido.folio}-", delete=False
    ) as archivo:
        archivo.write(pdf)
        ruta = archivo.name
    try:
        try:
            resultado = subprocess.run(
                ["lp", "-d", cola, "-o", MEDIA_LP, ruta],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except FileNotFoundError:
            raise ValueError(
                "No encontré el comando lp (CUPS) en el servidor. "
                "Usa el botón Imprimir del navegador y avisa a Mesa de Control."
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                f"La impresora '{cola}' no respondió en 15 segundos. "
                "Revisa que esté encendida y conectada, y reintenta."
            )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(ruta)

    if resultado.returncode != 0:
        detalle = " ".join((resultado.stderr or resultado.stdout or "").split())
        if len(detalle) > 160:
            detalle = detalle[:160].rstrip() + "…"
        raise ValueError(
            f"La impresora '{cola}' rechazó el trabajo: "
            f"{detalle or 'sin detalle del sistema de impresión'}. "
            "Revisa que la cola exista en el sistema y que la térmica tenga etiquetas."
        )

    registrar_evento(
        "etiqueta", guia.numero, "impresa_en_bodega",
        delta={"folio": pedido.folio, "cola": cola},
    )
    return f"Etiqueta de {pedido.folio} enviada a la impresora ({cola})."


# ─────────────────────────────────────────────────────────────────────────────
# Mesa → Bodega → Posiciones (Chema 2026-10-13): etiqueta de la posición y
# etiquetas de las cajas que hay en ella
# ─────────────────────────────────────────────────────────────────────────────


def _item_posicion(ubicacion):
    from apps.inventario.services import es_tarima, qr_posicion  # lazy por contrato

    return {"tipo": "tarima", "nombre": ubicacion.codigo, "qr": qr_posicion(ubicacion),
            "titulo": "TORRE · TARIMA" if es_tarima(ubicacion) else "TORRE · POSICIÓN"}


def imprimir_etiqueta_posicion(ubicacion, actor):
    """La etiqueta QR de una posición o tarima (`U|nombre`) a la térmica de
    etiquetas. Regresa el trabajo (relay) o None (local)."""
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps

    pdf = generar_pdf_etiquetas([_item_posicion(ubicacion)])
    trabajo = encolar_impresion(
        pdf, tipo=TrabajoImpresion.TIPO_TARIMA, impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS, media=MEDIA_ETIQUETAS,
        referencia=f"Posición {ubicacion.codigo}", cliente=None, nombre=f"posicion-{ubicacion.codigo}.pdf",
    )
    registrar_evento(
        "ubicacion", ubicacion.codigo, "etiqueta_posicion_impresa", actor=actor,
        delta={"trabajo": trabajo.pk if trabajo else None}, motivo=f"Etiqueta de la posición {ubicacion.codigo}.",
    )
    return trabajo


def _item_caja_de_saldo(sku, lote):
    """El item de etiqueta de una caja que ya está ubicada (sin escaneo): datos
    del producto, el lote y su caducidad del catálogo, QR `P|barras|lote`."""
    from apps.inventario.services import qr_producto  # lazy por contrato

    codigo_lote = lote.codigo if lote is not None else ""
    return {
        "tipo": "caja", "marca": sku.cliente.nombre, "sku": sku.codigo, "producto": sku.descripcion,
        "presentacion": sku.variante, "lote": codigo_lote, "caducidad": lote.fecha_caducidad if lote is not None else None,
        "barras": sku.codigo_barras or sku.codigo, "qr": qr_producto(sku, codigo_lote), "danada": False,
    }


def imprimir_etiquetas_posicion(ubicacion, filas, actor):
    """Etiquetas de caja (51×51) de lo que hay en una posición: `filas` =
    [(sku, lote | None, cantidad)]. Salen en trabajos del tamaño de tanda con
    referencia "<posición> etiquetas a-b"; evento `etiquetas_impresas` en la
    posición. Regresa [trabajos]."""
    from apps.configuracion.services import tanda_etiquetas  # lazy por contrato
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps

    filas = [(sku, lote, int(cantidad)) for sku, lote, cantidad in filas if int(cantidad) > 0]
    items = [_item_caja_de_saldo(sku, lote) for sku, lote, cantidad in filas for _ in range(cantidad)]
    if not items:
        raise ValueError("Captura cuántas etiquetas quieres de cada producto.")
    clientes = {sku.cliente for sku, _lote, _cantidad in filas}
    cliente = clientes.pop() if len(clientes) == 1 else None
    tamano = tanda_etiquetas()
    trabajos = []
    for i in range(0, len(items), tamano):
        tanda = items[i:i + tamano]
        desde, hasta = i + 1, i + len(tanda)
        trabajos.append(encolar_impresion(
            generar_pdf_etiquetas(tanda), tipo=TrabajoImpresion.TIPO_PRODUCTO, impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS,
            media=MEDIA_ETIQUETAS, referencia=f"{ubicacion.codigo} etiquetas {desde}-{hasta}", cliente=cliente,
            nombre=f"etiquetas-{ubicacion.codigo}-{desde}-{hasta}.pdf",
        ))
    registrar_evento(
        "ubicacion", ubicacion.codigo, "etiquetas_impresas", actor=actor,
        delta={"cajas": len(items), "trabajos": [t.pk for t in trabajos if t],
               "por_producto": [{"sku": sku.codigo, "lote": lote.codigo if lote else None, "cajas": cantidad} for sku, lote, cantidad in filas]},
        motivo=f"{len(items)} etiqueta(s) de caja de lo que hay en {ubicacion.codigo}.",
    )
    return trabajos
