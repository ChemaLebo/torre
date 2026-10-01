"""Adapters de carrier.

`EnviaAdapter` habla con la API real de envia.com (cuenta con tasas
preferenciales; Paquetexpress como carrier físico preferente para Colima).
`MockAdapter` simula todo en memoria cuando no hay `ENVIA_API_KEY` (dev/demo).
La selección vive en `services.get_adapter()`.
"""

import base64
from datetime import timedelta, timezone as dt_timezone
import hashlib
import itertools
import json
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
    "state": "CX",  # code_2_digits de CDMX: el vocabulario de envia, para todo carrier
    "country": "MX",
    "postalCode": "01780",
}

# Tope general del `content` del bulto; los conectores mas estrictos van en
# TORRE["CONTENIDO_MAX_POR_CARRIER"] (ver _recortar_contenido).
CONTENIDO_MAX_DEFAULT = 120

ESTADOS_CANONICOS = {
    "GUIA_CREADA",
    "RECOLECTADO",
    "EN_TRANSITO",
    "EN_RUTA",
    "ENTREGADO",
    "INTENTO_FALLIDO",
    "RETENIDO",
    "RETORNO",
    "EXCEPCION",
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
    return (
        unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    )


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

    def generar(self, pedido, carrier, servicio, paquete=None, ciudad=None):
        """Genera la guía. Regresa dict: numero, etiqueta_url, costo, raw.
        `ciudad` fuerza la ciudad del destino (reintento con el municipio del
        catálogo cuando el carrier rechazó la localidad; ver services)."""
        raise NotImplementedError

    def cancelar(self, guia):
        """Cancela la guía ante el carrier. Regresa bool."""
        raise NotImplementedError

    def rastrear(self, numero):
        """Regresa dict: estado (canónico o None), descripcion, ts_evento, raw."""
        raise NotImplementedError

    def agendar_recoleccion(self, carrier, fecha, hora_desde, hora_hasta, guias, instrucciones=""):
        """Agenda UNA recolección por todas las guías; regresa {"folio", "costo"}.
        No todo proveedor lo ofrece (99minutos: pickup nativo en la orden)."""
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
            resp = requests.post(
                f"{self.api_base}{ruta}",
                json=payload,
                headers=self._headers(),
                timeout=25,
            )
        except requests.RequestException as exc:
            raise ErrorCarrier(
                f"No se pudo contactar a envia.com ({ruta}): {exc}"
            ) from exc
        return self._json(resp)

    @staticmethod
    def _json(resp):
        if resp.status_code >= 400:
            raise ErrorCarrier(
                f"envia.com respondió {resp.status_code}: {resp.text[:300]}"
            )
        try:
            cuerpo = resp.json()
        except ValueError as exc:
            raise ErrorCarrier("Respuesta de envia.com no es JSON válido") from exc
        if isinstance(cuerpo, dict) and cuerpo.get("error"):
            raise ErrorCarrier(f"envia.com regresó error: {cuerpo['error']}")
        return cuerpo

    # ── Payloads ──
    @staticmethod
    def _origen(carrier=None):
        """Origen del envío. `carrier` se conserva por compatibilidad de firma.

        El state va en code_2_digits ("CX"), el vocabulario propio de envia
        (FAQ, 2026-09) para todos los carriers. Antes viajaba el code_shopify
        "DF" con un override solo para estafeta, que lo rechazaba (1129,
        PED-00015): era el síntoma de este mismo problema.
        """
        return dict(getattr(settings, "ENVIA_ORIGEN", None) or ORIGEN_DEFAULT)

    # Número exterior: los conectores de envia lo piden en SU campo — amPm
    # rechaza con "424 - El numero exterior es requerido" aunque venga en la
    # calle (PED-00030/00034, 2026-09-21). Shopify lo trae dentro de address1.
    _RE_NUMERO_MARCADO = re.compile(
        r"(?:#|\bn[úu]m(?:ero)?\b\.?|\bno\b\.?)\s*:?\s*([0-9]{1,6}(?:-?[A-Za-z]{1,2})?)\b",
        re.IGNORECASE,
    )
    _RE_NUMERO_SUELTO = re.compile(r"(?<![A-Za-z0-9])([0-9]{1,6}(?:-?[A-Za-z]{1,2})?)(?![A-Za-z0-9])")
    _MARCAS_NO_EXTERIOR = frozenset({
        "int", "interior", "depto", "dpto", "departamento", "piso", "local", "oficina",
        "of", "edif", "edificio", "torre", "mz", "manzana", "lt", "lote", "casa", "km",
    })

    @classmethod
    def _numero_exterior(cls, direccion):
        """Número exterior de la dirección, para el campo `number` de envia.

        Manda el dato explícito (`number`) si viene. Si no, se busca en
        address1: primero con marcador ("No. 25", "#45-B", "Núm. 12"); luego
        el ÚLTIMO número suelto que no sea de interior/depto/piso/local ni de
        manzana/lote/km ("Chipre 139" → 139, "5 de Mayo 12 Int 3" → 12,
        "1234 Main St" → 1234). En address2 solo cuenta con marcador (ahí va
        la colonia, y "Sección 2" no es un número de casa). Sin nada, "S/N":
        el conector exige algo y esa es la convención mexicana.
        """
        explicito = str(direccion.get("number") or "").strip()
        if explicito:
            return explicito
        calle = str(direccion.get("address1") or direccion.get("street") or direccion.get("calle") or "")
        marcado = cls._RE_NUMERO_MARCADO.search(calle)
        if marcado:
            return marcado.group(1)
        candidatos = []
        for m in cls._RE_NUMERO_SUELTO.finditer(calle):
            previa = re.search(r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\.?\s*$", calle[: m.start()])
            if previa and previa.group(1).lower() in cls._MARCAS_NO_EXTERIOR:
                continue
            candidatos.append(m.group(1))
        if candidatos:
            return candidatos[-1]
        segunda = str(direccion.get("address2") or "")
        marcado = cls._RE_NUMERO_MARCADO.search(segunda)
        if marcado:
            return marcado.group(1)
        return "S/N"

    @staticmethod
    def _localidad(cp):
        """LocalidadCP del catálogo de envia para el CP; None si no se conoce."""
        if not cp:
            return None
        from .localidades import localidad_por_cp  # lazy: modelo + HTTP
        return localidad_por_cp(cp)

    @classmethod
    def _destino(cls, pedido, ciudad_forzada=None):
        """Bloque destination de envia. `ciudad_forzada` sustituye a la ciudad
        del catálogo: el reintento con el municipio cuando el carrier
        rechazó la localidad (services._reintentar_con_municipio)."""
        from .cotizador import CP_ESTADO, estado_envia  # lazy: la misma tabla que usa el cotizador

        d = pedido.direccion or {}
        cp = str(pedido.cp or d.get("zip") or d.get("postalCode") or "").strip()
        # El province_code de Shopify (o CP_ESTADO para pedidos sin él) está en
        # el vocabulario code_shopify; envia valida con SUS códigos de 2 letras
        # (FAQ), así que se traduce con estado_envia. Los code_shopify de 4-5
        # letras (CHIH, TAMPS...) ni siquiera pasan el esquema de envia
        # ("String is too long", PED-00021) y estafeta rechaza DF (PED-00019/20).
        estado = (
            d.get("province_code")
            or CP_ESTADO.get(cp[:2])
            or d.get("state")
            or d.get("estado", "")
        )
        estado = estado_envia(estado)
        ciudad = d.get("city") or d.get("ciudad", "")
        # Ciudad y estado del catálogo de envia para el CP cuando lo conoce:
        # iMile valida CP↔ciudad contra SU catálogo y rechaza lo que tecleó el
        # comprador ("Zip Code [72830] does not match city [Puebla]",
        # "city [CHETUMAL] not exist"; PED-00030/00034). Sin catálogo, Shopify.
        localidad = cls._localidad(cp)
        if localidad is not None and localidad.localidad:
            ciudad = localidad.localidad
            estado = localidad.estado or estado
        if ciudad_forzada:
            ciudad = ciudad_forzada
        return {
            "name": pedido.comprador_nombre or d.get("name", ""),
            "street": d.get("address1") or d.get("street") or d.get("calle", ""),
            "number": cls._numero_exterior(d),
            "district": d.get("address2") or d.get("colonia", ""),
            "city": ciudad,
            "state": estado,
            "country": "MX",
            "postalCode": cp,
            "phone": pedido.comprador_tel or d.get("phone", ""),
            "email": pedido.comprador_email or d.get("email", ""),
        }

    @staticmethod
    def _recortar_contenido(texto, carrier=None):
        """Descripcion del bulto (`content`) con el tope del conector del carrier.

        TORRE["CONTENIDO_MAX_POR_CARRIER"]: el conector de Estafeta rechaza
        mas de 25 caracteres (400 "size must be between: 1 and 25 chars",
        PED-00018, 2026-09-07); los demas aceptan el tope general de 120. El
        corte respeta el limite de palabra para que la guia no termine a media
        palabra (salvo una sola palabra mas larga que el tope). Nunca vacio:
        sin texto viaja "Mercancia".

        Solo ASCII: acentos y enies se transliteran (RIO, PARAMO, ANEJO) y lo
        que no tenga equivalente se quita. Envia cuenta bytes: "RÍO" recortado
        a 25 caracteres mide 26 y Estafeta lo rechaza; y el conector de iMile
        firma la peticion con el contenido y con un caracter no ASCII la firma
        sale nula ("sign NotNull"). PED-00031, 2026-09-21.
        """
        texto = str(texto or "")
        texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
        texto = " ".join(texto.split())
        topes = settings.TORRE.get("CONTENIDO_MAX_POR_CARRIER") or {}
        tope = int(topes.get(carrier or "", CONTENIDO_MAX_DEFAULT))
        if len(texto) > tope:
            corte = texto[:tope]
            if texto[tope] != " " and " " in corte:
                corte = corte.rsplit(" ", 1)[0]
            texto = corte.rstrip(" ,;:")
        return texto or "Mercancía"

    @staticmethod
    def _paquetes(pedido, paquete=None, carrier=None):
        if paquete is not None:
            lineas = list(paquete.lineas.select_related("linea_pedido__sku"))
            contenido = (
                ", ".join(
                    f"{pl.cantidad}x {(pl.linea_pedido.sku.descripcion or pl.linea_pedido.sku.codigo)}"
                    for pl in lineas
                )[:120]
                or "Mercancía"
            )
            contenido = EnviaAdapter._recortar_contenido(contenido, carrier)
            # El peso real de báscula manda; el plan solo si la caja no se pesó.
            peso_kg = (
                round(paquete.peso_real_gr / 1000.0, 2)
                if paquete.peso_real_gr
                else float(paquete.peso_kg)
            )
            return [
                {
                    "content": contenido,
                    "amount": 1,
                    "type": "box",
                    "weight": peso_kg,
                    "weightUnit": "KG",
                    "lengthUnit": "CM",
                    "dimensions": {
                        "length": paquete.largo_cm,
                        "width": paquete.ancho_cm,
                        "height": paquete.alto_cm,
                    },
                    "declaredValue": float(pedido.valor_declarado or 0),
                }
            ]
        peso_gr = pedido.peso_real_gr or pedido.peso_esperado_gr or 1000
        peso_kg = max(round(peso_gr / 1000.0, 2), 0.1)
        largo, ancho, alto = 30, 25, 20
        contenido = "Mercancía"
        try:
            relacion = (
                pedido.lineas if hasattr(pedido, "lineas") else pedido.lineapedido_set
            )
            skus = [linea.sku for linea in relacion.select_related("sku").all()]
            if skus:
                largo = max(s.largo_cm or largo for s in skus)
                ancho = max(s.ancho_cm or ancho for s in skus)
                alto = max(s.alto_cm or alto for s in skus)
                contenido = (
                    ", ".join(
                        filter(None, ((s.descripcion or s.codigo) for s in skus))
                    )[:120]
                    or contenido
                )
        except (AttributeError, TypeError):
            pass  # pedido sin líneas cargables: se envía con dimensiones default
        contenido = EnviaAdapter._recortar_contenido(contenido, carrier)
        return [
            {
                "content": contenido,
                "amount": 1,
                "type": "box",
                "weight": peso_kg,
                "weightUnit": "KG",
                "lengthUnit": "CM",
                "dimensions": {"length": largo, "width": ancho, "height": alto},
                "declaredValue": float(pedido.valor_declarado or 0),
            }
        ]

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

    def _payload(self, pedido, carrier, servicio, paquete=None, ciudad=None):
        return self._sanear(
            {
                "origin": self._origen(carrier),
                "destination": self._destino(pedido, ciudad_forzada=ciudad),
                "packages": self._paquetes(pedido, paquete=paquete, carrier=carrier),
                "shipment": {"carrier": carrier, "service": servicio, "type": 1},
                # /ship/generate/ exige settings con printFormat y printSize (enum de
                # envia.com); STOCK_4X6 = etiqueta térmica 10×15. El rate tolera extras.
                "settings": {
                    "currency": "MXN",
                    "printFormat": "PDF",
                    "printSize": "STOCK_4X6",
                },
            }
        )

    # ── Operaciones ──
    def agendar_recoleccion(self, carrier, fecha, hora_desde, hora_hasta,
                            guias, instrucciones=""):
        """POST /ship/pickup/: una visita del carrier por TODAS las guías.

        El fee se cobra al balance. El origen va en el code_2_digits de envia
        (CX), el mismo vocabulario que generate y que el ejemplo de su doc.
        """
        peso_total = 0.0
        for g in guias:
            paquete = getattr(g, "paquete", None)
            if paquete is not None and paquete.peso_real_gr:
                peso_total += paquete.peso_real_gr / 1000.0
            elif paquete is not None and paquete.peso_kg:
                peso_total += float(paquete.peso_kg)
            else:
                peso_total += (g.pedido.peso_real_gr or g.pedido.peso_esperado_gr or 1000) / 1000.0
        payload = self._sanear({
            "origin": self._origen(carrier),
            "shipment": {
                "carrier": carrier,
                "type": 1,
                "pickup": {
                    "timeFrom": int(hora_desde),
                    "timeTo": int(hora_hasta),
                    "date": fecha.isoformat(),
                    "instructions": instrucciones or "Recoleccion Local 380 E",
                    "totalPackages": len(guias),
                    "totalWeight": round(peso_total, 2),
                    "weightUnit": "KG",
                    "carrier": carrier,
                    "trackingNumbers": [g.numero for g in guias],
                },
            },
        })
        cuerpo = self._post("/ship/pickup/", payload)
        data = cuerpo.get("data") if isinstance(cuerpo, dict) else None
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = cuerpo if isinstance(cuerpo, dict) else {}
        folio = (data.get("pickupNumber") or data.get("pickup_number")
                 or data.get("confirmationNumber") or data.get("id") or "")
        costo = data.get("totalPrice") or data.get("price")
        return {"folio": str(folio), "costo": costo, "raw": cuerpo}

    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        """Una cotización real por lane. 'No cotiza' es resultado (ok=False), no error."""
        from .cotizador import CP_ESTADO, estado_envia  # lazy: mesa también importa esa tabla de ahí

        largo, ancho, alto = dims or (30, 25, 20)
        payload = {
            "origin": self._origen(carrier),
            "destination": {
                "name": "Cotizacion",
                "email": "alonso@wop.partners",
                "phone": "5500000000",
                "street": "Conocida",
                "number": "1",
                "district": "Centro",
                "city": "Ciudad",
                # 2 letras de envia: con code_shopify de 4-5 letras el rate
                # regresaba "String is too long" y el lane quedaba sin cotizar.
                "state": estado_envia(CP_ESTADO.get(str(cp_destino)[:2], "DF")),
                "country": "MX",
                "postalCode": str(cp_destino),
            },
            "packages": [
                {
                    "content": "Mercancia",
                    "amount": 1,
                    "type": "box",
                    "weight": float(peso_kg),
                    "weightUnit": "KG",
                    "lengthUnit": "CM",
                    "dimensions": {"length": largo, "width": ancho, "height": alto},
                }
            ],
            "shipment": {"carrier": carrier, "type": 1},
            "settings": {"currency": "MXN"},
        }
        try:
            resp = requests.post(
                f"{self.api_base}/ship/rate/",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            datos = resp.json()
        except (requests.RequestException, ValueError):
            return {
                "carrier": carrier,
                "servicio": "",
                "precio": None,
                "estimado": "",
                "ok": False,
            }
        tarifas = datos.get("data")
        if not isinstance(tarifas, list) or not tarifas:
            return {
                "carrier": carrier,
                "servicio": "",
                "precio": None,
                "estimado": "",
                "ok": False,
            }
        mejor = min(tarifas, key=lambda t: t.get("totalPrice", 10**9))
        return {
            "carrier": carrier,
            "servicio": mejor.get("service", ""),
            "precio": Decimal(str(mejor["totalPrice"])),
            "estimado": mejor.get("deliveryEstimate", ""),
            "ok": True,
        }

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        cuerpo = self._post(
            "/ship/rate/", self._payload(pedido, carrier, servicio, paquete=paquete)
        )
        opciones = cuerpo.get("data") or []
        if isinstance(opciones, dict):
            opciones = [opciones]
        if not opciones:
            raise ErrorCarrier(
                f"envia.com no regresó tarifas para {carrier}/{servicio}"
            )
        elegida = next((o for o in opciones if o.get("service") == servicio), None)
        if elegida is None:
            elegida = min(
                opciones,
                key=lambda o: float(o.get("totalPrice") or o.get("total") or 0),
            )
        total = elegida.get("totalPrice") or elegida.get("total") or 0
        return Decimal(str(total)).quantize(Decimal("0.01"))

    def generar(self, pedido, carrier, servicio, paquete=None, ciudad=None):
        cuerpo = self._post(
            "/ship/generate/", self._payload(pedido, carrier, servicio, paquete=paquete, ciudad=ciudad)
        )
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
        self._post(
            "/ship/cancel/", {"carrier": guia.carrier, "trackingNumber": guia.numero}
        )
        return True

    def rastrear(self, numero):
        try:
            resp = requests.get(
                f"{self.queries_base}/guide/{numero}",
                headers=self._headers(),
                timeout=25,
            )
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
        ts_evento = _parsear_fecha(
            ultimo.get("date") or ultimo.get("created_at") or d0.get("lastUpdate")
        )
        return {
            "estado": estado,
            "descripcion": descripcion,
            "ts_evento": ts_evento,
            "raw": d0,
            "eventos": self._historial(eventos),
        }

    @staticmethod
    def _historial(eventos):
        """[{estado, crudo, descripcion, ts, raw}] del historial completo de envia
        (fecha por evento) para EventoGuia; en orden cronológico."""
        historial = []
        for e in eventos:
            if not isinstance(e, dict):
                continue
            crudo = str(e.get("status") or e.get("code") or "")
            descripcion = str(e.get("description") or e.get("event") or crudo)[:300]
            historial.append({
                "estado": normalizar_estado_envia(crudo) or normalizar_estado_envia(descripcion) or "",
                "crudo": crudo[:80], "descripcion": descripcion,
                "ts": _parsear_fecha(e.get("date") or e.get("created_at")), "raw": e,
            })
        historial.sort(key=lambda h: (h["ts"] is None, h["ts"] or 0))
        return historial

    @staticmethod
    def _evento_mas_reciente(eventos):
        limpios = [e for e in eventos if isinstance(e, dict)]
        if not limpios:
            return {}
        con_fecha = [
            (e, _parsear_fecha(e.get("date") or e.get("created_at"))) for e in limpios
        ]
        fechados = [par for par in con_fecha if par[1] is not None]
        if fechados:
            return max(fechados, key=lambda par: par[1])[0]
        return limpios[-1]


# ── 99minutos directo (API v3) ───────────────────────────────────────────────

# Estado numérico de 99minutos → estado canónico de Guia.
CODIGOS_ESTADO_99MIN = {
    1001: "GUIA_CREADA",
    1002: "GUIA_CREADA",  # borrador / confirmada
    2001: "GUIA_CREADA",
    2002: "GUIA_CREADA",  # por recoger / chofer asignado
    2003: "RECOLECTADO",
    2101: "EXCEPCION",  # recogida fallida
    3001: "EN_TRANSITO",
    3002: "EN_TRANSITO",
    3003: "EN_TRANSITO",
    3004: "EN_TRANSITO",
    4001: "EN_RUTA",
    4002: "ENTREGADO",
    4101: "INTENTO_FALLIDO",
    5001: "RETORNO",
    5002: "RETORNO",
    5101: "EXCEPCION",
    8001: "EXCEPCION",
    8002: "EXCEPCION",
    8004: "EXCEPCION",  # robo / perdido / dañado
    8003: "EXCEPCION",  # cancelada fuera de Torre
}

# Enum de deliveryType de POST /api/v3/orders (docs 2026-06). NextDay (NXD)
# existe en tarifas y cobertura pero NO en este enum: no se compra con él.
_DELIVERY_TYPES_99MIN = {"NAL", "SPT", "SMD", "99M", "CO2F", "RET", "TLM", "P2P"}
# Nombres de servicio (de Torre y los que 99minutos regresa al cotizar,
# "Sprint"/"SameDay"...) → código del enum. Lo que no mapea usa el tipo
# configurado (settings.NOVENTA9_DELIVERY_TYPE, hoy SPT).
SERVICIOS_99MIN = {
    "express": "SPT", "sprint": "SPT", "same_day": "SMD", "sameday": "SMD",
    "nacional": "NAL", "99minutos": "99M", "co2free": "CO2F", "retorno": "RET",
}


def delivery_type_99min(servicio):
    """Código de deliveryType para comprar: el del enum si ya viene como
    código, el mapa de nombres si es un nombre conocido, y si no el tipo
    configurado ("ground", vacío, "NextDay"…)."""
    codigo = (servicio or "").strip()
    if codigo.upper() in _DELIVERY_TYPES_99MIN:
        return codigo.upper()
    return SERVICIOS_99MIN.get(codigo.lower().replace(" ", ""), delivery_type_configurado())


def delivery_type_configurado():
    """settings.NOVENTA9_DELIVERY_TYPE validado contra el enum (default SPT)."""
    tipo = str(getattr(settings, "NOVENTA9_DELIVERY_TYPE", "SPT") or "SPT").strip().upper()
    return tipo if tipo in _DELIVERY_TYPES_99MIN else "SPT"


class Adapter99Minutos(CarrierAdapter):
    """API directa de 99minutos (v3): oauth JWT ~1h, /orders + /documents/guides
    (etiqueta zebra 4×6 en BASE64, no URL), rates por par de CP y retrieve de
    shipments. Sandbox con el mismo contrato vía NOVENTA9_API_BASE.
    OJO unidades: 99minutos habla GRAMOS donde envia habla KG."""

    PROVEEDOR = "99minutos"
    PAIS = "MEX"  # enum LocationCountryEnum de la API v3 (MEX, COL, CHL, PER)
    _token_cache = {"token": "", "expira": 0.0}  # compartido entre instancias

    def __init__(self):
        self.base = getattr(
            settings, "NOVENTA9_API_BASE", "https://delivery.99minutos.com"
        ).rstrip("/")
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
            raise ErrorCarrier(
                f"99minutos oauth respondió {resp.status_code}: {resp.text[:200]}"
            )
        cuerpo = resp.json()
        token = cuerpo.get("access_token") or ""
        if not token:
            raise ErrorCarrier("99minutos no regresó access_token")
        cache["token"] = token
        cache["expira"] = time.time() + max(
            int(cuerpo.get("expires_in") or 3599) - 60, 60
        )
        return token

    def _request(self, metodo, ruta, params=None, json_body=None, reintento=True):
        try:
            resp = requests.request(
                metodo,
                f"{self.base}{ruta}",
                params=params,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Accept": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ErrorCarrier(
                f"No se pudo contactar a 99minutos ({ruta}): {exc}"
            ) from exc
        if resp.status_code == 401 and reintento:
            type(self).reiniciar_token()  # JWT vencido: re-auth una sola vez
            return self._request(
                metodo, ruta, params=params, json_body=json_body, reintento=False
            )
        return resp

    @staticmethod
    def _json(resp, ruta):
        if resp.status_code >= 400:
            raise ErrorCarrier(
                f"99minutos respondió {resp.status_code} en {ruta}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ErrorCarrier(f"Respuesta de 99minutos no es JSON ({ruta})") from exc

    @staticmethod
    def _origen_info():
        return dict(getattr(settings, "ENVIA_ORIGEN", None) or ORIGEN_DEFAULT)

    # ── cotización por lane ──
    def _size_para(self, peso_kg, dims):
        return self._size_para_gramos(int(Decimal(str(peso_kg)) * 1000), dims)

    def _size_para_gramos(self, peso_gr, dims):
        """Talla (xs…xxl) de GET /shipping/rates/sizes para peso en gramos y
        medidas en cm; la exige cada item de /orders (2026-09-24)."""
        largo, ancho, alto = dims or (30, 25, 20)
        resp = self._request(
            "GET",
            "/api/v3/shipping/rates/sizes",
            params={"weight": int(peso_gr), "width": ancho, "height": alto, "depth": largo},
        )
        cuerpo = self._json(resp, "/shipping/rates/sizes")
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if isinstance(datos, list) and datos:
            datos = datos[0]
        if isinstance(datos, dict):
            return str(datos.get("size") or datos.get("name") or "")
        return str(datos or "")

    @staticmethod
    def _mejor_tarifa(cuerpo):
        """(precio, servicio, estimado) de la tarifa más barata de la respuesta
        de /shipping/rates/zipcodes (2026-09-24, cuenta real): el total con IVA
        vive en `prices.total`, el servicio en `deliveryType` ("Sprint") y la
        promesa en `eta` (addedDays + dateTime). Se toleran las formas planas
        (totalPrice / deliveryEstimate) de la doc vieja."""
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else cuerpo
        if isinstance(datos, dict):
            datos = [datos]
        if not isinstance(datos, list):
            return None
        opciones = []
        for opcion in datos:
            if not isinstance(opcion, dict):
                continue
            precios = opcion.get("prices") if isinstance(opcion.get("prices"), dict) else {}
            crudo = precios.get("total")
            if crudo is None:
                crudo = (
                    opcion.get("totalPrice")
                    or opcion.get("price")
                    or opcion.get("amount")
                    or opcion.get("total")
                )
            if crudo is None:
                continue
            try:
                precio = Decimal(str(crudo)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                continue
            servicio = delivery_type_99min(str(opcion.get("deliveryType") or opcion.get("service") or ""))
            eta = opcion.get("eta") if isinstance(opcion.get("eta"), dict) else {}
            if eta.get("addedDays") is not None or eta.get("dateTime"):
                dias = eta.get("addedDays")
                fecha = str(eta.get("dateTime") or "")[:10]
                estimado = " · ".join(p for p in (
                    f"{dias} día{'s' if str(dias) != '1' else ''}" if dias is not None else "",
                    f"llega {fecha}" if fecha else "",
                ) if p)
            else:
                estimado = str(opcion.get("deliveryEstimate") or opcion.get("estimatedDelivery") or "")
            opciones.append((precio, servicio, estimado[:60]))
        return min(opciones, key=lambda o: o[0]) if opciones else None

    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        origen_cp = str(self._origen_info().get("postalCode", "01780"))
        sin_cobertura = {
            "carrier": carrier,
            "servicio": "",
            "precio": None,
            "estimado": "",
            "ok": False,
        }
        try:
            size = self._size_para(peso_kg, dims)
            # Sin delivery_type 99minutos responde NextDay, que no se puede
            # comprar (no está en el enum de /orders): se cotiza lo que se compra.
            params = {"delivery_type": delivery_type_configurado()}
            if size:
                params["size"] = size
            resp = self._request(
                "GET",
                f"/api/v3/shipping/rates/zipcodes/{self.PAIS}/{origen_cp}/{self.PAIS}/{cp_destino}",
                params=params,
            )
            if (
                resp.status_code == 412
            ):  # sin cobertura del par de CPs: resultado, no error
                return sin_cobertura
            cuerpo = self._json(resp, "/shipping/rates/zipcodes")
        except ErrorCarrier:
            return sin_cobertura
        mejor = self._mejor_tarifa(cuerpo)
        if mejor is None:
            return sin_cobertura
        precio, servicio, estimado = mejor
        return {
            "carrier": carrier,
            "servicio": servicio,
            "precio": precio,
            "estimado": estimado,
            "ok": True,
        }

    # ── generación ──
    @staticmethod
    def _fisico(pedido, paquete):
        if paquete is not None:
            lineas = list(paquete.lineas.select_related("linea_pedido__sku"))
            contenido = (
                ", ".join(
                    f"{pl.cantidad}x {(pl.linea_pedido.sku.descripcion or pl.linea_pedido.sku.codigo)}"
                    for pl in lineas
                )[:120]
                or "Mercancía"
            )
            # El peso real de báscula manda; el plan solo si la caja no se pesó.
            peso_gr = int(paquete.peso_real_gr or Decimal(str(paquete.peso_kg)) * 1000)
            return (
                peso_gr,
                paquete.largo_cm,
                paquete.ancho_cm,
                paquete.alto_cm,
                contenido,
            )
        peso_gr = int(pedido.peso_real_gr or pedido.peso_esperado_gr or 1000)
        return (peso_gr, 30, 25, 20, "Mercancía")

    @staticmethod
    def _telefono(crudo):
        tel = str(crudo or "").strip().replace(" ", "")
        if not tel:
            return ""
        return tel if tel.startswith("+") else f"+52{tel}"

    def _size_item(self, peso_gr, dims):
        """Talla del item para /orders; "unknown" (valor válido del enum) si
        el endpoint de tallas falla: la talla no debe tumbar la compra."""
        try:
            return self._size_para_gramos(peso_gr, dims) or "unknown"
        except ErrorCarrier:
            return "unknown"

    def _shipment(self, pedido, servicio, paquete, interno):
        bodega = self._origen_info()
        d = pedido.direccion or {}
        nombre = (pedido.comprador_nombre or d.get("name") or "").strip() or "Comprador"
        partes = nombre.split(" ", 1)
        peso_gr, largo, ancho, alto, contenido = self._fisico(pedido, paquete)
        return EnviaAdapter._sanear(
            {
                "internalKey": interno,
                "deliveryType": delivery_type_99min(servicio),
                # El esquema de /orders exige `options`; ahí va pickUpAfter:
                # empacado = listo, disponible para recolección desde YA
                # (+5 min de colchón por relojes de servidor). Las notas van
                # impresas en la etiqueta: el folio ayuda en bodega.
                "options": {
                    "pickUpAfter": (timezone.now() + timedelta(minutes=5)).isoformat(),
                    "notes": interno,
                },
                "sender": {
                    "firstName": bodega.get("company")
                    or bodega.get("name")
                    or "WOP Fulfillment",
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
                    "address": ", ".join(
                        filter(
                            None,
                            [
                                bodega.get("street", ""),
                                bodega.get("district", ""),
                                bodega.get("city", ""),
                            ],
                        )
                    ),
                    "country": self.PAIS,
                    "zipcode": str(bodega.get("postalCode", "")),
                    "city": bodega.get("city", ""),
                },
                "destination": {
                    "address": ", ".join(
                        filter(
                            None,
                            [
                                d.get("address1") or d.get("street") or "",
                                d.get("address2") or "",
                                d.get("city") or "",
                                d.get("province") or "",
                            ],
                        )
                    ),
                    "country": self.PAIS,
                    "zipcode": str(pedido.cp or d.get("zip") or ""),
                    "city": d.get("city") or "",
                },
                "items": [
                    {
                        "size": self._size_item(peso_gr, (largo, ancho, alto)),  # obligatoria en /orders
                        "description": contenido,
                        "weight": peso_gr,  # GRAMOS — no confundir con los KG de envia
                        "length": largo,
                        "width": ancho,
                        "height": alto,
                    }
                ],
            }
        )

    @staticmethod
    def _internal_key(pedido, paquete):
        """internalKey única por caja e intento. 99minutos responde 202 a una
        clave repetida y Torre recupera esa guía (idempotencia ante un timeout);
        pero una guía CANCELADA y recomprada (cambio de dirección, cambio de
        paquetería) necesita clave nueva o recuperaría la cancelada: se agrega
        el número de intento cuando ya hay guías registradas para esa caja."""
        from .models import Guia  # lazy: modelo de la misma app, evita ciclo en carga

        base = f"{pedido.folio}-{paquete.numero if paquete is not None else 1}"
        previas = Guia.objects.filter(pedido=pedido, paquete=paquete).count()
        return base if not previas else f"{base}-r{previas}"

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
        resp = self._request(
            "POST",
            "/api/v3/documents/guides",
            json_body={
                "guides": [
                    {"identifier": str(tracking), "size": "zebra"}
                ],  # zebra = térmica 4×6
            },
        )
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
            raise ErrorCarrier(
                f"El PDF de la guía {tracking} no se pudo decodificar"
            ) from exc
        if not pdf.startswith(b"%PDF"):
            raise ErrorCarrier(f"La etiqueta de {tracking} no es un PDF válido")
        return pdf

    def generar(self, pedido, carrier, servicio, paquete=None, ciudad=None):
        interno = self._internal_key(pedido, paquete)
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
            "raw": {
                "proveedor": "99minutos",
                "trackingId": tracking,
                "internalKey": interno,
            },
        }

    def cancelar(self, guia):
        resp = self._request("DELETE", f"/api/v3/shipments/{guia.numero}")
        self._json(resp, "/shipments (cancel)")
        return True

    def _eventos_tracking(self, numero):
        """events[] de GET /api/v3/shipments/tracking?identifier= (historial con
        createdAt por evento); [] si el endpoint no trae eventos."""
        try:
            resp = self._request("GET", "/api/v3/shipments/tracking", params={"identifier": numero})
            cuerpo = self._json(resp, "/shipments/tracking")
        except ErrorCarrier:
            return []
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else None
        if isinstance(datos, list):
            datos = datos[0] if datos else {}
        eventos = (datos or {}).get("events") if isinstance(datos, dict) else None
        historial = []
        for e in eventos or []:
            if not isinstance(e, dict):
                continue
            try:
                codigo = int(e.get("statusCode"))
            except (TypeError, ValueError):
                codigo = None
            nombre = str(e.get("statusName") or "")
            comentario = str((e.get("data") or {}).get("comment") or "") if isinstance(e.get("data"), dict) else ""
            historial.append({
                "estado": CODIGOS_ESTADO_99MIN.get(codigo, "") if codigo is not None else "",
                "crudo": str(e.get("statusCode") or nombre)[:80],
                "descripcion": (f"{nombre} · {comentario}" if comentario else nombre)[:300],
                "ts": _parsear_fecha(e.get("createdAt") or e.get("created_at")), "raw": e,
            })
        historial.sort(key=lambda h: (h["ts"] is None, h["ts"] or 0))
        return historial

    def rastrear(self, numero):
        """Historial de /shipments/tracking (estado y hora del último evento) y,
        si viene vacío, el estado del shipment como antes."""
        historial = self._eventos_tracking(numero)
        if historial:
            ultimo = historial[-1]
            return {
                "estado": ultimo["estado"] or normalizar_estado_envia(ultimo["descripcion"]),
                "descripcion": ultimo["descripcion"],
                "ts_evento": ultimo["ts"],
                "raw": ultimo["raw"],
                "eventos": historial,
            }
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
        return {
            "estado": estado,
            "descripcion": descripcion,
            "ts_evento": ts_evento,
            "raw": datos,
        }

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
MOCK_PUNTOPOST_PREFIJOS = {
    "06",
    "44",
    "64",
    "91",
    "72",
    "76",
    "01",
    "02",
    "03",
    "45",
    "66",
    "67",
}
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
    "puntopost": "5-7 días",
    "estafeta": "2-3 días",
    "paquetexpress": "1-2 días",
    "fedex": "1-2 días",
    "noventa9Minutos": "2-4 días",
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


# ── iMile directo (API v3, openapi.imile.com; doc bajada a docs/imile-api/) ──
# Estado de rastreo de iMile (latestStatus / locusType de /client/track) →
# canónico de Guia. El diccionario oficial NO está en su portal (2026-09-25):
# estos nombres salen de los ejemplos de su doc y de sus integradores; lo que
# no mapea cae al texto del evento (normalizar_estado_envia). Confirmar con
# iMile al recibir credenciales.
ESTADOS_IMILE_EXACTOS = {
    "submitorder": "GUIA_CREADA", "submitted": "GUIA_CREADA", "ordercreated": "GUIA_CREADA",
    "pickedup": "RECOLECTADO", "pickup": "RECOLECTADO", "collected": "RECOLECTADO", "picked": "RECOLECTADO",
    "arrive": "EN_TRANSITO", "arrived": "EN_TRANSITO", "depart": "EN_TRANSITO", "departed": "EN_TRANSITO",
    "intransit": "EN_TRANSITO", "transit": "EN_TRANSITO", "shipped": "EN_TRANSITO", "sorting": "EN_TRANSITO",
    "outfordelivery": "EN_RUTA", "delivering": "EN_RUTA", "dispatched": "EN_RUTA",
    "delivered": "ENTREGADO", "signed": "ENTREGADO", "pod": "ENTREGADO",
    "deliveryfailed": "INTENTO_FALLIDO", "failed": "INTENTO_FALLIDO", "ndr": "INTENTO_FALLIDO", "rejected": "INTENTO_FALLIDO",
    "returnarrive": "RETORNO", "returned": "RETORNO", "returndelivered": "RETORNO", "rts": "RETORNO",
    "cancelorder": "EXCEPCION", "cancelled": "EXCEPCION", "canceled": "EXCEPCION", "lost": "EXCEPCION", "damaged": "EXCEPCION",
    "onhold": "RETENIDO", "hold": "RETENIDO",
}
# Contención, en orden de prioridad (lo específico antes que lo genérico).
_ESTADOS_IMILE_CONTIENEN = [
    ("cancel", "EXCEPCION"), ("lost", "EXCEPCION"), ("damage", "EXCEPCION"),
    ("return", "RETORNO"), ("rts", "RETORNO"),
    ("fail", "INTENTO_FALLIDO"), ("ndr", "INTENTO_FALLIDO"), ("reject", "INTENTO_FALLIDO"), ("notdeliver", "INTENTO_FALLIDO"),
    ("hold", "RETENIDO"),
    ("delivered", "ENTREGADO"), ("signed", "ENTREGADO"), ("pod", "ENTREGADO"),
    ("outfordelivery", "EN_RUTA"), ("delivering", "EN_RUTA"), ("dispatch", "EN_RUTA"),
    ("pick", "RECOLECTADO"), ("collect", "RECOLECTADO"),
    ("arriv", "EN_TRANSITO"), ("depart", "EN_TRANSITO"), ("transit", "EN_TRANSITO"), ("sort", "EN_TRANSITO"), ("ship", "EN_TRANSITO"),
    ("submit", "GUIA_CREADA"), ("created", "GUIA_CREADA"),
]
_TOKEN_IMILE_INVALIDO = {"402", "407", "408"}
VALOR_DECLARADO_COTIZACION = 500.0  # MXN: valor de referencia para cotizar un lane sin pedido


def normalizar_estado_imile(estado, tipo="", detalle=""):
    """latestStatus de iMile → estado canónico de Guia (None si no se reconoce)."""
    clave = re.sub(r"[^a-z]", "", str(estado or "").lower())
    if clave in ESTADOS_IMILE_EXACTOS:
        return ESTADOS_IMILE_EXACTOS[clave]
    for fragmento, canon in _ESTADOS_IMILE_CONTIENEN:
        if fragmento in clave:
            return canon
    if str(tipo or "").lower() == "cancelorder":
        return "EXCEPCION"
    return normalizar_estado_envia(str(estado or "")) or normalizar_estado_envia(str(detalle or ""))


def _parsear_fecha_imile(texto, zona=None):
    """"yyyy-MM-dd HH:mm:ss" de iMile + su zona ("GMT-06:00", "-6", "+8") →
    datetime aware con la hora EXACTA del carrier (no la del poll); sin zona
    se asume la de Torre. None si no parsea."""
    texto = str(texto or "").strip()
    if not texto:
        return None
    dt = parse_datetime(texto) or parse_datetime(texto.replace(" ", "T"))
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return dt
    m = re.search(r"([+-])\s*(\d{1,2})(?::?(\d{2}))?", str(zona or ""))
    if m:
        signo = 1 if m.group(1) == "+" else -1
        desfase = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return dt.replace(tzinfo=dt_timezone(signo * desfase))
    return timezone.make_aware(dt, timezone.get_default_timezone())


def _nombre_estado_cp(cp):
    """Nombre del estado por prefijo de CP ("Ciudad de México", "Jalisco") para
    los campos `province` de iMile; "" si no se infiere."""
    from apps.finanzas.services import NOMBRE_ESTADO  # lazy por contrato

    from .cotizador import CP_ESTADO  # lazy: evita ciclo en carga

    cp = str(cp or "").strip()
    codigo = CP_ESTADO.get(cp[:2]) if len(cp) >= 2 else None
    return NOMBRE_ESTADO.get(codigo, "") if codigo else ""


def _decimal_imile(valor):
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01")) if valor is not None else None
    except (InvalidOperation, ValueError):
        return None


class ErrorImile(ErrorCarrier):
    """ErrorCarrier con el `code` de la API de iMile (30001 orderNo duplicado,
    407 token inválido, 40025 ciudad inexistente…)."""

    def __init__(self, mensaje, codigo=""):
        super().__init__(mensaje)
        self.codigo = str(codigo or "")


class AdapterImile(CarrierAdapter):
    """API directa de iMile (openapi v3; doc 2026-09-25 en docs/imile-api/, con
    la firma validada contra su ejemplo). Todo es POST JSON con un sobre común
    (customerId, signMethod, format, version, timestamp, timeZone, accessToken)
    firmado con la secretKey: MD5/SHA256 en mayúsculas de secretKey + llaves
    ordenadas ASCII con su valor + el JSON COMPACTO de `param` + secretKey.
    Token de 2 h (/auth/accessToken/grant); createOrder regresa la guía
    (expressNo) y la etiqueta 6×4 en base64; track por guía con la hora y la
    zona del carrier; cancelación solo antes de la recolección. Unidades: kg
    y cm (volumen en cm³). Sandbox con el mismo contrato vía IMILE_API_BASE.
    Lo que falta confirmar con iMile: logisticsProductCode de la cuenta,
    diccionario de estados de track y si RFC/CURP aplican a envíos domésticos."""

    PROVEEDOR = "imile"
    PAIS = "MEX"
    VERSION = "1.0.0"
    ORDER_TYPE_ENVIO = "100"
    SERVICIO = "standard"
    _token_cache = {"token": "", "expira": 0.0}  # compartido entre instancias

    def __init__(self):
        self.base = str(getattr(settings, "IMILE_API_BASE", "https://openapi.imile.com") or "").rstrip("/")
        credencial = getattr(settings, "IMILE_API_KEY", "") or ""
        self.customer_id, _, self.secret = credencial.partition(":")
        self.sign_method = (getattr(settings, "IMILE_SIGN_METHOD", "MD5") or "MD5").upper()
        self.time_zone = str(getattr(settings, "IMILE_TIME_ZONE", "-6") or "-6")
        self.product_code = getattr(settings, "IMILE_PRODUCT_CODE", "") or ""

    @classmethod
    def reiniciar_token(cls):
        cls._token_cache = {"token": "", "expira": 0.0}

    # ── sobre común, firma y transporte ──
    def firmar(self, comunes, param_json):
        """Firma del sobre: hash(secretKey + Σ llave+valor en orden ASCII (sin
        `param` ni `sign`) + JSON compacto de param + secretKey), en MAYÚSCULAS."""
        cadena = self.secret + "".join(f"{k}{comunes[k]}" for k in sorted(comunes)) + param_json + self.secret
        algoritmo = hashlib.sha256 if self.sign_method == "SHA256" else hashlib.md5
        return algoritmo(cadena.encode("utf-8")).hexdigest().upper()

    def _comunes(self, con_token):
        comunes = {
            "customerId": self.customer_id, "signMethod": self.sign_method, "format": "json",
            "version": self.VERSION, "timestamp": str(int(time.time() * 1000)), "timeZone": self.time_zone,
        }
        if con_token:
            comunes["accessToken"] = self._token()
        return comunes

    def _llamar(self, ruta, param, con_token=True, reintento=True):
        """POST firmado. Regresa el cuerpo con code "200"; un token vencido o
        inválido (402/407/408) re-autentica UNA vez; otro code → ErrorImile."""
        comunes = self._comunes(con_token)
        # El JSON de `param` se firma tal cual viaja: mismos separadores y orden.
        param_json = json.dumps(param, separators=(",", ":"), ensure_ascii=False)
        cuerpo = dict(comunes)
        cuerpo["sign"] = self.firmar(comunes, param_json)
        cuerpo["param"] = param
        datos = json.dumps(cuerpo, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            resp = requests.post(
                f"{self.base}{ruta}", data=datos,
                headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ErrorCarrier(f"No se pudo contactar a iMile ({ruta}): {exc}") from exc
        if resp.status_code != 200:
            raise ErrorImile(f"iMile respondió HTTP {resp.status_code} en {ruta}: {resp.text[:300]}", codigo=str(resp.status_code))
        try:
            respuesta = resp.json()
        except ValueError as exc:
            raise ErrorCarrier(f"Respuesta de iMile no es JSON ({ruta})") from exc
        codigo = str(respuesta.get("code") or "")
        if codigo == "200":
            return respuesta
        if codigo in _TOKEN_IMILE_INVALIDO and con_token and reintento:
            type(self).reiniciar_token()
            return self._llamar(ruta, param, con_token=True, reintento=False)
        raise ErrorImile(f"iMile {codigo} en {ruta}: {respuesta.get('message') or 'sin mensaje'}", codigo=codigo)

    def _token(self):
        cache = type(self)._token_cache
        if cache["token"] and cache["expira"] > time.time():
            return cache["token"]
        respuesta = self._llamar("/auth/accessToken/grant", {"grantType": "clientCredential"}, con_token=False, reintento=False)
        datos = respuesta.get("data") or {}
        token = str(datos.get("accessToken") or "")
        if not token:
            raise ErrorCarrier("iMile no regresó accessToken")
        cache["token"] = token
        cache["expira"] = time.time() + max(int(datos.get("expiresIn") or 7200) - 120, 60)
        return token

    # ── partes del envío ──
    @staticmethod
    def _origen_info():
        return dict(getattr(settings, "ENVIA_ORIGEN", None) or ORIGEN_DEFAULT)

    def _remitente(self):
        b = self._origen_info()
        return {
            "contacts": (b.get("name") or b.get("company") or "WOP Fulfillment")[:50],
            "contactCompany": b.get("company") or "",
            "phone": Adapter99Minutos._telefono(b.get("phone")),
            "email": b.get("email") or "",
            "addressType": "warehouse",
            "country": self.PAIS,
            "province": _nombre_estado_cp(b.get("postalCode")) or "Ciudad de México",
            "city": b.get("city") or "",
            "zipCode": str(b.get("postalCode") or ""),
            "street": b.get("street") or "",
            "externalNo": str(b.get("number") or ""),
            "address": ", ".join(filter(None, [b.get("street"), b.get("district"), b.get("city")])),
        }

    @staticmethod
    def _localidad(cp):
        try:
            from .localidades import localidad_por_cp  # lazy: modelo + HTTP al catálogo de envia
            return localidad_por_cp(cp)
        except Exception:  # noqa: BLE001 — sin catálogo se manda lo de Shopify
            return None

    def _destinatario(self, pedido, ciudad_forzada=None):
        """consigneeInfo: la ciudad del catálogo por CP (iMile valida el par
        CP↔ciudad; `ciudad_forzada` = el municipio del reintento dirigido),
        el estado por prefijo de CP y el número exterior separado."""
        d = pedido.direccion or {}
        cp = str(pedido.cp or d.get("zip") or "").strip()
        localidad = self._localidad(cp)
        ciudad = (ciudad_forzada or (localidad.localidad if localidad else "") or d.get("city") or "").strip()
        provincia = _nombre_estado_cp(cp) or str(d.get("province") or "")
        nombre = (pedido.comprador_nombre or d.get("name") or "Comprador").strip()
        return {
            "contacts": nombre[:50],
            "phone": Adapter99Minutos._telefono(pedido.comprador_tel or d.get("phone")),
            "email": pedido.comprador_email or d.get("email") or "",
            "addressType": "customer",
            "country": self.PAIS,
            "province": provincia,
            "city": ciudad,
            "zipCode": cp,
            "street": str(d.get("address1") or "")[:100],
            "externalNo": EnviaAdapter._numero_exterior(d),
            "address": ", ".join(filter(None, [d.get("address1"), d.get("address2"), ciudad, provincia, cp]))[:200],
            "address2": str(d.get("address2") or "")[:100],
        }

    @staticmethod
    def _lineas(pedido, paquete):
        """[(sku, cantidad)] de la caja (PaqueteLinea) o de lo que surte el pedido."""
        if paquete is not None:
            return [(pl.linea_pedido.sku, pl.cantidad) for pl in paquete.lineas.select_related("linea_pedido__sku")]
        return [(l.sku, l.pendiente) for l in pedido.lineas_por_surtir]

    def _valor_declarado(self, pedido, paquete):
        total = sum(float(sku.precio_declarado or 0) * cantidad for sku, cantidad in self._lineas(pedido, paquete))
        if paquete is None or total <= 0:
            return float(pedido.valor_declarado or 0) or total
        return total

    def _paquete_info(self, pedido, paquete):
        peso_gr, largo, ancho, alto, _contenido = Adapter99Minutos._fisico(pedido, paquete)
        largo, ancho, alto = (int(x or 0) for x in (largo, ancho, alto))
        return {
            "paymentMethod": "PPD", "collectingMoney": 0,
            "clientDeclaredValue": round(self._valor_declarado(pedido, paquete), 2), "clientDeclaredCurrency": "Local",
            "goodsType": "Normal", "isValuables": 0,
            "length": largo, "width": ancho, "high": alto,
            "totalVolume": (largo * ancho * alto) or 1,
            "grossWeight": round(max(int(peso_gr), 1) / 1000, 3), "totalCount": 1,
        }

    def _skus(self, pedido, paquete):
        hs = getattr(settings, "IMILE_HS_CODE_DEFAULT", "") or ""
        skus = []
        for sku, cantidad in self._lineas(pedido, paquete):
            nombre = (sku.descripcion or sku.codigo)[:50]
            item = {
                "skuNo": sku.codigo[:50], "skuName": nombre, "skuLocalName": nombre,
                "skuQty": max(int(cantidad or 0), 1),
                "skuDeclaredValue": round(float(sku.precio_declarado or 0), 2),
                "skuWeight": round(max(int(sku.peso_gr or 0), 1) / 1000, 3),
            }
            if hs:
                item["skuHsCode"] = hs  # "Customs Code (Required for Mexico)": confirmar si aplica a doméstico
            skus.append(item)
        if not skus:
            skus.append({"skuNo": "MERCANCIA", "skuName": "Mercancía", "skuLocalName": "Mercancía", "skuQty": 1,
                         "skuDeclaredValue": round(float(pedido.valor_declarado or 0), 2), "skuWeight": 1.0})
        return skus

    def _estimado(self):
        dias = str(getattr(settings, "IMILE_DIAS_PROMESA", "") or "").strip()
        return f"{dias} día{'s' if dias != '1' else ''}" if dias.isdigit() else ""

    # ── cotización ──
    def _param_tarifa(self, cp_destino, ciudad_destino, peso_kg, dims, valor):
        largo, ancho, alto = (int(x or 0) for x in (dims or (30, 25, 20)))
        origen = self._origen_info()
        return {
            "senderInfo": {"country": self.PAIS, "province": _nombre_estado_cp(origen.get("postalCode")) or "",
                           "city": origen.get("city") or "", "zipCode": str(origen.get("postalCode") or "")},
            "consigneeInfo": {"country": self.PAIS, "province": _nombre_estado_cp(cp_destino) or "",
                              "city": ciudad_destino or "", "zipCode": str(cp_destino)},
            "orderType": self.ORDER_TYPE_ENVIO, "paymentMethod": "PPD", "goodsType": "Normal",
            "totalWeight": round(float(peso_kg), 3), "totalVolume": (largo * ancho * alto) or 1,
            "clientDeclaredValue": round(float(valor), 2), "clientDeclaredCurrency": "Local",
        }

    def cotizar_lane(self, carrier, cp_destino, peso_kg, dims=None):
        sin_cobertura = {"carrier": carrier, "servicio": "", "precio": None, "estimado": "", "ok": False}
        localidad = self._localidad(cp_destino)
        param = self._param_tarifa(cp_destino, localidad.localidad if localidad else "", peso_kg, dims, VALOR_DECLARADO_COTIZACION)
        try:
            datos = self._llamar("/client/order/calShippingFee", param).get("data") or {}
        except ErrorCarrier as exc:
            return {**sin_cobertura, "detalle": str(exc)[:200]}  # sin cobertura o sin cuenta: resultado, no excepción
        precio = _decimal_imile(datos.get("totalAmount"))
        if precio is None:
            return sin_cobertura
        return {"carrier": carrier, "servicio": self.SERVICIO, "precio": precio, "estimado": self._estimado(), "ok": True}

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        peso_gr, largo, ancho, alto, _ = Adapter99Minutos._fisico(pedido, paquete)
        destino = self._destinatario(pedido)
        param = self._param_tarifa(destino["zipCode"], destino["city"], max(int(peso_gr), 1) / 1000, (largo, ancho, alto),
                                   self._valor_declarado(pedido, paquete))
        datos = self._llamar("/client/order/calShippingFee", param).get("data") or {}
        precio = _decimal_imile(datos.get("totalAmount"))
        if precio is None:
            raise ErrorCarrier(f"iMile no regresó tarifa para {pedido.folio} (CP {destino['zipCode']})")
        return precio

    # ── generación ──
    @staticmethod
    def _pdf(b64, numero):
        if not b64:
            raise ErrorCarrier(f"iMile no regresó la etiqueta (imileAwb) de la guía {numero}")
        try:
            pdf = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise ErrorCarrier(f"La etiqueta de {numero} no se pudo decodificar") from exc
        if not pdf.startswith(b"%PDF"):
            raise ErrorCarrier(f"La etiqueta de {numero} no es un PDF válido")
        return pdf

    def generar(self, pedido, carrier, servicio, paquete=None, ciudad=None):
        if not self.product_code:
            raise ErrorCarrier("iMile: falta IMILE_PRODUCT_CODE (el logisticsProductCode que asigna iMile a la cuenta).")
        orden = Adapter99Minutos._internal_key(pedido, paquete)  # única por caja e intento
        param = {
            "orderNo": orden, "orderType": self.ORDER_TYPE_ENVIO,
            "serviceInfo": {"logisticsProductCode": self.product_code, "pickupService": 0, "deliveryService": "Delivery"},
            "packageInfo": self._paquete_info(pedido, paquete),
            "skuInfos": self._skus(pedido, paquete),
            "senderInfo": self._remitente(),
            "consigneeInfo": self._destinatario(pedido, ciudad_forzada=ciudad),
        }
        try:
            datos = self._llamar("/client/order/v2/createOrder", param).get("data") or {}
        except ErrorImile as exc:
            if exc.codigo != "30001":
                raise
            # orderNo duplicado (un timeout previo): la guía ya existe allá;
            # se recupera con su etiqueta en vez de comprar otra.
            datos = self._llamar("/client/order/reprintOrder", {"orderCode": orden, "orderCodeType": "2"}).get("data") or {}
        numero = str(datos.get("expressNo") or "")
        if not numero:
            raise ErrorCarrier("iMile no regresó expressNo (número de guía)")
        return {
            "numero": numero,
            "etiqueta_url": "",
            "etiqueta_pdf": self._pdf(datos.get("imileAwb"), numero),
            "costo": None,  # el rate ya vive en el plan (precio_cotizado)
            "raw": {"proveedor": "imile", "expressNo": numero, "orderNo": orden,
                    "subWaybillNo": datos.get("subWaybillNo") or []},
        }

    def cancelar(self, guia):
        """Solo antes de la recolección (después es por su portal); el orderCode
        es nuestro orderNo, guardado en raw al generar."""
        orden = (guia.raw or {}).get("orderNo") or Adapter99Minutos._internal_key(guia.pedido, guia.paquete).rsplit("-r", 1)[0]
        self._llamar("/client/order/deleteOrder", {"orderCode": orden, "waybillNo": guia.numero})
        return True

    # ── rastreo ──
    def rastrear(self, numero):
        """/client/track/getOne por número de guía, en español: historial
        completo (locus) con la hora y la zona que reporta iMile."""
        respuesta = self._llamar("/client/track/getOne", {"orderType": "1", "language": "3", "orderNo": str(numero)})
        datos = respuesta.get("data") or {}
        if isinstance(datos, list):
            datos = datos[0] if datos else {}
        historial = []
        for e in datos.get("locus") or []:
            if not isinstance(e, dict):
                continue
            sitio = str(e.get("latestSite") or "").strip()
            detalle = str(e.get("locusDetailed") or e.get("latestStatus") or "").strip()
            historial.append({
                "estado": normalizar_estado_imile(e.get("latestStatus"), e.get("locusType"), detalle) or "",
                "crudo": str(e.get("latestStatus") or e.get("locusType") or "")[:80],
                "descripcion": (f"{detalle} · {sitio}" if sitio and sitio not in detalle else detalle)[:300],
                "ts": _parsear_fecha_imile(e.get("latestStatusTime"), e.get("timeZone") or datos.get("timeZone")),
                "raw": e,
            })
        historial.sort(key=lambda h: (h["ts"] is None, h["ts"] or 0))
        estado = normalizar_estado_imile(datos.get("latestStatus"), datos.get("locusType"))
        if estado is None and historial:
            estado = historial[-1]["estado"] or None
        ultimo = " · ".join(filter(None, [str(datos.get("latestStatus") or ""), str(datos.get("latestSite") or "")]))
        return {
            "estado": estado,
            "descripcion": (ultimo or (historial[-1]["descripcion"] if historial else ""))[:300],
            "ts_evento": _parsear_fecha_imile(datos.get("latestStatusTime"), datos.get("timeZone"))
                         or (historial[-1]["ts"] if historial else None),
            "raw": datos,
            "eventos": historial,
        }

    # ── recolección ──
    def agendar_recoleccion(self, carrier, fecha, hora_desde, hora_hasta, guias, instrucciones=""):
        """/order/pick/notify: una visita por todas las guías (solo guías sin
        chofer asignado, día hábil, ventana 09:00-18:00 hora local)."""
        respuesta = self._llamar("/order/pick/notify", {
            "waybillNos": [g.numero for g in guias], "pickDate": fecha.isoformat(),
            "pickStart": f"{int(hora_desde):02d}:00", "pickEnd": f"{int(hora_hasta):02d}:00", "returnBatchNo": True,
        })
        datos = respuesta.get("data")
        folio = datos.get("batchNo") if isinstance(datos, dict) else datos
        return {"folio": str(folio or ""), "costo": None}


class MockAdapter(CarrierAdapter):
    """Simulador en memoria: números MOCK-#### y tracking manipulable.

    Se usa cuando no hay `ENVIA_API_KEY` (dev/demo/tests). `avanzar_estado`
    permite simular el viaje del paquete sin tocar la API real.
    """

    PROVEEDOR = "mock"

    SECUENCIA_FELIZ = [
        "GUIA_CREADA",
        "RECOLECTADO",
        "EN_TRANSITO",
        "EN_RUTA",
        "ENTREGADO",
    ]
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
            return {
                "carrier": carrier,
                "servicio": "",
                "precio": None,
                "estimado": "",
                "ok": False,
            }
        prefijo = str(cp_destino)[:2]
        if carrier == "puntopost" and (
            prefijo not in MOCK_PUNTOPOST_PREFIJOS
            or Decimal(str(peso_kg)) > MOCK_PUNTOPOST_MAX_KG
        ):
            return {
                "carrier": carrier,
                "servicio": "",
                "precio": None,
                "estimado": "",
                "ok": False,
            }
        return {
            "carrier": carrier,
            "servicio": "mock",
            "precio": _interpolar(tabla, peso_kg),
            "estimado": MOCK_ESTIMADOS.get(carrier, ""),
            "ok": True,
        }

    def cotizar(self, pedido, carrier, servicio, paquete=None):
        if paquete is not None and paquete.precio_cotizado:
            return Decimal(paquete.precio_cotizado)
        return (
            Decimal("65.00")
            if getattr(pedido, "es_local", False)
            else Decimal("118.00")
        )

    def agendar_recoleccion(self, carrier, fecha, hora_desde, hora_hasta,
                            guias, instrucciones=""):
        folio = f"PU-MOCK-{next(self._consecutivo):04d}"
        return {"folio": folio, "costo": Decimal("85.00"), "raw": {"mock": True}}

    def generar(self, pedido, carrier, servicio, paquete=None, ciudad=None):
        numero = f"MOCK-{next(self._consecutivo):04d}"
        while numero in self._registro:
            numero = f"MOCK-{next(self._consecutivo):04d}"
        self._registro[numero] = "GUIA_CREADA"
        if paquete is not None and paquete.precio_cotizado:
            costo = Decimal(paquete.precio_cotizado)
        else:
            costo = (
                self.cotizar(pedido, carrier, servicio) * Decimal("0.82")
            ).quantize(Decimal("0.01"))
        return {
            "numero": numero,
            "etiqueta_url": f"https://etiquetas.mock/{numero}.pdf",
            "costo": costo,
            "raw": {
                "mock": True,
                "carrier": carrier,
                "service": servicio,
                "trackingNumber": numero,
            },
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
                estado = self.SECUENCIA_FELIZ[
                    min(idx + 1, len(self.SECUENCIA_FELIZ) - 1)
                ]
            except ValueError:
                estado = "EN_TRANSITO"
        if estado not in ESTADOS_CANONICOS:
            raise ValueError(f"Estado no canónico: {estado}")
        self._registro[numero] = estado
        return estado
