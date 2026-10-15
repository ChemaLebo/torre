"""Pedidos por código postal (Chema 2026-10-15): a dónde se envía y cuánto.

Pedidos CREADOS en el periodo que ya salieron (recolectados, en tránsito,
entregados, con entrega presunta, retornados o parcialmente despachados), por
CP de destino: pedidos, paquetes (cajas despachadas; las cajas de reposición
no cuentan, es el "caso ideal"; un pedido que salió entero sin cajas por caja
cuenta como un paquete), ciudad, municipio o alcaldía, estado y zona de cobro.

Mesa ve además lo pagado a 99minutos por los paquetes que viajaron con ellos y
una tarifa ESTIMADA para extrapolar a los CP que 99minutos no ha cotizado:
la real del CP si la hay; si no, la real de su ciudad; si no, el promedio del
estado en la misma zona; si no, el del estado; si no, vacía. "Base de la
estimación" dice cuál aplicó. Solo datos observados en el periodo: nunca una
tarifa inventada.

La ciudad sale del catálogo de localidades por CP (`envios.LocalidadCP`: lo
que Torre ya consultó a envia). Toda la Ciudad de México es UNA ciudad con la
alcaldía por catálogo o por el prefijo del CP. Sin catálogo, la ciudad que
tecleó el comprador en Shopify, limpia (lo de antes de la coma; sin acentos
ni mayúsculas para agrupar). `ciudades.py` reutiliza `acumular` por ciudad.
"""
import re
import unicodedata
from collections import defaultdict

from django.utils import timezone

from apps.envios.models import Guia, LocalidadCP, Paquete
from apps.pedidos.models import Pedido

from .base import dinero

CLAVE = "destinos"
TITULO = "Pedidos por código postal"
DESCRIPCION = (
    "Pedidos creados en el periodo que ya salieron, por código postal de destino: pedidos, paquetes "
    "(cajas despachadas, sin reposiciones), ciudad, municipio y zona de cobro. Mesa ve además lo pagado a "
    "99minutos y una tarifa estimada para los códigos postales que 99minutos no ha cotizado."
)
CON_FECHAS = True
FILTROS = [
    {"nombre": "zona", "etiqueta": "Zona", "tipo": "select", "default": "",
     "opciones": [("", "Todas"), ("local", "Local"), ("metro", "Metro"), ("nacional", "Nacional")]},
]
COLUMNAS = [
    ("CP", "texto"), ("Ciudad", "texto"), ("Municipio / alcaldía", "texto"), ("Estado", "texto"), ("Zona", "texto"),
    ("Pedidos", "entero"), ("Paquetes", "entero"), ("Primer pedido", "fecha"), ("Último pedido", "fecha"),
]
COLUMNAS_MESA = [
    ("Paquetes 99minutos", "entero"), ("Costo 99 promedio", "dinero"), ("Costo 99 total", "dinero"),
    ("Tarifa 99 estimada", "dinero"), ("Base de la estimación", "texto"),
]

ESTADOS_ENVIADOS = (
    Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGA_PRESUNTA, Pedido.ENTREGADO, Pedido.RETORNADO,
    Pedido.PARCIALMENTE_DESPACHADO,
)
CIUDAD_DE_MEXICO = "Ciudad de México"
# En la CDMX los dos primeros dígitos del CP son la alcaldía (SEPOMEX).
ALCALDIAS_CDMX = {
    "01": "Álvaro Obregón", "02": "Azcapotzalco", "03": "Benito Juárez", "04": "Coyoacán",
    "05": "Cuajimalpa de Morelos", "06": "Cuauhtémoc", "07": "Gustavo A. Madero", "08": "Iztacalco",
    "09": "Iztapalapa", "10": "La Magdalena Contreras", "11": "Miguel Hidalgo", "12": "Milpa Alta",
    "13": "Tláhuac", "14": "Tlalpan", "15": "Venustiano Carranza", "16": "Xochimilco",
}
SIN_CP, SIN_CIUDAD = "(sin CP)", "(sin ciudad)"
_NO_VIAJO = (Guia.CANCELADA, Guia.SUSTITUIDA)
_MINUSCULAS = {"de", "del", "la", "las", "los", "y", "el", "en"}


def es_99minutos(guia):
    """Guía que viajó con 99minutos (directo o por el pool de envia)."""
    return guia.proveedor == "99minutos" or guia.carrier == "noventa9Minutos"


def cp_de(pedido):
    """CP de destino a 5 dígitos ("" si no hay): el del pedido o el de la dirección."""
    crudo = pedido.cp or (pedido.direccion or {}).get("zip") or ""
    digitos = "".join(ch for ch in str(crudo) if ch.isdigit())
    return digitos.zfill(5) if 0 < len(digitos) <= 5 else ""


def sin_acentos(texto):
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def limpiar_ciudad(texto):
    """La ciudad tal como la tecleó el comprador, presentable: lo de antes de
    la coma, sin puntuación sobrante y con mayúsculas de nombre propio."""
    base = re.sub(r"\s+", " ", (texto or "").split(",")[0]).strip(" .-")
    palabras = []
    for i, palabra in enumerate(base.lower().split(" ")):
        palabras.append(palabra if (i and palabra in _MINUSCULAS) else palabra[:1].upper() + palabra[1:])
    return " ".join(palabras).strip()


def clave_ciudad(texto):
    """Llave para juntar la misma ciudad escrita de distintas formas (acentos, mayúsculas, coma)."""
    return sin_acentos(limpiar_ciudad(texto)).lower()


def guia_que_viajo(guias):
    """La guía viva más reciente (sin canceladas ni sustituidas), o None."""
    vivas = [g for g in guias if g.estado not in _NO_VIAJO]
    return max(vivas, key=lambda g: g.pk) if vivas else None


def destino(cp, pedido, localidades):
    """Dónde cae el CP: {ciudad, municipio, estado, zona, grupo, de_catalogo,
    clave}. `grupo` es el nombre con el que se agrupa por ciudad (municipio
    del catálogo o la ciudad de Shopify limpia) y `clave` su llave."""
    from apps.envios.cotizador import CP_ESTADO  # lazy: evita ciclo en carga
    from apps.finanzas.services import NOMBRE_ESTADO, zona_de_cp  # lazy por contrato

    direccion = pedido.direccion or {}
    codigo = CP_ESTADO.get(cp[:2]) if cp else None
    estado = NOMBRE_ESTADO.get(codigo, "") if codigo else ""
    if not estado:
        estado = limpiar_ciudad(direccion.get("province") or "")
    zona = (zona_de_cp(cp) or "") if cp else ""
    loc = localidades.get(cp)
    if codigo == "DF":
        alcaldia = (loc.municipio if loc else "") or ALCALDIAS_CDMX.get(cp[:2], "")
        return {"ciudad": CIUDAD_DE_MEXICO, "municipio": alcaldia, "estado": estado, "zona": zona,
                "grupo": CIUDAD_DE_MEXICO, "de_catalogo": True, "clave": (clave_ciudad(CIUDAD_DE_MEXICO), estado)}
    municipio = loc.municipio if loc else ""
    shopify = limpiar_ciudad(direccion.get("city") or "")
    grupo = municipio or shopify or SIN_CIUDAD
    return {"ciudad": (loc.localidad if loc else "") or municipio or shopify, "municipio": municipio,
            "estado": estado, "zona": zona, "grupo": grupo, "de_catalogo": bool(municipio),
            "clave": (clave_ciudad(grupo) if grupo != SIN_CIUDAD else SIN_CIUDAD, estado)}


def acumular(cliente, inicio, fin, filtros):
    """{cp: acumulado} de los pedidos del cliente creados en [inicio, fin)
    que ya salieron, con el filtro de zona aplicado."""
    zona_filtro = (filtros or {}).get("zona") or ""
    pedidos = list(
        Pedido.objects.filter(cliente=cliente, creado__gte=inicio, creado__lt=fin, estado__in=ESTADOS_ENVIADOS)
        .prefetch_related("paquetes__lineas", "paquetes__guias", "guias").order_by("creado", "pk")
    )
    cps = {cp_de(p) for p in pedidos} - {""}
    localidades = {l.cp: l for l in LocalidadCP.objects.filter(cp__in=cps)}
    por_cp = {}
    for pedido in pedidos:
        cp = cp_de(pedido)
        donde = destino(cp, pedido, localidades)
        if zona_filtro and donde["zona"] != zona_filtro:
            continue
        a = por_cp.get(cp)
        if a is None:
            a = por_cp[cp] = {"cp": cp or SIN_CP, **donde, "pedidos": 0, "paquetes": 0, "paquetes_99": 0,
                              "costos": [], "primero": None, "ultimo": None}
        a["pedidos"] += 1
        fecha = timezone.localdate(pedido.creado)
        a["primero"] = fecha if a["primero"] is None else min(a["primero"], fecha)
        a["ultimo"] = fecha if a["ultimo"] is None else max(a["ultimo"], fecha)
        cajas = [c for c in pedido.paquetes.all() if c.estado == Paquete.DESPACHADO and not c.es_reposicion]
        if cajas:
            guias = [guia_que_viajo(c.guias.all()) for c in cajas]
        else:
            guias = [guia_que_viajo([g for g in pedido.guias.all() if g.paquete_id is None])]
        a["paquetes"] += len(guias)
        for guia in guias:
            if guia is not None and es_99minutos(guia):
                a["paquetes_99"] += 1
                if guia.costo_cotizado:
                    a["costos"].append(guia.costo_cotizado)
    return por_cp


def promedio_dinero(costos):
    return dinero(sum(costos) / len(costos)) if costos else None


def total_dinero(costos):
    return dinero(sum(costos)) if costos else None


def promedios_observados(por_cp):
    """Costos de 99minutos juntados por ciudad, por estado y zona, y por estado."""
    por_ciudad, por_estado_zona, por_estado = defaultdict(list), defaultdict(list), defaultdict(list)
    for a in por_cp.values():
        por_ciudad[a["clave"]] += a["costos"]
        if a["estado"]:
            por_estado_zona[(a["estado"], a["zona"])] += a["costos"]
            por_estado[a["estado"]] += a["costos"]
    return por_ciudad, por_estado_zona, por_estado


def estimar(candidatos):
    """(tarifa, base) del primer nivel con costos observados; (None, "") si ninguno."""
    for costos, base in candidatos:
        if costos:
            return promedio_dinero(costos), base
    return None, ""


def generar(cliente, inicio, fin, filtros, es_mesa):
    por_cp = acumular(cliente, inicio, fin, filtros)
    por_ciudad, por_estado_zona, por_estado = promedios_observados(por_cp)
    filas, total = [], {"pedidos": 0, "paquetes": 0, "paquetes_99": 0, "costos": []}
    for cp, a in sorted(por_cp.items(), key=lambda kv: (-kv[1]["pedidos"], -kv[1]["paquetes"], kv[0])):
        fila = [a["cp"], a["ciudad"], a["municipio"], a["estado"], a["zona"], a["pedidos"], a["paquetes"],
                a["primero"], a["ultimo"]]
        if es_mesa:
            tarifa, base = estimar((
                (a["costos"], "real del CP"),
                (por_ciudad[a["clave"]], "real de la ciudad"),
                (por_estado_zona[(a["estado"], a["zona"])], "promedio del estado y zona"),
                (por_estado[a["estado"]], "promedio del estado"),
            ))
            fila += [a["paquetes_99"], promedio_dinero(a["costos"]), total_dinero(a["costos"]), tarifa, base]
        filas.append(fila)
        for campo in ("pedidos", "paquetes", "paquetes_99"):
            total[campo] += a[campo]
        total["costos"] += a["costos"]
    if filas:
        fila = ["Total", "", "", "", "", total["pedidos"], total["paquetes"], None, None]
        if es_mesa:
            fila += [total["paquetes_99"], promedio_dinero(total["costos"]), total_dinero(total["costos"]), None, ""]
        filas.append(fila)
    resumen = [("pedidos", total["pedidos"]), ("paquetes", total["paquetes"]), ("códigos postales", len(por_cp))]
    if es_mesa:
        resumen += [("paquetes por 99minutos", total["paquetes_99"]),
                    ("pagado a 99minutos", total_dinero(total["costos"]) or 0)]
    return {"filas": filas, "resumen": resumen, "grupos": []}
