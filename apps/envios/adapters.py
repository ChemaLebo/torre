"""Adapters de carrier.

`EnviaAdapter` habla con la API real de envia.com (cuenta con tasas
preferenciales; Paquetexpress como carrier físico preferente para Colima).
`MockAdapter` simula todo en memoria cuando no hay `ENVIA_API_KEY` (dev/demo).
La selección vive en `services.get_adapter()`.
"""
import base64
import itertools
import re
import time
import unicodedata
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class ErrorCarrier(Exception):
    """Falla de comunicación o respuesta inválida del carrier/agregador."""


# Origen por defecto: bodega Local 380 E. Sobrescribible con settings.ENVIA_ORIGEN.
# Este bloque viaja al carrier en CADA guía como contacto del remitente
# (recolecta y retornos). Teléfono en 10 dígitos: envia lo acepta nacional y
# el adapter de 99minutos le antepone el +52 solo.
ORIGEN_DEFAULT = {
    "name": "WOP Fulfillment - Local 380 E",
    "company": "WOP Fulfillment",
    "email": "alonso@wop.partners",
    "phone": "5528587520",
    "street": "Av. Torres de Ixtapantongo 380, Local E",
    "number": "380",
    "district": "Olivar de los Padres",
    "city": "Ciudad de Mexico",
    "state": "DF",
    "country": "CX",
    "postalCode": "01780",
}

ESTADOS_CANONICOS = {
    "GUIA_CREADA", "RECOLECTADO", "EN_TRANSITO", "EN_RUTA", "ENTREGADO",
    "INTENTO_FALLIDO", "RETENIDO", "RETORNO", "EXCEPCION",
}

# Patrones ordenados: el primero que aparezca como subcadena gana.
# El orden importa (p. ej. "failed delivery attempt" debe caer en
# INTENTO_FALLIDO antes de que "delivered" atrape "delivery").
PATRONES_ESTADO_ENVIA = [
    # En ruta de entrega (última milla)
    ("out_for_delivery", "EN_RUTA"),
    ("on_route", "EN_RUTA"),
    ("onroute", "EN_RUTA"),
    ("en_ruta", "EN_RUTA"),
    ("reparto", "EN_RUTA"),
    ("last_mile", "EN_RUTA"),
    # Intento fallido (antes que "entregado"/"delivered")
    ("delivery_attempt", "INTENTO_FALLIDO"),
    ("intento", "INTENTO_FALLIDO"),
    ("failed", "INTENTO_FALLIDO"),
    ("attempt", "INTENTO_FALLIDO"),
    ("not_delivered", "INTENTO_FALLIDO"),
    ("no_entregado", "INTENTO_FALLIDO"),
    ("fallido", "INTENTO_FALLIDO"),
    ("ausente", "INTENTO_FALLIDO"),
    # Entregado
    ("delivered", "ENTREGADO"),
    ("entregado", "ENTREGADO"),
    ("entregada", "ENTREGADO"),
    ("proof_of_delivery", "ENTREGADO"),
    # Retorno
    ("return", "RETORNO"),
    ("retorno", "RETORNO"),
    ("devol", "RETORNO"),
    ("devuel", "RETORNO"),
    ("remitente", "RETORNO"),
    # Recolectado por el carrier
    ("picked", "RECOLECTADO"),
    ("pick_up", "RECOLECTADO"),
    ("pickup", "RECOLECTADO"),
    ("collect", "RECOLECTADO"),
    ("recolect", "RECOLECTADO"),
    ("recogido", "RECOLECTADO"),
    # En tránsito
    ("transit", "EN_TRANSITO"),
    ("transito", "EN_TRANSITO"),
    ("camino", "EN_TRANSITO"),
    # Retenido
    ("held", "RETENIDO"),
    ("hold", "RETENIDO"),
    ("retenid", "RETENIDO"),
    ("retencion", "RETENIDO"),
    ("customs", "RETENIDO"),
    ("aduana", "RETENIDO"),
    # Excepción
    ("exception", "EXCEPCION"),
    ("excepcion", "EXCEPCION"),
    ("incident", "EXCEPCION"),
    ("incidenc", "EXCEPCION"),
    ("siniestro", "EXCEPCION"),
    ("extravio", "EXCEPCION"),
    ("lost", "EXCEPCION"),
    ("damaged", "EXCEPCION"),
    ("danado", "EXCEPCION"),
    ("error", "EXCEPCION"),
    # Guía creada (al final: son los textos más genéricos)
    ("created", "GUIA_CREADA"),
    ("creada", "GUIA_CREADA"),
    ("generated", "GUIA_CREADA"),
    ("generada", "GUIA_CREADA"),
    ("label", "GUIA_CREADA"),
    ("etiqueta", "GUIA_CREADA"),
    ("registered", "GUIA_CREADA"),
    ("waiting", "GUIA_CREADA"),
]


def _quitar_acentos(texto):
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")


def normalizar_estado_envia(texto):
    """Estado/descripción cruda de envia.com → estado canónico de Guia.

    Regresa None si no se reconoce (el poller entonces no mueve la guía).
    """
    if not texto:
        return None
    clave = _quitar_acentos(str(texto)).strip().lower()
    clave = re.sub(r"[\s\-./]+", "_", clave)
    if clave.upper() in ESTADOS_CANONICOS:
        return clave.upper()
    for patron, canonico in PATRONES_ESTADO_ENVIA:
        if patron in clave:
            return canonico
    return None


def _parsear_fecha(valor):
    """Fecha del carrier → datetime timezone-aware (None si no parsea)."""
    if not valor:
        return None
    dt = parse_datetime(str(valor))
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return dt


class CarrierAdapter:
    """Contrato mínimo de un carrier: cotizar, cotizar_lane, generar, cancelar, rastrear."""

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        """Regresa el costo cotizado (Decimal) del pedido o de un paquete."""
        raise NotImplementedError

    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        """Cotiza un lane (CP destino, peso) para UN carrier: la fila
        {"carrier","servicio","precio","estimado","ok"} que consume el
        planificador. Sin cobertura o sin respuesta útil → ok=False; la
        falta de cobertura es resultado, jamás excepción."""
        raise NotImplementedError

    def generar(self, pedido, carrier, servicio, paquete=None):
        """Genera la guía. Regresa dict: numero, etiqueta_url, costo, raw."""
        raise NotImplementedError

    def cancelar(self, guia):
        """Cancela la guía ante el carrier. Regresa bool."""
        raise NotImplementedError

    def rastrear(self, numero):
        """Regresa dict: estado (canónico o None), descripcion, ts_evento, raw."""
        raise NotImplementedError


class EnviaAdapter(CarrierAdapter):
    """API real de envia.com: /ship/rate/, /ship/generate/, queries /guide/."""

    PROVEEDOR = "envia"

    def __init__(self):
        self.api_base = settings.ENVIA_API_BASE.rstrip("/")
        self.queries_base = settings.ENVIA_QUERIES_BASE.rstrip("/")
        self.api_key = settings.ENVIA_API_KEY

    # ── HTTP ──
    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _post(self, ruta, payload):
        try:
            resp = requests.post(f"{self.api_base}{ruta}", json=payload, headers=self._headers(), timeout=25)
        except requests.RequestException as exc:
            raise ErrorCarrier(f"No se pudo contactar a envia.com ({ruta}): {exc}") from exc
        return self._json(resp)

    @staticmethod
    def _json(resp):
        if resp.status_code >= 400:
            raise ErrorCarrier(f"envia.com respondió {resp.status_code}: {resp.text[:300]}")
        try:
            cuerpo = resp.json()
        except ValueError as exc:
            raise ErrorCarrier("Respuesta de envia.com no es JSON válido") from exc
        if isinstance(cuerpo, dict) and cuerpo.get("error"):
            raise ErrorCarrier(f"envia.com regresó error: {cuerpo['error']}")
        return cuerpo

    # ── Payloads ──
    @staticmethod
    def _origen():
        return dict(getattr(settings, "ENVIA_ORIGEN", None) or ORIGEN_DEFAULT)

    @staticmethod
    def _destino(pedido):
        from .cotizador import CP_ESTADO  # lazy: la misma tabla que usa el cotizador

        d = pedido.direccion or {}
        cp = str(pedido.cp or d.get("zip") or d.get("postalCode") or "").strip()
        # /ship/generate/ valida contra la columna code_shopify de su catálogo
        # (FAQ de envia) — que es EXACTAMENTE el province_code de Shopify: se
        # pasa derecho, y el CP (tabla en ese mismo vocabulario) cubre los
        # pedidos manuales sin province_code.
        estado = d.get("province_code") or CP_ESTADO.get(cp[:2]) or d.get("state") or d.get("estado", "")
        return {
            "name": pedido.comprador_nombre or d.get("name", ""),
            "street": d.get("address1") or d.get("street") or d.get("calle", ""),
            "number": str(d.get("number") or ""),
            "district": d.get("address2") or d.get("colonia", ""),
            "city": d.get("city") or d.get("ciudad", ""),
            "state": estado,
            "country": "MX",
            "postalCode": cp,
            "phone": pedido.comprador_tel or d.get("phone", ""),
            "email": pedido.comprador_email or d.get("email", ""),
        }

    @staticmethod
    def _paquetes(pedido, paquete=None):
        if paquete is not None:
            lineas = list(paquete.lineas.select_related("linea_pedido__sku"))
            contenido = ", ".join(
                f"{pl.cantidad}x {(pl.linea_pedido.sku.descripcion or pl.linea_pedido.sku.codigo)}"
                for pl in lineas
            )[:120] or "Mercancía"
            # El peso real de báscula manda; el plan solo si la caja no se pesó.
            peso_kg = (
                round(paquete.peso_real_gr / 1000.0, 2)
                if paquete.peso_real_gr else float(paquete.peso_kg)
            )
            return [{
                "content": contenido,
                "amount": 1,
                "type": "box",
                "weight": peso_kg,
                "weightUnit": "KG",
                "lengthUnit": "CM",
                "dimensions": {"length": paquete.largo_cm, "width": paquete.ancho_cm,
                               "height": paquete.alto_cm},
                "declaredValue": float(pedido.valor_declarado or 0),
            }]
        peso_gr = pedido.peso_real_gr or pedido.peso_esperado_gr or 1000
        peso_kg = max(round(peso_gr / 1000.0, 2), 0.1)
        largo, ancho, alto = 30, 25, 20
        contenido = "Mercancía"
        try:
            relacion = pedido.lineas if hasattr(pedido, "lineas") else pedido.lineapedido_set
            skus = [linea.sku for linea in relacion.select_related("sku").all()]
            if skus:
                largo = max(s.largo_cm or largo for s in skus)
                ancho = max(s.ancho_cm or ancho for s in skus)
                alto = max(s.alto_cm or alto for s in skus)
                contenido = ", ".join(
                    filter(None, ((s.descripcion or s.codigo) for s in skus))
                )[:120] or contenido
        except (AttributeError, TypeError):
            pass  # pedido sin líneas cargables: se envía con dimensiones default
        return [{
            "content": contenido,
            "amount": 1,
            "type": "box",
            "weight": peso_kg,
            "weightUnit": "KG",
            "lengthUnit": "CM",
            "dimensions": {"length": largo, "width": ancho, "height": alto},
            "declaredValue": float(pedido.valor_declarado or 0),
        }]

    @staticmethod
    def _sanear(nodo):
        """Envia truena con em-dashes y comillas tipográficas: ASCII seguro."""
        reemplazos = {"—": "-", "–": "-", "\u2019": "'", "\u201c": '"', "\u201d": '"'}
        if isinstance(nodo, dict):
            return {k: EnviaAdapter._sanear(v) for k, v in nodo.items()}
        if isinstance(nodo, list):
            return [EnviaAdapter._sanear(v) for v in nodo]
        if isinstance(nodo, str):
            for malo, bueno in reemplazos.items():
                nodo = nodo.replace(malo, bueno)
            return nodo
        return nodo

    def _payload(self, pedido, carrier, servicio, paquete=None):
        return self._sanear({
            "origin": self._origen(),
            "destination": self._destino(pedido),
            "packages": self._paquetes(pedido, paquete=paquete),
            "shipment": {"carrier": carrier, "service": servicio, "type": 1},
            # /ship/generate/ exige settings con printFormat y printSize (enum de
            # envia.com); STOCK_4X6 = etiqueta térmica 10×15. El rate tolera extras.
            "settings": {"currency": "MXN", "printFormat": "PDF", "printSize": "STOCK_4X6"},
        })

    # ── Operaciones ──
    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        """Una cotización real por lane. 'No cotiza' es resultado (ok=False), no error."""
        from .cotizador import CP_ESTADO  # lazy: mesa también importa esa tabla de ahí
        largo, ancho, alto = dims or (30, 25, 20)
        payload = {
            "origin": self._origen(),
            "destination": {
                "name": "Cotizacion", "email": "alonso@wop.partners", "phone": "5500000000",
                "street": "Conocida", "number": "1", "district": "Centro", "city": "Ciudad",
                "state": CP_ESTADO.get(str(cp_destino)[:2], "DF"), "country": "MX",
                "postalCode": str(cp_destino),
            },
            "packages": [{
                "content": "Mercancia", "amount": 1, "type": "box",
                "weight": float(peso_kg), "weightUnit": "KG", "lengthUnit": "CM",
                "dimensions": {"length": largo, "width": ancho, "height": alto},
            }],
            "shipment": {"carrier": carrier, "type": 1},
            "settings": {"currency": "MXN"},
        }
        try:
            resp = requests.post(
                f"{self.api_base}/ship/rate/", json=payload,
                headers=self._headers(), timeout=30,
            )
            datos = resp.json()
        except (requests.RequestException, ValueError):
            return {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        tarifas = datos.get("data")
        if not isinstance(tarifas, list) or not tarifas:
            return {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        mejor = min(tarifas, key=lambda t: t.get("totalPrice", 10**9))
        return {
            "carrier": carrier, "servicio": mejor.get("service", ""),
            "precio": Decimal(str(mejor["totalPrice"])),
            "estimado": mejor.get("deliveryEstimate", ""), "ok": True,
        }

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        cuerpo = self._post("/ship/rate/", self._payload(pedido, carrier, servicio, paquete=paquete))
        opciones = cuerpo.get("data") or []
        if isinstance(opciones, dict):
            opciones = [opciones]
        if not opciones:
            raise ErrorCarrier(f"envia.com no regresó tarifas para {carrier}/{servicio}")
        elegida = next((o for o in opciones if o.get("service") == servicio), None)
        if elegida is None:
            elegida = min(opciones, key=lambda o: float(o.get("totalPrice") or o.get("total") or 0))
        total = elegida.get("totalPrice") or elegida.get("total") or 0
        return Decimal(str(total)).quantize(Decimal("0.01"))

    def generar(self, pedido, carrier, servicio, paquete=None):
        cuerpo = self._post("/ship/generate/", self._payload(pedido, carrier, servicio, paquete=paquete))
        datos = cuerpo.get("data") or []
        if isinstance(datos, dict):
            datos = [datos]
        if not datos:
            raise ErrorCarrier("envia.com no regresó guía en /ship/generate/")
        d0 = datos[0]
        numero = d0.get("trackingNumber") or d0.get("tracking_number") or ""
        if not numero:
            raise ErrorCarrier("envia.com no regresó trackingNumber en /ship/generate/")
        total = d0.get("totalPrice") or d0.get("total") or 0
        return {
            "numero": numero,
            "etiqueta_url": d0.get("label") or d0.get("labelUrl") or "",
            "costo": Decimal(str(total)).quantize(Decimal("0.01")),
            "raw": d0,
        }

    def cancelar(self, guia):
        self._post("/ship/cancel/", {"carrier": guia.carrier, "trackingNumber": guia.numero})
        return True

    def rastrear(self, numero):
        try:
            resp = requests.get(f"{self.queries_base}/guide/{numero}", headers=self._headers(), timeout=25)
        except requests.RequestException as exc:
            raise ErrorCarrier(f"No se pudo rastrear la guía {numero}: {exc}") from exc
        cuerpo = self._json(resp)
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if isinstance(datos, dict):
            datos = [datos]
        if not datos:
            raise ErrorCarrier(f"envia.com sin datos de rastreo para {numero}")
        d0 = datos[0]
        eventos = d0.get("events") or d0.get("history") or d0.get("eventHistory") or []
        ultimo = self._evento_mas_reciente(eventos)
        crudo = d0.get("status") or d0.get("statusDetail") or ultimo.get("status") or ""
        descripcion = ultimo.get("description") or ultimo.get("event") or str(crudo)
        estado = normalizar_estado_envia(crudo) or normalizar_estado_envia(descripcion)
        ts_evento = _parsear_fecha(ultimo.get("date") or ultimo.get("created_at") or d0.get("lastUpdate"))
        return {"estado": estado, "descripcion": descripcion, "ts_evento": ts_evento, "raw": d0}

    @staticmethod
    def _evento_mas_reciente(eventos):
        limpios = [e for e in eventos if isinstance(e, dict)]
        if not limpios:
            return {}
        con_fecha = [(e, _parsear_fecha(e.get("date") or e.get("created_at"))) for e in limpios]
        fechados = [par for par in con_fecha if par[1] is not None]
        if fechados:
            return max(fechados, key=lambda par: par[1])[0]
        return limpios[-1]


# ── 99minutos directo (API v3) ───────────────────────────────────────────────

# Estado numérico de 99minutos → estado canónico de Guia.
CODIGOS_ESTADO_99MIN = {
    1001: "GUIA_CREADA", 1002: "GUIA_CREADA",   # borrador / confirmada
    2001: "GUIA_CREADA", 2002: "GUIA_CREADA",   # por recoger / chofer asignado
    2003: "RECOLECTADO",
    2101: "EXCEPCION",                          # recogida fallida
    3001: "EN_TRANSITO", 3002: "EN_TRANSITO", 3003: "EN_TRANSITO", 3004: "EN_TRANSITO",
    4001: "EN_RUTA",
    4002: "ENTREGADO",
    4101: "INTENTO_FALLIDO",
    5001: "RETORNO", 5002: "RETORNO",
    5101: "EXCEPCION",
    8001: "EXCEPCION", 8002: "EXCEPCION", 8004: "EXCEPCION",  # robo / perdido / dañado
    8003: "EXCEPCION",                          # cancelada fuera de Torre
}

_DELIVERY_TYPES_99MIN = {"NAL", "SPT", "SMD", "99M", "CO2F", "RET", "TLM", "P2P"}
# Servicio de Torre → deliveryType nativo; "ground" (el default del ruteo) = NAL.
SERVICIOS_99MIN = {"ground": "NAL", "": "NAL", "express": "SPT", "same_day": "SMD"}


def delivery_type_99min(servicio):
    codigo = (servicio or "").strip()
    if codigo.upper() in _DELIVERY_TYPES_99MIN:
        return codigo.upper()
    return SERVICIOS_99MIN.get(codigo.lower(), "NAL")


class Adapter99Minutos(CarrierAdapter):
    """API directa de 99minutos (v3): oauth JWT ~1h, /orders + /documents/guides
    (etiqueta zebra 4×6 en BASE64, no URL), rates por par de CP y retrieve de
    shipments. Sandbox con el mismo contrato vía NOVENTA9_API_BASE.
    OJO unidades: 99minutos habla GRAMOS donde envia habla KG."""

    PROVEEDOR = "99minutos"
    PAIS = "MEX"  # verificar en sandbox (MEX vs MX)
    _token_cache = {"token": "", "expira": 0.0}  # compartido entre instancias

    def __init__(self):
        self.base = getattr(settings, "NOVENTA9_API_BASE", "https://delivery.99minutos.com").rstrip("/")
        credencial = getattr(settings, "NOVENTA9_API_KEY", "")
        self.client_id, _, self.client_secret = credencial.partition(":")

    # ── auth ──
    @classmethod
    def reiniciar_token(cls):
        cls._token_cache = {"token": "", "expira": 0.0}

    def _token(self):
        cache = type(self)._token_cache
        if cache["token"] and cache["expira"] > time.time():
            return cache["token"]
        try:
            resp = requests.post(
                f"{self.base}/api/v3/oauth/token",
                json={"client_id": self.client_id, "client_secret": self.client_secret},
                timeout=25,
            )
        except requests.RequestException as exc:
            raise ErrorCarrier(f"No se pudo autenticar con 99minutos: {exc}") from exc
        if resp.status_code >= 400:
            raise ErrorCarrier(f"99minutos oauth respondió {resp.status_code}: {resp.text[:200]}")
        cuerpo = resp.json()
        token = cuerpo.get("access_token") or ""
        if not token:
            raise ErrorCarrier("99minutos no regresó access_token")
        cache["token"] = token
        cache["expira"] = time.time() + max(int(cuerpo.get("expires_in") or 3599) - 60, 60)
        return token

    def _request(self, metodo, ruta, params=None, json_body=None, reintento=True):
        try:
            resp = requests.request(
                metodo, f"{self.base}{ruta}", params=params, json=json_body,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ErrorCarrier(f"No se pudo contactar a 99minutos ({ruta}): {exc}") from exc
        if resp.status_code == 401 and reintento:
            type(self).reiniciar_token()  # JWT vencido: re-auth una sola vez
            return self._request(metodo, ruta, params=params, json_body=json_body, reintento=False)
        return resp

    @staticmethod
    def _json(resp, ruta):
        if resp.status_code >= 400:
            raise ErrorCarrier(f"99minutos respondió {resp.status_code} en {ruta}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ErrorCarrier(f"Respuesta de 99minutos no es JSON ({ruta})") from exc

    @staticmethod
    def _origen_info():
        return dict(getattr(settings, "ENVIA_ORIGEN", None) or ORIGEN_DEFAULT)

    # ── cotización por lane ──
    def _size_para(self, peso_kg, dims):
        largo, ancho, alto = dims or (30, 25, 20)
        resp = self._request("GET", "/api/v3/shipping/rates/sizes", params={
            "weight": int(Decimal(str(peso_kg)) * 1000), "width": ancho, "height": alto, "depth": largo,
        })
        cuerpo = self._json(resp, "/shipping/rates/sizes")
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if isinstance(datos, list) and datos:
            datos = datos[0]
        if isinstance(datos, dict):
            return str(datos.get("size") or datos.get("name") or "")
        return str(datos or "")

    @staticmethod
    def _mejor_tarifa(cuerpo):
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if isinstance(datos, dict):
            datos = [datos]
        if not isinstance(datos, list):
            return None
        opciones = []
        for opcion in datos:
            if not isinstance(opcion, dict):
                continue
            crudo = (opcion.get("totalPrice") or opcion.get("price")
                     or opcion.get("amount") or opcion.get("total"))
            if crudo is None:
                continue
            try:
                precio = Decimal(str(crudo)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                continue
            opciones.append((
                precio,
                str(opcion.get("deliveryType") or opcion.get("service") or "NAL"),
                str(opcion.get("deliveryEstimate") or opcion.get("estimatedDelivery") or ""),
            ))
        return min(opciones, key=lambda o: o[0]) if opciones else None

    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        origen_cp = str(self._origen_info().get("postalCode", "01780"))
        sin_cobertura = {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        try:
            size = self._size_para(peso_kg, dims)
            params = {"delivery_type": "NAL"}
            if size:
                params["size"] = size
            resp = self._request(
                "GET",
                f"/api/v3/shipping/rates/zipcodes/{self.PAIS}/{origen_cp}/{self.PAIS}/{cp_destino}",
                params=params,
            )
            if resp.status_code == 412:  # sin cobertura del par de CPs: resultado, no error
                return sin_cobertura
            cuerpo = self._json(resp, "/shipping/rates/zipcodes")
        except ErrorCarrier:
            return sin_cobertura
        mejor = self._mejor_tarifa(cuerpo)
        if mejor is None:
            return sin_cobertura
        precio, servicio, estimado = mejor
        return {"carrier": carrier, "servicio": servicio, "precio": precio, "estimado": estimado, "ok": True}

    # ── generación ──
    @staticmethod
    def _fisico(pedido, paquete):
        if paquete is not None:
            lineas = list(paquete.lineas.select_related("linea_pedido__sku"))
            contenido = ", ".join(
                f"{pl.cantidad}x {(pl.linea_pedido.sku.descripcion or pl.linea_pedido.sku.codigo)}"
                for pl in lineas
            )[:120] or "Mercancía"
            # El peso real de báscula manda; el plan solo si la caja no se pesó.
            peso_gr = int(paquete.peso_real_gr or Decimal(str(paquete.peso_kg)) * 1000)
            return (peso_gr,
                    paquete.largo_cm, paquete.ancho_cm, paquete.alto_cm, contenido)
        peso_gr = int(pedido.peso_real_gr or pedido.peso_esperado_gr or 1000)
        return (peso_gr, 30, 25, 20, "Mercancía")

    @staticmethod
    def _telefono(crudo):
        tel = str(crudo or "").strip().replace(" ", "")
        if not tel:
            return ""
        return tel if tel.startswith("+") else f"+52{tel}"

    def _shipment(self, pedido, servicio, paquete, interno):
        bodega = self._origen_info()
        d = pedido.direccion or {}
        nombre = (pedido.comprador_nombre or d.get("name") or "").strip() or "Comprador"
        partes = nombre.split(" ", 1)
        peso_gr, largo, ancho, alto, contenido = self._fisico(pedido, paquete)
        return EnviaAdapter._sanear({
            "internalKey": interno,
            "deliveryType": delivery_type_99min(servicio),
            "sender": {
                "firstName": bodega.get("company") or bodega.get("name") or "WOP Fulfillment",
                "lastName": "Bodega",
                "phone": self._telefono(bodega.get("phone")),
                "email": bodega.get("email", ""),
            },
            "recipient": {
                "firstName": partes[0],
                "lastName": partes[1] if len(partes) > 1 else ".",
                "phone": self._telefono(pedido.comprador_tel or d.get("phone")),
                "email": pedido.comprador_email or d.get("email") or "",
            },
            "origin": {
                "address": ", ".join(filter(None, [
                    bodega.get("street", ""), bodega.get("district", ""), bodega.get("city", ""),
                ])),
                "country": self.PAIS,
                "zipcode": str(bodega.get("postalCode", "")),
                "city": bodega.get("city", ""),
            },
            "destination": {
                "address": ", ".join(filter(None, [
                    d.get("address1") or d.get("street") or "",
                    d.get("address2") or "",
                    d.get("city") or "",
                    d.get("province") or "",
                ])),
                "country": self.PAIS,
                "zipcode": str(pedido.cp or d.get("zip") or ""),
                "city": d.get("city") or "",
            },
            "items": [{
                "description": contenido,
                "weight": peso_gr,  # GRAMOS — no confundir con los KG de envia
                "length": largo, "width": ancho, "height": alto,
            }],
        })

    def _tracking_de_orden(self, cuerpo):
        datos = cuerpo.get("data") or {}
        envios = datos.get("shipments") if isinstance(datos, dict) else None
        if envios and isinstance(envios[0], dict):
            return envios[0].get("trackingId") or envios[0].get("tracking_id") or ""
        return ""

    def _shipment_remoto(self, identificador):
        resp = self._request("GET", f"/api/v3/shipments/{identificador}")
        cuerpo = self._json(resp, "/shipments")
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if isinstance(datos, list):
            datos = datos[0] if datos else {}
        return datos if isinstance(datos, dict) else {}

    def _etiqueta_zebra(self, tracking):
        resp = self._request("POST", "/api/v3/documents/guides", json_body={
            "guides": [{"identifier": str(tracking), "size": "zebra"}],  # zebra = térmica 4×6
        })
        cuerpo = self._json(resp, "/documents/guides")
        datos = cuerpo.get("data") or []
        if isinstance(datos, dict):
            datos = [datos]
        b64 = datos[0].get("pdf") if datos and isinstance(datos[0], dict) else ""
        if not b64:
            raise ErrorCarrier(f"99minutos no regresó el PDF de la guía {tracking}")
        try:
            pdf = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise ErrorCarrier(f"El PDF de la guía {tracking} no se pudo decodificar") from exc
        if not pdf.startswith(b"%PDF"):
            raise ErrorCarrier(f"La etiqueta de {tracking} no es un PDF válido")
        return pdf

    def generar(self, pedido, carrier, servicio, paquete=None):
        interno = f"{pedido.folio}-{paquete.numero if paquete is not None else 1}"
        envio = self._shipment(pedido, servicio, paquete, interno)
        resp = self._request("POST", "/api/v3/orders", json_body={"shipments": [envio]})
        if resp.status_code == 202:
            # internalKey duplicado: la guía ya existe allá — se recupera, no se recompra.
            tracking = self._shipment_remoto(interno).get("trackingId") or ""
        else:
            tracking = self._tracking_de_orden(self._json(resp, "/orders"))
        if not tracking:
            raise ErrorCarrier("99minutos no regresó trackingId en /orders")
        return {
            "numero": str(tracking),
            "etiqueta_url": "",
            "etiqueta_pdf": self._etiqueta_zebra(tracking),
            "costo": None,  # el rate ya vive en el plan (precio_cotizado)
            "raw": {"proveedor": "99minutos", "trackingId": tracking, "internalKey": interno},
        }

    def cancelar(self, guia):
        resp = self._request("DELETE", f"/api/v3/shipments/{guia.numero}")
        self._json(resp, "/shipments (cancel)")
        return True

    def rastrear(self, numero):
        datos = self._shipment_remoto(numero)
        if not datos:
            raise ErrorCarrier(f"99minutos sin datos de rastreo para {numero}")
        codigo, texto = self._estado_crudo(datos)
        estado = CODIGOS_ESTADO_99MIN.get(codigo) if codigo is not None else None
        if estado is None:
            estado = normalizar_estado_envia(texto)
        ts_evento = _parsear_fecha(
            datos.get("updatedAt") or datos.get("lastUpdate") or datos.get("updated_at")
        )
        descripcion = (texto or (str(codigo) if codigo is not None else ""))[:300]
        return {"estado": estado, "descripcion": descripcion, "ts_evento": ts_evento, "raw": datos}

    @staticmethod
    def _estado_crudo(datos):
        crudo = datos.get("status")
        if isinstance(crudo, dict):
            codigo = crudo.get("code") or crudo.get("id")
            texto = str(crudo.get("name") or crudo.get("description") or "")
        else:
            codigo = crudo
            texto = str(datos.get("statusName") or datos.get("statusDescription") or "")
        try:
            codigo = int(codigo)
        except (TypeError, ValueError):
            texto = texto or (str(codigo) if codigo else "")
            codigo = None
        return codigo, texto


# Tarifario MOCK (tabla real medida contra la API 2026-08; se usa sin
# ENVIA_API_KEY: dev, demo y tests). Cobertura y topes incluidos.
MOCK_PUNTOPOST_PREFIJOS = {"06", "44", "64", "91", "72", "76", "01", "02", "03", "45", "66", "67"}
MOCK_PUNTOPOST_MAX_KG = Decimal("10")
MOCK_TARIFARIO = {
    "puntopost": {4: 86, 8: 91, 10: 91},
    "estafeta": {4: 149, 8: 177, 12: 201, 16: 224, 20: 247, 25: 278},
    "paquetexpress": {4: 194, 8: 228, 12: 260, 16: 291, 20: 323, 25: 362},
    "fedex": {4: 184, 8: 229, 12: 248, 16: 297, 20: 331, 25: 376},
    # Estimado para demo/dev (carril SAL-99MIN): medir contra la API directa.
    "noventa9Minutos": {4: 139, 8: 165, 12: 189, 16: 210, 20: 232, 25: 260},
}
MOCK_ESTIMADOS = {
    "puntopost": "5-7 días", "estafeta": "2-3 días", "paquetexpress": "1-2 días",
    "fedex": "1-2 días", "noventa9Minutos": "2-4 días",
}


def _interpolar(tabla, peso):
    """Interpola/extrapola linealmente el tarifario mock."""
    puntos = sorted(tabla.items())
    peso = float(peso)
    if peso <= puntos[0][0]:
        return Decimal(str(puntos[0][1]))
    for (p1, c1), (p2, c2) in zip(puntos, puntos[1:]):
        if peso <= p2:
            frac = (peso - p1) / (p2 - p1)
            return Decimal(str(round(c1 + frac * (c2 - c1), 2)))
    (p1, c1), (p2, c2) = puntos[-2], puntos[-1]
    pendiente = (c2 - c1) / (p2 - p1)
    return Decimal(str(round(c2 + (peso - p2) * pendiente, 2)))


class MockAdapter(CarrierAdapter):
    """Simulador en memoria: números MOCK-#### y tracking manipulable.

    Se usa cuando no hay `ENVIA_API_KEY` (dev/demo/tests). `avanzar_estado`
    permite simular el viaje del paquete sin tocar la API real.
    """

    PROVEEDOR = "mock"

    SECUENCIA_FELIZ = ["GUIA_CREADA", "RECOLECTADO", "EN_TRANSITO", "EN_RUTA", "ENTREGADO"]
    DESCRIPCIONES = {
        "GUIA_CREADA": "Guía generada (mock)",
        "RECOLECTADO": "Paquete recolectado en origen (mock)",
        "EN_TRANSITO": "En tránsito hacia destino (mock)",
        "EN_RUTA": "En ruta de entrega (mock)",
        "ENTREGADO": "Entregado (mock)",
        "INTENTO_FALLIDO": "Intento de entrega fallido: destinatario ausente (mock)",
        "RETENIDO": "Paquete retenido (mock)",
        "RETORNO": "Retornado al remitente (mock)",
        "EXCEPCION": "Excepción del carrier (mock)",
    }

    _registro = {}  # numero -> estado canónico (compartido entre instancias)
    _consecutivo = itertools.count(1)

    @classmethod
    def reiniciar(cls):
        """Limpia el estado simulado (para tests)."""
        cls._registro = {}
        cls._consecutivo = itertools.count(1)

    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        """Tarifario simulado fiel a lo medido: cobertura y topes incluidos."""
        tabla = MOCK_TARIFARIO.get(carrier)
        if tabla is None:
            return {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        prefijo = str(cp_destino)[:2]
        if carrier == "puntopost" and (
            prefijo not in MOCK_PUNTOPOST_PREFIJOS or Decimal(str(peso_kg)) > MOCK_PUNTOPOST_MAX_KG
        ):
            return {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        return {
            "carrier": carrier, "servicio": "mock", "precio": _interpolar(tabla, peso_kg),
            "estimado": MOCK_ESTIMADOS.get(carrier, ""), "ok": True,
        }

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        if paquete is not None and paquete.precio_cotizado:
            return Decimal(paquete.precio_cotizado)
        return Decimal("65.00") if getattr(pedido, "es_local", False) else Decimal("118.00")

    def generar(self, pedido, carrier, servicio, paquete=None):
        numero = f"MOCK-{next(self._consecutivo):04d}"
        while numero in self._registro:
            numero = f"MOCK-{next(self._consecutivo):04d}"
        self._registro[numero] = "GUIA_CREADA"
        if paquete is not None and paquete.precio_cotizado:
            costo = Decimal(paquete.precio_cotizado)
        else:
            costo = (self.cotizar(pedido, carrier, servicio) * Decimal("0.82")).quantize(Decimal("0.01"))
        return {
            "numero": numero,
            "etiqueta_url": f"https://etiquetas.mock/{numero}.pdf",
            "costo": costo,
            "raw": {"mock": True, "carrier": carrier, "service": servicio, "trackingNumber": numero},
        }

    def cancelar(self, guia):
        self._registro.pop(guia.numero, None)
        return True

    def rastrear(self, numero):
        estado = self._registro.setdefault(numero, "GUIA_CREADA")
        return {
            "estado": estado,
            "descripcion": self.DESCRIPCIONES.get(estado, estado),
            "ts_evento": None,
            "raw": {"mock": True, "trackingNumber": numero, "status": estado},
        }

    def avanzar_estado(self, numero, estado=None):
        """Simula tracking: avanza al siguiente paso de la ruta feliz,
        o brinca directo al estado canónico dado (para simular fallos)."""
        actual = self._registro.get(numero, "GUIA_CREADA")
        if estado is None:
            try:
                idx = self.SECUENCIA_FELIZ.index(actual)
                estado = self.SECUENCIA_FELIZ[min(idx + 1, len(self.SECUENCIA_FELIZ) - 1)]
            except ValueError:
                estado = "EN_TRANSITO"
        if estado not in ESTADOS_CANONICOS:
            raise ValueError(f"Estado no canónico: {estado}")
        self._registro[numero] = estado
        return estado
