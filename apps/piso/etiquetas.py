"""Etiqueta de envío en PDF para la térmica de bodega (Aiyin BY-480BT vía CUPS).

`generar_pdf_etiqueta` dibuja la misma etiqueta 10×15 de piso/etiqueta.html
pero en PDF de alto contraste para térmica 203 dpi: Code128 del número de guía
y QR a la página pública del repartidor. `imprimir_etiqueta` la manda directo a
la cola CUPS del servidor con `lp` — el packer imprime desde la tablet con un
botón, sin pelearse con diálogos de impresión.

Con `TORRE_MODO_IMPRESION=relay` (Torre en un VPS, la térmica en bodega) la
etiqueta no va a `lp`: se encola en `envios.TrabajoImpresion` y el agente de
bodega (deploy/agente_impresion.py) la baja, la imprime y confirma.
"""
import contextlib
import os
import subprocess
import tempfile
from decimal import Decimal
from io import BytesIO

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
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
    """Peso del bulto: el del paquete; legacy → peso real (o esperado) del pedido."""
    if guia.paquete is not None:
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


def generar_pdf_etiqueta(guia):
    """PDF de UNA página 100×150 mm listo para la térmica. Regresa los bytes.

    Blanco y negro de alto contraste; mismo contenido que piso/etiqueta.html:
    marca + folio, carrier + Code128 de la guía, destinatario con CP enorme,
    referencias destacadas y QR a la página del repartidor. JAMÁS imprime
    valor declarado ni teléfono del comprador.
    """
    from apps.rastreo.services import (  # lazy por contrato
        obtener_o_crear_token_etiqueta,
        referencias_direccion,
        url_publica_etiqueta,
    )

    pedido = guia.pedido
    direccion = pedido.direccion or {}
    branding = pedido.cliente.branding or {}
    marca = branding.get("nombre_publico") or pedido.cliente.nombre
    paquete_n, paquete_m = _paquete_n_de_m(guia)
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

    # 1) Cabecera: marca del cliente + folio | Paquete N de M.
    y -= 13
    etiqueta_paquete = f"Paquete {paquete_n} de {paquete_m}"
    ancho_paquete = stringWidth(etiqueta_paquete, NEGRITA, 10)
    lienzo.setFont(NEGRITA, 13)
    lienzo.drawString(
        MARGEN, y, _recortar(str(marca).upper(), NEGRITA, 13, ANCHO_UTIL - ancho_paquete - 3 * mm)
    )
    lienzo.setFont(NEGRITA, 10)
    lienzo.drawRightString(ANCHO - MARGEN, y, etiqueta_paquete)
    y -= 11
    lienzo.setFont(MONO, 9)
    lienzo.drawString(MARGEN, y, pedido.folio)
    y = _separador(lienzo, y - 4)

    # 2) Carrier + número de guía en Code128 (o texto grande si es muy largo).
    carrier = str(guia.carrier or "").upper()
    if guia.servicio:
        carrier += f" · {str(guia.servicio).upper()}"
    y -= 10
    lienzo.setFont(NEGRITA, 10)
    lienzo.drawString(MARGEN, y, _recortar(carrier, NEGRITA, 10, ANCHO_UTIL))
    numero = (guia.numero or "").strip()
    barras = _code128_ajustado(numero)
    if barras is not None:
        y -= 4 + BARRA_ALTO
        barras.drawOn(lienzo, MARGEN + (ANCHO_UTIL - barras.width) / 2, y)
        y -= 11
        lienzo.setFont(MONO_NEGRITA, 10)
        lienzo.drawCentredString(ANCHO / 2, y, numero)
    else:
        texto = numero or "SIN NÚMERO"
        tamano = 16
        while tamano > 8 and stringWidth(texto, MONO_NEGRITA, tamano) > ANCHO_UTIL:
            tamano -= 1
        y -= 6 + tamano
        lienzo.setFont(MONO_NEGRITA, tamano)
        lienzo.drawCentredString(ANCHO / 2, y, _recortar(texto, MONO_NEGRITA, tamano, ANCHO_UTIL))
    y = _separador(lienzo, y - 5)

    # 3) Destinatario: nombre + dirección del JSON + CP enorme (≥28 pt).
    y -= 8
    lienzo.setFont(NEGRITA, 7)
    lienzo.drawString(MARGEN, y, "DESTINATARIO")
    y -= 13
    lienzo.setFont(NEGRITA, 12)
    lienzo.drawString(MARGEN, y, _recortar(pedido.comprador_nombre or "—", NEGRITA, 12, ANCHO_UTIL))
    lineas_direccion = []
    if direccion.get("address1"):
        lineas_direccion += _envolver(direccion["address1"], NORMAL, 9.5, ANCHO_UTIL, max_lineas=2)
    if direccion.get("address2"):
        lineas_direccion += _envolver(direccion["address2"], NORMAL, 9.5, ANCHO_UTIL, max_lineas=1)
    ciudad = ", ".join(
        parte
        for parte in (
            str(direccion.get("city") or "").strip(),
            str(direccion.get("province") or "").strip(),
            str(direccion.get("zip") or "").strip(),
        )
        if parte
    )
    if ciudad:
        lineas_direccion += _envolver(ciudad, NORMAL, 9.5, ANCHO_UTIL, max_lineas=1)
    lienzo.setFont(NORMAL, 9.5)
    for linea in lineas_direccion:
        y -= 11
        lienzo.drawString(MARGEN, y, linea)
    y -= 26
    lienzo.setFont(MONO_NEGRITA, 30)
    lienzo.drawString(MARGEN, y, _recortar(f"CP {cp or '—'}", MONO_NEGRITA, 30, ANCHO_UTIL))
    y = _separador(lienzo, y - 5)

    # 4) Referencias destacadas: banda negra con texto blanco (o manda al QR).
    if referencias:
        lineas_refs = []
        for referencia in referencias:
            lineas_refs += _envolver(referencia, NEGRITA, 10, ANCHO_UTIL - 4 * mm, max_lineas=2)
        lineas_refs = lineas_refs[:3]
    else:
        lineas_refs = ["Sin referencias — usa el QR"]
    alto_banda = 12 + 12 * len(lineas_refs)
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
    y_pie = MARGEN + 14
    y_qr = max(y - QR_LADO, y_pie)
    _dibujar_qr(lienzo, qr_url, MARGEN, y_qr)
    x_leyenda = MARGEN + QR_LADO + 4 * mm
    ancho_leyenda = ANCHO - MARGEN - x_leyenda
    lienzo.setFont(NEGRITA, 11)
    y_texto = y_qr + QR_LADO - 14
    for linea in _envolver(
        "REPARTIDOR: escanea — ubicación exacta + ayuda", NEGRITA, 11, ancho_leyenda, max_lineas=4
    ):
        lienzo.drawString(x_leyenda, y_texto, linea)
        y_texto -= 13
    lienzo.setFont(MONO, 6.5)
    y_texto -= 2
    for linea in _envolver_crudo(qr_url, MONO, 6.5, ancho_leyenda, max_lineas=3):
        lienzo.drawString(x_leyenda, y_texto, linea)
        y_texto -= 8

    # 6) Pie: peso + manejo + folio chico. JAMÁS valor declarado ni teléfono.
    lienzo.setLineWidth(1.5)
    lienzo.line(MARGEN, MARGEN + 12, ANCHO - MARGEN, MARGEN + 12)
    lienzo.setFont(NEGRITA, 9)
    if peso_kg is not None:
        lienzo.drawString(MARGEN, MARGEN + 3, f"{peso_kg} kg")
    lienzo.drawCentredString(ANCHO / 2, MARGEN + 3, "FRÁGIL · ESTE LADO ARRIBA")
    lienzo.setFont(MONO, 8)
    lienzo.drawRightString(ANCHO - MARGEN, MARGEN + 3, pedido.folio)

    lienzo.showPage()
    lienzo.save()
    return buffer.getvalue()


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


def _encolar_etiqueta(guia):
    """Modo relay: guarda el PDF en un TrabajoImpresion; el agente de bodega imprime.

    No toca `lp` ni necesita impresora en el servidor. El trabajo queda
    PENDIENTE y el agente lo recoge por la API en el siguiente poll (segundos).
    """
    from apps.envios.models import TrabajoImpresion  # lazy: evita ciclos entre apps

    pedido = guia.pedido
    pdf = _pdf_etiqueta(guia)
    trabajo = TrabajoImpresion.objects.create(
        guia=guia,
        pdf=ContentFile(pdf, name=f"etiqueta-{pedido.folio}-g{guia.pk}.pdf"),
    )
    registrar_evento(
        "etiqueta", guia.numero, "encolada_impresion",
        delta={"folio": pedido.folio, "trabajo": trabajo.pk},
    )
    return f"Etiqueta de {pedido.folio} en cola de impresión — sale en segundos en la bodega."


def imprimir_etiqueta(guia):
    """Imprime la etiqueta según TORRE_MODO_IMPRESION. Regresa el mensaje de éxito.

    "local" (default): genera el PDF y lo manda a la térmica con `lp` — la cola
    CUPS viene de TORRE_IMPRESORA_ETIQUETAS (torre/.env). "relay": la encola
    para el agente de bodega. Cualquier falla se traduce a ValueError con qué
    pasó y qué hacer, para el flash del piso.
    """
    if getattr(settings, "TORRE_MODO_IMPRESION", "local") == "relay":
        return _encolar_etiqueta(guia)
    cola = os.environ.get("TORRE_IMPRESORA_ETIQUETAS", "").strip()
    if not cola:
        raise ValueError(
            "No hay impresora configurada. Conecta la térmica, agrégala en el sistema "
            "y pon su nombre de cola en torre/.env como TORRE_IMPRESORA_ETIQUETAS — "
            "mientras, usa el botón Imprimir del navegador."
        )
    pedido = guia.pedido
    pdf = _pdf_etiqueta(guia)
    with tempfile.NamedTemporaryFile(
        suffix=".pdf", prefix=f"etiqueta-{pedido.folio}-", delete=False
    ) as archivo:
        archivo.write(pdf)
        ruta = archivo.name
    try:
        try:
            resultado = subprocess.run(
                ["lp", "-d", cola, "-o", MEDIA_LP, ruta],
                capture_output=True, text=True, timeout=15,
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
