"""Catálogo de localidades por CP: geocodes de envia, con caché en LocalidadCP.

iMile valida el par CP↔ciudad contra su catálogo y el conector de envia no lo
traduce: "The Consignee Zip Code [72830] does not match city [Puebla]" y
"consignee city [CHETUMAL] not exist" (PED-00030 y PED-00034, 2026-09-21).
Shopify trae la ciudad como la tecleó el comprador. Aquí se resuelve la del
catálogo de envia para ese CP (GET {ENVIA_GEOCODES_BASE}/zipcode/MX/<cp>, con
el mismo Bearer que el resto de envia) y se guarda en LocalidadCP: una
consulta por CP. Sin llave, con ENVIA_MODO off, o sin respuesta útil (red,
error HTTP, JSON raro) se regresa None y el destino conserva lo de Shopify: el
catálogo mejora, nunca bloquea. Los fallos quedan auditados como
localidad_cp_fallo para poder verlos.
"""
import re

import requests
from django.conf import settings

from apps.core.services import registrar_evento

from .models import LocalidadCP

_RE_CP = re.compile(r"^\d{5}$")
_VACIO = {"localidad": "", "municipio": "", "estado": "", "colonias": []}


def localidad_por_cp(cp):
    """LocalidadCP del CP, de la caché o de geocodes; None si no se pudo resolver."""
    cp = str(cp or "").strip()
    if not _RE_CP.match(cp):
        return None
    fila = LocalidadCP.objects.filter(cp=cp).first()
    if fila is not None:
        return fila
    datos = _consultar_geocodes(cp)
    if datos is None:
        return None
    fila, _ = LocalidadCP.objects.update_or_create(cp=cp, defaults=datos)
    return fila


def _consultar_geocodes(cp):
    """{localidad, municipio, estado, colonias} del CP según envia.

    CP que el catálogo no conoce → vacío (se cachea para no insistir); sin
    llave, modo off o sin respuesta útil → None (no se cachea nada).
    """
    if not getattr(settings, "ENVIA_API_KEY", "") or getattr(settings, "ENVIA_MODO", "off") == "off":
        return None
    url = f"{settings.ENVIA_GEOCODES_BASE.rstrip('/')}/zipcode/MX/{cp}"
    cabeceras = {"Authorization": f"Bearer {settings.ENVIA_API_KEY}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=cabeceras, timeout=10)
        if resp.status_code == 404:
            return dict(_VACIO)
        resp.raise_for_status()
        cuerpo = resp.json()
    except (requests.RequestException, ValueError) as exc:
        registrar_evento(
            "cp", cp, "localidad_cp_fallo",
            motivo=f"geocodes de envia sin respuesta útil: {str(exc)[:200]}",
        )
        return None
    filas = cuerpo if isinstance(cuerpo, list) else (cuerpo.get("data") if isinstance(cuerpo, dict) else None)
    if not filas or not isinstance(filas[0], dict):
        return dict(_VACIO)
    fila = filas[0]
    estado = ((fila.get("state") or {}).get("code") or {}).get("2digit") or ""
    regiones = fila.get("regions") or {}
    return {
        "localidad": str(fila.get("locality") or "")[:120],
        "municipio": str(regiones.get("region_2") or "")[:120],
        "estado": str(estado)[:2],
        "colonias": [str(s) for s in (fila.get("suburbs") or []) if s][:200],
    }
