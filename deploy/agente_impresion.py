#!/usr/bin/env python3
"""Agente de impresión de Torre — corre en la bodega, junto a la térmica.

Torre vive en un VPS; la Aiyin BY-480BT está por USB en una máquina de la
bodega (mini PC o la Mac vieja). Este script hace polling autenticado a la API
de Torre (/api/impresion/), baja cada etiqueta PDF pendiente, la imprime con
`lp` (CUPS local) y confirma el resultado. Python 3 puro, solo stdlib: no
necesita pip, virtualenv ni Django.

Configuración por variables de entorno:
  TORRE_URL              https://torre.ejemplo.mx  (sin diagonal final)
  TORRE_TOKEN_IMPRESION  el MISMO token configurado en el .env del VPS
  IMPRESORA              nombre de la cola CUPS local (ej. Aiyin_BY480BT; `lpstat -p`)
  INTERVALO              segundos entre polls (default 3)

Probar a mano:
  TORRE_URL=https://torre.ejemplo.mx TORRE_TOKEN_IMPRESION=xxx \
  IMPRESORA=Aiyin_BY480BT python3 agente_impresion.py

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
  Environment=IMPRESORA=Aiyin_BY480BT
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


def _imprimir_pdf(ruta, impresora):
    """Manda el PDF a la cola CUPS local en media 10×15. Regresa (ok, detalle)."""
    resultado = subprocess.run(
        ["lp", "-d", impresora, "-o", "media=Custom.100x150mm", ruta],
        capture_output=True, text=True, timeout=30,
    )
    if resultado.returncode != 0:
        detalle = " ".join((resultado.stderr or resultado.stdout or "lp falló").split())
        return False, detalle[:300]
    return True, ""


def procesar_trabajo(trabajo, base, token, impresora):
    """Baja el PDF de UN trabajo, lo imprime y reporta el resultado a Torre.

    `trabajo` es un dict de /api/impresion/pendientes/ ({id, folio, guia,
    url_pdf}). Regresa True si la etiqueta salió. Si la descarga o `lp`
    truenan, el error viaja en el POST de resultado y Torre decide reintentos.
    """
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
            ok, detalle = _imprimir_pdf(ruta, impresora)
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
    intervalo = float(os.environ.get("INTERVALO", "3"))
    if not (base and token and impresora):
        _log("Faltan TORRE_URL, TORRE_TOKEN_IMPRESION o IMPRESORA. Revisa el entorno.")
        return 1
    _log(f"Agente arriba: {base} → impresora {impresora} (poll cada {intervalo:g} s)")
    espera = intervalo
    while True:
        try:
            pendientes = json.loads(_peticion(f"{base}/api/impresion/pendientes/", token))
            for trabajo in pendientes:
                procesar_trabajo(trabajo, base, token, impresora)
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
