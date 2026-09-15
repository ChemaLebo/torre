"""Reparto de carriers por porcentajes (integración "reparto", por cliente).

Objetivo: que un cliente mande, p. ej., 75 % de sus pedidos por un carrier y
25 % por otro para medir incidencias por carrier y ajustar después. Plan
cerrado con Chema el 2026-09-14 (DEUDA-TECNICA.md).

Cómo reparte: una "baraja" por bloque. El bloque tiene `tam` cartas con los
carriers en la proporción exacta de los pesos (75/25 → 4 cartas: 3 y 1); se
baraja con semilla fija y cada pedido elegible saca la siguiente carta. Al
cerrar cada bloque el reparto es exacto; a media baraja puede ir desviado.

Sin tablas ni consultas que crezcan con la historia: `Cliente.reparto_cursor`
(N del siguiente pedido) se toma bajo `select_for_update` y se incrementa;
bloque y posición salen de `divmod(n - reparto_base, tam)` y la baraja del
bloque se recalcula en memoria (≤ REPARTO_BLOQUE_MAX cartas). La carta queda
en `Pedido.reparto_carrier` para que todas las llamadas a elegir_carrier del
mismo pedido coincidan, y en la auditoría (`reparto_carrier`) para el reporte.

Sin handover: si el carrier de la carta falla al cotizar o generar, el pedido
queda sin plan/guía visible en Mesa. La diferencia entre cartas asignadas y
guías efectivas ES la tasa de fallo de la integración y se quiere ver.
"""
import hashlib
import random
from fractions import Fraction

from django.conf import settings
from django.db import transaction

from apps.core.services import registrar_evento

ACCION_CARTA = "reparto_carrier"


def bloque_max():
    """Tope de cartas por bloque (TORRE["REPARTO_BLOQUE_MAX"], default 100)."""
    return int(settings.TORRE.get("REPARTO_BLOQUE_MAX", 100))


def carriers_elegibles():
    """Carriers que la ficha ofrece para repartir: la lista blanca de
    cotización más los que tienen integración directa (99minutos)."""
    return list(dict.fromkeys(list(settings.TORRE["CARRIERS_COTIZAR"]) + ["noventa9Minutos"]))


def _fracciones(pesos):
    """{carrier: Fraction(peso/100)} solo de los pesos > 0, en orden de carrier
    (el orden fijo es parte de la reproducibilidad de la baraja)."""
    limpio = {}
    for carrier in sorted(pesos):
        fraccion = Fraction(str(pesos[carrier])) / 100
        if fraccion > 0:
            limpio[carrier] = fraccion
    return limpio


def tamano_bloque(pesos, tope=None):
    """Cartas por bloque: el mínimo que representa los pesos exactos (75/25 →
    4, 70/30 → 10, 92/8 → 25); si excede el tope se usa el tope y los pesos
    se redondean (ver cartas_por_carrier)."""
    tope = bloque_max() if tope is None else tope
    fracciones = _fracciones(pesos)
    if not fracciones:
        return 0
    denominador = 1
    for fraccion in fracciones.values():
        denominador = denominador * fraccion.denominator // _mcd(denominador, fraccion.denominator)
    return min(denominador, tope)


def _mcd(a, b):
    while b:
        a, b = b, a % b
    return a


def cartas_por_carrier(pesos, tam):
    """{carrier: cartas} en un bloque de `tam`. Exacto cuando `tam` es el
    mínimo; con tope, redondeo por mayor residuo para que la suma sea `tam`
    (33.33/33.33/33.34 en 100 → 33/33/34, jamás 99 ni 101)."""
    fracciones = _fracciones(pesos)
    if not fracciones or not tam:
        return {}
    exactas = {c: f * tam for c, f in fracciones.items()}
    cartas = {c: int(v) for c, v in exactas.items()}
    faltan = tam - sum(cartas.values())
    for carrier in sorted(exactas, key=lambda c: (exactas[c] - int(exactas[c]), c), reverse=True)[:faltan]:
        cartas[carrier] += 1
    return cartas


def resumen_pesos(pesos, tope=None):
    """Para la ficha: tamaño de bloque y, por carrier, el peso pedido y el
    efectivo (cartas/tam); `redondeados` lista los que no quedaron exactos."""
    tam = tamano_bloque(pesos, tope)
    cartas = cartas_por_carrier(pesos, tam)
    filas, redondeados = [], []
    for carrier, n in cartas.items():
        pedido = Fraction(str(pesos[carrier]))
        efectivo = Fraction(n * 100, tam)
        filas.append({"carrier": carrier, "peso": pesos[carrier], "cartas": n,
                      "efectivo": float(efectivo)})
        if efectivo != pedido:
            redondeados.append(f"{carrier} {pesos[carrier]} → {float(efectivo):g}")
    return {"tam": tam, "filas": filas, "redondeados": redondeados}


def semilla(slug, bloque):
    """SHA-256 de "<slug>-<bloque>" como entero: reproducible entre procesos y
    años (jamás hash() de Python, que cambia por proceso)."""
    return int(hashlib.sha256(f"{slug}-{bloque}".encode()).hexdigest(), 16)


def baraja(slug, bloque, pesos, tam=None):
    """Las `tam` cartas del bloque en el orden en que se sacan."""
    tam = tamano_bloque(pesos) if tam is None else tam
    cartas = []
    for carrier, n in cartas_por_carrier(pesos, tam).items():
        cartas += [carrier] * n
    random.Random(semilla(slug, bloque)).shuffle(cartas)
    return cartas


def posicion(cliente):
    """(bloque, posición, tam) de la SIGUIENTE carta del cliente, para
    mostrar "carta 3 de 4 del bloque 12" (posición base 1 = cartas sacadas + 1)."""
    tam = tamano_bloque(cliente.reparto_pesos)
    if not tam:
        return 0, 0, 0
    bloque, pos = divmod(cliente.reparto_cursor - cliente.reparto_base, tam)
    return bloque, pos + 1, tam


def sacar_carta(pedido):
    """Carrier que le toca al pedido. Idempotente por pedido: la primera
    llamada saca la carta bajo candado (pedido y cliente) y la persiste en
    Pedido.reparto_carrier; las siguientes la regresan. Sin pesos → ValueError."""
    from apps.core.models import Cliente  # lazy por contrato
    from apps.pedidos.models import Pedido  # lazy por contrato

    with transaction.atomic():
        bloqueado = Pedido.objects.select_for_update().only("id", "cliente_id", "reparto_carrier").get(pk=pedido.pk)
        if bloqueado.reparto_carrier:
            pedido.reparto_carrier = bloqueado.reparto_carrier
            return pedido.reparto_carrier
        cliente = Cliente.objects.select_for_update().get(pk=bloqueado.cliente_id)
        pesos = cliente.reparto_pesos or {}
        tam = tamano_bloque(pesos)
        if not tam:
            raise ValueError(f"El cliente {cliente.slug} está en reparto pero no tiene pesos configurados.")
        n = cliente.reparto_cursor
        cliente.reparto_cursor = n + 1
        cliente.save(update_fields=["reparto_cursor"])
        bloque, pos = divmod(n - cliente.reparto_base, tam)
        carta = baraja(cliente.slug, bloque, pesos, tam)[pos]
        Pedido.objects.filter(pk=pedido.pk).update(reparto_carrier=carta)
        pedido.reparto_carrier = carta
        registrar_evento(
            "pedido", pedido.pk, ACCION_CARTA, cliente=cliente,
            delta={"n": n, "bloque": bloque, "posicion": pos, "tam": tam, "carta": carta},
            motivo=f"Reparto {cliente.slug}: carta {pos + 1} de {tam} del bloque {bloque} → {carta}",
        )
        return carta


# ── Reporte (Mesa → Reportes → Reparto de carriers) ──────────────────

def reporte_mes(inicio, fin, cliente=None):
    """Por cliente en reparto (o con cartas en el mes): por carrier, cartas
    asignadas, guías efectivas (guía activa con la carta), guías con otro
    carrier (forzado después de la carta), sin guía (pendiente o fallo) y
    canceladas; más los pedidos con guía sin carta (forzados por ReglaEnvio)
    y el bloque en curso. Los pedidos entran por su fecha de creación."""
    from apps.core.models import Cliente  # lazy por contrato
    from apps.pedidos.models import Pedido  # lazy por contrato

    pedidos = (
        Pedido.objects.filter(creado__gte=inicio, creado__lt=fin)
        .select_related("cliente").prefetch_related("guias").order_by("pk")
    )
    if cliente is not None:
        pedidos = pedidos.filter(cliente=cliente)
    clientes = {c.pk: c for c in Cliente.objects.filter(integracion_envios=Cliente.INTEGRACION_REPARTO)}
    if cliente is not None:
        clientes = {cliente.pk: cliente} if cliente.pk in clientes else {}
    por_cliente = {}
    for p in pedidos:
        if not p.reparto_carrier and p.cliente_id not in clientes:
            continue
        clientes.setdefault(p.cliente_id, p.cliente)
        datos = por_cliente.setdefault(p.cliente_id, {"carriers": {}, "forzados_regla": 0, "pedidos": 0})
        datos["pedidos"] += 1
        guia = next((g for g in p.guias.all() if g.es_activa), None)
        if not p.reparto_carrier:
            if guia is not None:
                datos["forzados_regla"] += 1
            continue
        fila = datos["carriers"].setdefault(
            p.reparto_carrier, {"cartas": 0, "efectivas": 0, "otro_carrier": 0, "sin_guia": 0, "canceladas": 0},
        )
        fila["cartas"] += 1
        if guia is not None and guia.carrier == p.reparto_carrier:
            fila["efectivas"] += 1
        elif guia is not None:
            fila["otro_carrier"] += 1
        elif p.estado == Pedido.CANCELADO:
            fila["canceladas"] += 1
        else:
            fila["sin_guia"] += 1
    resultado = []
    for pk, c in sorted(clientes.items(), key=lambda par: par[1].nombre):
        datos = por_cliente.get(pk, {"carriers": {}, "forzados_regla": 0, "pedidos": 0})
        pesos = c.reparto_pesos or {}
        bloque, pos, tam = posicion(c)
        filas = []
        for carrier in sorted(set(pesos) | set(datos["carriers"])):
            fila = datos["carriers"].get(carrier, {"cartas": 0, "efectivas": 0, "otro_carrier": 0, "sin_guia": 0, "canceladas": 0})
            total = sum(f["cartas"] for f in datos["carriers"].values())
            filas.append({"carrier": carrier, "peso": pesos.get(carrier), **fila,
                          "porcentaje": round(fila["cartas"] * 100 / total, 1) if total else None})
        resultado.append({
            "cliente": c, "en_reparto": c.integracion_envios == Cliente.INTEGRACION_REPARTO,
            "filas": filas, "cartas": sum(f["cartas"] for f in filas),
            "forzados_regla": datos["forzados_regla"], "pedidos": datos["pedidos"],
            "bloque": bloque, "posicion": pos, "tam": tam,
        })
    return resultado
