"""Servicios del rastreo público brandeado."""
import os
import secrets
from urllib.parse import urlencode

from .models import AccesoEtiqueta, AccesoRastreo


def _base_publica():
    return os.environ.get("BASE_URL_PUBLICA", "http://127.0.0.1:8380").rstrip("/")


def _nuevo_token():
    while True:
        token = secrets.token_urlsafe(9)[:12]
        if not AccesoRastreo.objects.filter(token=token).exists():
            return token


def obtener_o_crear_token(pedido):
    acceso, creado = AccesoRastreo.objects.get_or_create(
        pedido=pedido, defaults={"token": _nuevo_token()}
    )
    return acceso.token


def url_publica(pedido):
    """Link de rastreo brandeado que viaja al comprador (WhatsApp/email)."""
    return f"{_base_publica()}/r/{obtener_o_crear_token(pedido)}/"


# ── Etiqueta v2: token del QR para el repartidor ─────────────────────────────


def _nuevo_token_etiqueta():
    while True:
        token = secrets.token_urlsafe(9)[:12]
        if not AccesoEtiqueta.objects.filter(token=token).exists():
            return token


def obtener_o_crear_token_etiqueta(guia):
    acceso, creado = AccesoEtiqueta.objects.get_or_create(
        guia=guia, defaults={"token": _nuevo_token_etiqueta()}
    )
    return acceso.token


def url_publica_etiqueta(guia):
    """URL corta que codifica el QR de la etiqueta: la página del repartidor."""
    return f"{_base_publica()}/r/e/{obtener_o_crear_token_etiqueta(guia)}/"


def url_maps(pedido):
    """URL de Google Maps con la ubicación exacta de la entrega.

    Preferencia: lat/lng exactas que marcó el comprador (Shopify las manda en
    shipping_address). Sin coordenadas → la dirección completa urlencodeada.
    """
    direccion = pedido.direccion or {}
    lat, lng = direccion.get("latitude"), direccion.get("longitude")
    if lat not in (None, "") and lng not in (None, ""):
        return f"https://maps.google.com/?q={lat},{lng}"
    partes = [
        str(direccion.get(campo) or "").strip()
        for campo in ("address1", "address2", "city", "province", "zip")
    ]
    texto = ", ".join(p for p in partes if p) or pedido.cp
    return "https://maps.google.com/?" + urlencode({"q": texto})


def referencias_direccion(direccion):
    """Referencias que se imprimen en la etiqueta: address2 + company, sin vacíos."""
    direccion = direccion or {}
    return [
        str(direccion.get(campo) or "").strip()
        for campo in ("address2", "company")
        if str(direccion.get(campo) or "").strip()
    ]
