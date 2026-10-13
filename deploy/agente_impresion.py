#!/usr/bin/env python3
"""Agente de impresión de Torre — corre en la bodega, junto a la térmica.

Torre vive en un VPS; las térmicas están por USB en una máquina de la bodega
(mini PC o laptop). Este script hace polling autenticado a la API de Torre
(/api/impresion/), baja cada PDF pendiente, lo imprime con `lp` (CUPS local)
en la cola que el trabajo pide y confirma el resultado. Python 3 puro, solo
stdlib: no necesita pip, virtualenv ni Django.

Dos impresoras (2026-10-13): la de guías (10×15 cm, `IMPRESORA`) y la de
etiquetas de recepción (51×51 mm, `IMPRESORA_ETIQUETAS`). Cada trabajo trae
`impresora` ("envios" | "etiquetas") y `media` (tamaño de papel para lp). Un
trabajo de etiquetas sin `IMPRESORA_ETIQUETAS` configurada se queda pendiente
(se avisa en el log) y sale en cuanto se configure; las guías no se detienen.

Configuración por variables de entorno:
  TORRE_URL              https://torre.ejemplo.mx  (sin diagonal final)
  TORRE_TOKEN_IMPRESION  el MISMO token configurado en el .env del VPS
  IMPRESORA              cola CUPS de la térmica de guías (ej. AiYin_BY-480BT; `lpstat -p`)
  IMPRESORA_ETIQUETAS    cola CUPS de la térmica de etiquetas 51×51 (ej. Etiquetas51); opcional
  INTERVALO              segundos entre polls (default 3)

Probar a mano:
  TORRE_URL=https://torre.ejemplo.mx TORRE_TOKEN_IMPRESION=xxx \
  IMPRESORA=AiYin_BY-480BT IMPRESORA_ETIQUETAS=Etiquetas51 python3 agente_impresion.py

── Instalación en Mac (launchd) ─────────────────────────────────────────────
Guardar como ~/Library/LaunchAgents/mx.torre.impresion.plist (ajustar rutas y
token) y cargar con: launchctl load ~/Library/LaunchAgents/mx.torre.impresion.plist

  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>mx.torre.impresion</string>
    <key>ProgramArguments</key><array>
      <string>/usr/bin/python3</string>
      <string>/Users/bodega/agente_impresion.py</string>
    </array>
    <key>EnvironmentVariables</key><dict>
      <key>TORRE_URL</key><string>https://torre.ejemplo.mx</string>
      <key>TORRE_TOKEN_IMPRESION</key><string>CAMBIAME</string>
      <key>IMPRESORA</key><string>Aiyin_BY480BT</string>
      <key>IMPRESORA_ETIQUETAS</key><string>Etiquetas51</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/torre-impresion.log</string>
    <key>StandardErrorPath</key><string>/tmp/torre-impresion.log</string>
  </dict></plist>

── Instalación en Linux (systemd) ───────────────────────────────────────────
Guardar como /etc/systemd/system/torre-impresion.service (ajustar rutas y
token) y activar con: sudo systemctl enable --now torre-impresion

  [Unit]
  Description=Agente de impresión de Torre (térmica de bodega)
  After=network-online.target cups.service

  [Service]
  Environment=TORRE_URL=https://torre.ejemplo.mx
  Environment=TORRE_TOKEN_IMPRESION=CAMBIAME
  Environment=IMPRESORA=AiYin_BY-480BT
  Environment=IMPRESORA_ETIQUETAS=Etiquetas51
  ExecStart=/usr/bin/python3 /opt/torre/agente_impresion.py
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ESPERA_MAXIMA = 60  # tope del backoff cuando Torre no contesta (segundos)
MEDIA_GUIAS = "Custom.100x150mm"  # papel de la térmica de guías (trabajos sin `media`)


def _log(mensaje):
    """Log a stdout con timestamp; launchd/systemd lo mandan al archivo de log."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {mensaje}", flush=True)


def _peticion(url, token, datos=None):
    """GET (datos=None) o POST JSON autenticado con el token Bearer. Regresa bytes."""
    solicitud = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    if datos is not None:
        solicitud.data = json.dumps(datos).encode()
        solicitud.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(solicitud, timeout=30) as respuesta:
        return respuesta.read()


def _imprimir_pdf(ruta, impresora, media=MEDIA_GUIAS):
    """Manda el PDF a la cola CUPS local con el papel del trabajo. Regresa (ok, detalle)."""
    resultado = subprocess.run(
        ["lp", "-d", impresora, "-o", f"media={media}", ruta],
        capture_output=True, text=True, timeout=30,
    )
    if resultado.returncode != 0:
        detalle = " ".join((resultado.stderr or resultado.stdout or "lp falló").split())
        return False, detalle[:300]
    return True, ""


def _cola_para(trabajo, impresora, impresora_etiquetas):
    """Cola CUPS que pide el trabajo: la de etiquetas 51×51 si viene
    impresora=etiquetas, si no la de guías. Vacío = no configurada."""
    if trabajo.get("impresora") == "etiquetas":
        return impresora_etiquetas or ""
    return impresora


def procesar_trabajo(trabajo, base, token, impresora, impresora_etiquetas=""):
    """Baja el PDF de UN trabajo, lo imprime en la cola que pide y reporta el
    resultado a Torre.

    `trabajo` es un dict de /api/impresion/pendientes/ ({id, folio, guia,
    impresora, media, url_pdf}). Regresa True si salió; None si el trabajo
    pide una impresora que no está configurada (se queda pendiente en Torre,
    sin consumir reintentos). Si la descarga o `lp` truenan, el error viaja
    en el POST de resultado y Torre decide reintentos.
    """
    cola = _cola_para(trabajo, impresora, impresora_etiquetas)
    if not cola:
        _log(f"{trabajo.get('folio', '?')}: pide la impresora de etiquetas y IMPRESORA_ETIQUETAS no está configurada; queda pendiente")
        return None
    media = trabajo.get("media") or MEDIA_GUIAS
    url_pdf = trabajo["url_pdf"]
    if url_pdf.startswith("/"):  # por si el server regresara ruta relativa
        url_pdf = base + url_pdf
    ruta = None
    try:
        try:
            pdf = _peticion(url_pdf, token)
            with tempfile.NamedTemporaryFile(
                suffix=".pdf", prefix="torre-etiqueta-", delete=False
            ) as archivo:
                archivo.write(pdf)
                ruta = archivo.name
            ok, detalle = _imprimir_pdf(ruta, cola, media=media)
        except Exception as error:  # descarga o lp reventaron: se reporta, no se muere
            ok, detalle = False, str(error)[:300]
        _peticion(
            f"{base}/api/impresion/{trabajo['id']}/resultado/", token,
            {"ok": True} if ok else {"ok": False, "error": detalle},
        )
        _log(f"{trabajo['folio']} ({trabajo['guia']}): {'impresa' if ok else 'ERROR ' + detalle}")
        return ok
    finally:
        if ruta:  # el tempfile jamás se queda tirado
            try:
                os.unlink(ruta)
            except OSError:
                pass


def main():
    base = os.environ.get("TORRE_URL", "").rstrip("/")
    token = os.environ.get("TORRE_TOKEN_IMPRESION", "")
    impresora = os.environ.get("IMPRESORA", "")
    impresora_etiquetas = os.environ.get("IMPRESORA_ETIQUETAS", "")
    intervalo = float(os.environ.get("INTERVALO", "3"))
    if not (base and token and impresora):
        _log("Faltan TORRE_URL, TORRE_TOKEN_IMPRESION o IMPRESORA. Revisa el entorno.")
        return 1
    _log(
        f"Agente arriba: {base} → guías en {impresora}, etiquetas en "
        f"{impresora_etiquetas or '(sin configurar: IMPRESORA_ETIQUETAS)'} (poll cada {intervalo:g} s)"
    )
    espera = intervalo
    while True:
        try:
            pendientes = json.loads(_peticion(f"{base}/api/impresion/pendientes/", token))
            for trabajo in pendientes:
                procesar_trabajo(trabajo, base, token, impresora, impresora_etiquetas)
            espera = intervalo  # respuesta sana → se reinicia el backoff
        except KeyboardInterrupt:
            raise
        except Exception as error:  # red caída o Torre abajo: backoff y seguimos
            _log(f"Sin conexión con Torre ({error}); reintento en {espera:g} s")
            espera = min(espera * 2, ESPERA_MAXIMA)
        time.sleep(espera)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("Agente detenido (Ctrl-C).")
        sys.exit(0)
