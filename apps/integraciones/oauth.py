"""Conexión de una tienda Shopify por OAuth desde Torre (Chema 2026-10-21).

Authorization code grant, el flujo de las apps sin interfaz: Mesa (o el
cliente, con un link firmado) abre la pantalla de permisos de Shopify con la
lista de scopes de Torre; Shopify regresa a /oauth/callback con un código y
Torre lo cambia por el token de la Admin API. Cada tienda trae su propia app
custom (client_id / client_secret): la distribución custom no cruza merchants.
Después de conectar, `pasos_post_conexion` deja la tienda operable: webhooks
por API (firmados con el client_secret), definición del metafield del link,
locations y verificación de permisos. Nunca se piden tokens con caducidad.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core import signing
from django.db import connection
from django.utils import timezone

from apps.core.services import registrar_evento

from .shopify import ShopifyClient, ShopifyError

# Permisos que Torre necesita en cada tienda (CONVENTIONS → integraciones).
SCOPES_TORRE = (
    "read_orders", "write_orders", "read_products",
    "read_inventory", "write_inventory", "read_locations",
    "read_fulfillments", "write_fulfillments",
    "read_merchant_managed_fulfillment_orders", "write_merchant_managed_fulfillment_orders",
)
SCOPE_TODAS_LAS_ORDENES = "read_all_orders"  # restringido: lo concede Soporte app por app
# Topic de la API GraphQL → topic del header X-Shopify-Topic que procesa Torre.
TOPICS_WEBHOOK = {
    "ORDERS_CREATE": "orders/create",
    "ORDERS_UPDATED": "orders/updated",
    "ORDERS_CANCELLED": "orders/cancelled",
    "FULFILLMENT_ORDERS_MOVED": "fulfillment_orders/moved",
}
_DOMINIO_SHOPIFY = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_SAL_LINK = "integraciones.oauth.link"
LINK_VIGENCIA_S = 7 * 24 * 3600


def base_publica():
    """Dominio público de Torre (BASE_URL_PUBLICA, el mismo de rastreo y QRs)."""
    return os.environ.get("BASE_URL_PUBLICA", "http://127.0.0.1:8380").rstrip("/")


def redirect_uri():
    """La redirect URL registrada en cada app del Dashboard: debe coincidir EXACTA."""
    return f"{base_publica()}/oauth/callback"


def scopes_de(tienda):
    """Permisos a pedir: los de Torre más read_all_orders si Soporte lo concedió."""
    scopes = list(SCOPES_TORRE)
    if tienda.read_all_orders:
        scopes.append(SCOPE_TODAS_LAS_ORDENES)
    return scopes


def iniciar_conexion(tienda, actor=None):
    """Genera el nonce de la conexión y regresa la URL de permisos de Shopify.
    ValueError si la tienda no tiene app (client_id / client_secret) o su
    dominio no es myshopify.com."""
    if not tienda.client_id or not tienda.client_secret:
        raise ValueError(f"{tienda.dominio}: captura el client ID y el client secret de su app antes de conectar.")
    if not _DOMINIO_SHOPIFY.match(tienda.dominio):
        raise ValueError(f"{tienda.dominio} no es un dominio myshopify.com.")
    tienda.oauth_state = secrets.token_urlsafe(24)
    tienda.save(update_fields=["oauth_state"])
    registrar_evento(
        "tienda", tienda.dominio, "conexion_iniciada", actor=actor, cliente=tienda.cliente,
        delta={"scopes": scopes_de(tienda)}, motivo="Se abrió la pantalla de permisos de Shopify.",
    )
    consulta = urlencode({
        "client_id": tienda.client_id, "scope": ",".join(scopes_de(tienda)),
        "redirect_uri": redirect_uri(), "state": tienda.oauth_state,
    })
    return f"https://{tienda.dominio}/admin/oauth/authorize?{consulta}"


def link_de_conexion(tienda):
    """Link firmado para que el cliente conecte desde su admin sin entrar a
    Mesa; vale 7 días y solo abre la pantalla de permisos de SU tienda."""
    if not tienda.client_id or not tienda.client_secret:
        raise ValueError(f"{tienda.dominio}: captura el client ID y el client secret de su app antes de generar el link.")
    firma = signing.TimestampSigner(salt=_SAL_LINK).sign(str(tienda.pk))
    return f"{base_publica()}/oauth/conectar/{firma}/"


def tienda_del_link(firma):
    """Tienda de un link de conexión; None si la firma no vale o caducó."""
    from .models import Tienda  # lazy: evita ciclos con services

    try:
        pk = signing.TimestampSigner(salt=_SAL_LINK).unsign(firma, max_age=LINK_VIGENCIA_S)
    except signing.BadSignature:
        return None
    return Tienda.objects.select_related("cliente").filter(pk=pk).first()


def firma_valida(params, client_secret):
    """HMAC del callback de Shopify: hex SHA-256 con el client_secret sobre los
    parámetros sin `hmac`, ordenados y unidos con & como clave=valor (la clave
    con % y = escapados; el valor con % y &), como lo documenta Shopify."""
    recibida = params.get("hmac", "")
    if not recibida or not client_secret:
        return False
    partes = []
    for clave in sorted(k for k in params if k != "hmac"):
        valor = params[clave]
        partes.append(
            clave.replace("%", "%25").replace("=", "%3D")
            + "="
            + valor.replace("%", "%25").replace("&", "%26")
        )
    esperada = hmac.new(client_secret.encode("utf-8"), "&".join(partes).encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, recibida)


def intercambiar_codigo(tienda, code):
    """POST /admin/oauth/access_token: el código de la aprobación por el token
    (sin caducidad: no se manda `expiring`). Regresa el cuerpo de Shopify."""
    try:
        resp = requests.post(
            f"https://{tienda.dominio}/admin/oauth/access_token",
            json={"client_id": tienda.client_id, "client_secret": tienda.client_secret, "code": code},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise ShopifyError(f"No se pudo contactar a {tienda.dominio} para cambiar el código: {exc}") from exc
    if resp.status_code >= 400:
        raise ShopifyError(f"{tienda.dominio} rechazó el código ({resp.status_code}): {resp.text[:200]}")
    try:
        cuerpo = resp.json()
    except ValueError as exc:
        raise ShopifyError(f"{tienda.dominio} no regresó JSON al cambiar el código.") from exc
    if not cuerpo.get("access_token"):
        raise ShopifyError(f"{tienda.dominio} no regresó access_token.")
    return cuerpo


def conectar_tienda(tienda, code, actor=None, *, desde_link_dashboard=False):
    """Guarda el token y los permisos otorgados y cierra el nonce. Evento
    `tienda_conectada` (sin el token); `desde_link_dashboard` deja dicho que
    la primera instalación entró por el link del Dashboard y no por el botón."""
    cuerpo = intercambiar_codigo(tienda, code)
    tienda.token = cuerpo["access_token"]
    tienda.scopes = cuerpo.get("scope") or ",".join(scopes_de(tienda))
    tienda.oauth_state = ""
    tienda.ts_conectada = timezone.now()
    tienda.save(update_fields=["token", "scopes", "oauth_state", "ts_conectada"])
    registrar_evento(
        "tienda", tienda.dominio, "tienda_conectada", actor=actor, cliente=tienda.cliente,
        delta={"scopes": tienda.scopes.split(","), "desde_link_dashboard": desde_link_dashboard},
        motivo="Token de la Admin API guardado por OAuth"
        + (" (primera instalación desde el link del Dashboard)." if desde_link_dashboard else "."),
    )
    return tienda


def pasos_post_conexion(tienda, actor=None):
    """Deja la tienda operable tras conectar; cada paso es best-effort y el
    resumen queda en el evento `post_conexion`:
    - webhooks: los cuatro topics por API hacia hooks/shopify/<id>/, firmados
      con el client_secret (webhook_secret = client_secret). Si la tienda ya
      firma con OTRO secret (webhooks capturados en Notificaciones) no se toca
      nada: Mesa decide si migra, para no recibir cada orden dos veces.
    - metafield: definición del link al portal (services.crear_definicion_link).
    - locations: las activas de la tienda; si solo hay una, o una dice WOP, se
      propone como location_id cuando está vacío.
    - permisos: currentAppInstallation.accessScopes contra scopes_de(tienda)."""
    from .services import crear_definicion_link  # lazy: services importa shopify

    resumen = {"webhooks": "", "metafield": "", "locations": [], "location_elegida": "", "permisos_faltantes": []}
    try:
        api = ShopifyClient(tienda)
    except ShopifyError as exc:
        resumen["error"] = str(exc)
        return resumen
    resumen["webhooks"] = _asegurar_webhooks(tienda, api)
    resumen["metafield"] = crear_definicion_link(tienda)
    resumen["locations"], resumen["location_elegida"] = _locations(tienda, api)
    resumen["permisos_faltantes"] = _permisos_faltantes(tienda, api)
    registrar_evento(
        "tienda", tienda.dominio, "post_conexion", actor=actor, cliente=tienda.cliente, delta=resumen,
        motivo="Después de conectar: webhooks, metafield, locations y permisos.",
    )
    return resumen


def _asegurar_webhooks(tienda, api):
    """Crea por API los webhooks que falten hacia la URL de la tienda y deja
    webhook_secret = client_secret (Shopify firma con él lo creado por API)."""
    if tienda.webhook_secret and tienda.webhook_secret != tienda.client_secret:
        return "sin tocar: la tienda ya firma con otro secret (webhooks de Notificaciones); migra desde Mesa si quieres"
    url = f"{base_publica()}/hooks/shopify/{tienda.pk}/"
    try:
        datos = api.graphql(
            "query { webhookSubscriptions(first: 100) { edges { node { id topic "
            "endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } } }"
        )
        existentes = set()
        for borde in (datos.get("webhookSubscriptions") or {}).get("edges") or []:
            nodo = borde.get("node") or {}
            endpoint = nodo.get("endpoint") or {}
            existentes.add((nodo.get("topic"), endpoint.get("callbackUrl")))
        creados, errores = [], []
        for topic, nombre in TOPICS_WEBHOOK.items():
            if (topic, url) in existentes:
                continue
            respuesta = api.graphql(
                "mutation($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {"
                " webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {"
                " webhookSubscription { id } userErrors { field message } } }",
                {"topic": topic, "sub": {"callbackUrl": url, "format": "JSON"}},
            )
            errores_usuario = (respuesta.get("webhookSubscriptionCreate") or {}).get("userErrors") or []
            if errores_usuario:
                errores.append(f"{nombre}: {errores_usuario[0].get('message')}")
            else:
                creados.append(nombre)
    except ShopifyError as exc:
        return f"error: {exc}"
    if tienda.webhook_secret != tienda.client_secret:
        tienda.webhook_secret = tienda.client_secret
        tienda.save(update_fields=["webhook_secret"])
    texto = f"{len(creados)} creados" if creados else "ya existían los 4"
    if errores:
        texto += "; errores: " + "; ".join(errores)
    return texto


def _locations(tienda, api):
    """(locations activas como texto, nombre de la elegida si se propuso una)."""
    try:
        datos = api.graphql("query { locations(first: 50, includeInactive: false) { edges { node { id name isActive } } } }")
    except ShopifyError as exc:
        return [f"error: {exc}"], ""
    locations = [
        {"id": nodo["id"], "name": nodo.get("name") or ""}
        for borde in (datos.get("locations") or {}).get("edges") or []
        for nodo in [borde.get("node") or {}] if nodo.get("id")
    ]
    elegida = ""
    if not tienda.location_id and locations:
        con_wop = [loc for loc in locations if "wop" in loc["name"].lower()]
        candidata = con_wop[0] if len(con_wop) == 1 else (locations[0] if len(locations) == 1 else None)
        if candidata is not None:
            tienda.location_id = candidata["id"]
            tienda.save(update_fields=["location_id"])
            elegida = candidata["name"]
    return [f"{loc['name']} · {loc['id']}" for loc in locations], elegida


def _permisos_faltantes(tienda, api):
    """Lo que Torre pide y la instalación no tiene; de paso refresca `scopes`."""
    try:
        datos = api.graphql("query { currentAppInstallation { accessScopes { handle } } }")
    except ShopifyError as exc:
        return [f"error: {exc}"]
    otorgados = {s.get("handle") for s in (datos.get("currentAppInstallation") or {}).get("accessScopes") or []}
    if otorgados:
        tienda.scopes = ",".join(sorted(otorgados))
        tienda.save(update_fields=["scopes"])
    return [s for s in scopes_de(tienda) if s not in otorgados]


def sincronizar_en_segundo_plano(tienda, actor=None):
    """"Sincronizar ahora" de UNA tienda: catálogo y reconciliación (backfill
    si es la primera) en un hilo con su propia conexión, nunca dentro de la
    petición de Mesa. Con TORRE["SHOPIFY_ESCRITURAS_EN_HILO"] = False
    (pruebas) corre en línea. ValueError sin token."""
    from . import services  # lazy: services importa shopify

    if not tienda.token:
        raise ValueError(f"{tienda.dominio} no tiene token: conecta la tienda primero.")

    def _correr():
        try:
            catalogo = services.sincronizar_catalogo(tienda)
            nuevos = services.reconciliar_pedidos(tienda)
            registrar_evento(
                "tienda", tienda.dominio, "sync_manual", actor=actor, cliente=tienda.cliente,
                delta={"catalogo": catalogo, "pedidos_nuevos": nuevos}, motivo="Sincronizar ahora desde Mesa.",
            )
        except Exception as exc:  # noqa: BLE001 — en un hilo no hay a quién levantar: queda el evento
            registrar_evento(
                "tienda", tienda.dominio, "sync_manual_error", actor=actor, cliente=tienda.cliente,
                delta={"error": str(exc)[:300]}, motivo="Sincronizar ahora falló.",
            )

    if not settings.TORRE.get("SHOPIFY_ESCRITURAS_EN_HILO", True):
        _correr()
        return None

    def _en_hilo():
        try:
            _correr()
        finally:
            connection.close()

    hilo = threading.Thread(target=_en_hilo, name=f"shopify-sync-{tienda.pk}", daemon=True)
    hilo.start()
    return hilo
