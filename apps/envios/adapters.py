"""Adapters de carrier.

`EnviaAdapter` habla con la API real de envia.com (cuenta con tasas
preferenciales; Paquetexpress como carrier físico preferente para Colima).
`MockAdapter` simula todo en memoria cuando no hay `ENVIA_API_KEY` (dev/demo).
La selección vive en `services.get_adapter()`.
"""
import itertools
import re
import unicodedata
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class ErrorCarrier(Exception):
    """Falla de comunicación o respuesta inválida del carrier/agregador."""


# Origen por defecto: bodega Local 380 E, Colima. Sobrescribible con settings.ENVIA_ORIGEN.
ORIGEN_DEFAULT = {
    "name": "Torre 3PL - Local 380 E",
    "company": "Torre 3PL",
    "email": "operaciones@torre3pl.mx",
    "phone": "5512340000",
    "street": "Av. Torres de Ixtapantongo 380, Local E",
    "number": "380",
    "district": "Olivar de los Padres",
    "city": "Ciudad de Mexico",
    "state": "DF",
    "country": "MX",
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
    """Contrato mínimo de un carrier: cotizar, generar, cancelar, rastrear."""

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        """Regresa el costo cotizado (Decimal) del pedido o de un paquete."""
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
        d = pedido.direccion or {}
        return {
            "name": pedido.comprador_nombre or d.get("name", ""),
            "street": d.get("address1") or d.get("street") or d.get("calle", ""),
            "number": str(d.get("number") or ""),
            "district": d.get("address2") or d.get("colonia", ""),
            "city": d.get("city") or d.get("ciudad", ""),
            "state": d.get("province_code") or d.get("state") or d.get("estado", ""),
            "country": "MX",
            "postalCode": str(pedido.cp or d.get("zip") or d.get("postalCode") or ""),
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
            return [{
                "content": contenido,
                "amount": 1,
                "type": "box",
                "weight": float(paquete.peso_kg),
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
            # /ship/generate/ exige settings con printFormat (400s sucesivos sin
            # ellos); el rate tolera claves extra. STICKER = etiqueta 10×15.
            "settings": {"currency": "MXN", "printFormat": "PDF", "printSize": "STICKER"},
        })

    # ── Operaciones ──
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


class MockAdapter(CarrierAdapter):
    """Simulador en memoria: números MOCK-#### y tracking manipulable.

    Se usa cuando no hay `ENVIA_API_KEY` (dev/demo/tests). `avanzar_estado`
    permite simular el viaje del paquete sin tocar la API real.
    """

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
