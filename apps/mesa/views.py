"""Mesa de Control: el tablero interno de la operación completa.

Aquí NO se filtra por tenant: la Mesa ve todos los clientes. Toda acción de
gestión (responder incidencias, compensaciones, reclamaciones, push manual)
pasa por los servicios de dominio para que la auditoría y los relojes SLA
queden siempre consistentes. Cross-app: modelos y servicios se importan lazy
(dentro de cada función) según CONVENTIONS.md.
"""
import csv
import io
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.configuracion.models import CorreoIncidencias
from apps.core.decorators import redirigir_rol, rol_requerido
from apps.core.models import Cliente, EventoAuditoria, EvidenciaFoto, PerfilUsuario
from apps.core.services import email_real, enviar_acceso, registrar_evento
from apps.mesa.forms import (
    CAMPOS_TARIFARIO_SIMPLES, CLAVES_BRANDING, FormAnuncioASNMesa, FormCliente,
    FormPedidoManual, FormSKU, FormTarifario, ZONAS_ENVIO,
)

# Minutos antes de vencer un reloj SLA en los que la pill pasa a "warn".
UMBRAL_WARN_SLA_MIN = 240

# Proyección visual del estado del pedido a la pill del sistema WOP.
PILL_PEDIDO = {
    "PENDIENTE": "",
    "EN_PICKING": "accent",
    "EMPACADO": "accent",
    "GUIA_GENERADA": "accent",
    "RECOLECTADO": "accent",
    "EN_TRANSITO": "accent",
    "ENTREGADO": "ok",
    "ENTREGA_PRESUNTA": "warn",
    "PARCIALMENTE_DESPACHADO": "warn",
    "CANCELACION_PENDIENTE": "warn",
    "CANCELADO": "",
    "RETORNADO": "crit",
}

# Estados desde los que la Mesa puede pedir cancelación: la matriz de
# pedidos.services.cancelar hace el resto (los terminales truenan con ValueError
# y CANCELACION_PENDIENTE se cierra en piso con el restock, no aquí).
ESTADOS_CANCELABLES_MESA = {
    "PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA",
    "RECOLECTADO", "EN_TRANSITO", "PARCIALMENTE_DESPACHADO", "ENTREGA_PRESUNTA",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de presentación
# ─────────────────────────────────────────────────────────────────────────────


def _humano_delta(delta):
    """'4 min', '3 h', '2 días' — para relojes y frescura, sin tecnicismos."""
    minutos = int(abs(delta.total_seconds()) // 60)
    if minutos < 60:
        return f"{minutos} min"
    horas = minutos // 60
    if horas < 48:
        return f"{horas} h"
    return f"{horas // 24} días"


def _frescura(ts, ahora=None):
    """'hace X min' para salud de sync. NUNCA la palabra 'tiempo real'."""
    if ts is None:
        return "nunca"
    ahora = ahora or timezone.now()
    return f"hace {_humano_delta(ahora - ts)}"


def _anotar_reloj_sla(incidencia, ahora):
    """Cuelga al objeto: reloj activo, pill ok|warn|crit y texto legible.

    El reloj activo es el de primera respuesta mientras no haya respuesta
    humana; después, el de resolución. Vencido → crit; por vencer → warn.
    """
    if incidencia.ts_primera_respuesta is None and incidencia.sla_respuesta_limite is not None:
        limite, etiqueta = incidencia.sla_respuesta_limite, "primera respuesta"
    else:
        limite, etiqueta = incidencia.sla_resolucion_limite, "resolución"

    incidencia.reloj_etiqueta = etiqueta
    incidencia.reloj_limite = limite
    if not incidencia.abierta:
        incidencia.reloj_pill = "ok"
        incidencia.reloj_texto = "reloj detenido"
        return incidencia
    if limite is None:
        incidencia.reloj_pill = "ok"
        incidencia.reloj_texto = "sin reloj"
        return incidencia

    restante = limite - ahora
    if restante < timedelta(0):
        incidencia.reloj_pill = "crit"
        incidencia.reloj_texto = f"{etiqueta} vencida hace {_humano_delta(restante)}"
    elif restante <= timedelta(minutes=UMBRAL_WARN_SLA_MIN):
        incidencia.reloj_pill = "warn"
        incidencia.reloj_texto = f"quedan {_humano_delta(restante)} para {etiqueta}"
    else:
        incidencia.reloj_pill = "ok"
        incidencia.reloj_texto = f"quedan {_humano_delta(restante)} para {etiqueta}"
    return incidencia


def _guias_con_retraso(hoy):
    """Guías vivas que pasaron su fecha compromiso sin entregarse (la misma
    regla que `envios.services._revisar_retraso`, Chema 2026-09-30: ya no
    cuenta el "sin movimiento"), con sus días de retraso, las más tardías
    primero. Una guía sin compromiso aún no ha salido."""
    from apps.envios.models import Guia

    retrasadas = list(
        Guia.objects.filter(fecha_compromiso__lt=hoy)
        .exclude(estado__in=list(Guia.ESTADOS_TERMINALES))
        .select_related("pedido", "pedido__cliente")
    )
    for guia in retrasadas:
        guia.dias_retraso = (hoy - guia.fecha_compromiso).days
    retrasadas.sort(key=lambda g: -g.dias_retraso)
    return retrasadas


def _pct_salida_mismo_dia(ahora):
    """% de la semana: pedidos pre-corte recolectados el mismo día de su ingreso.

    Los pedidos de hoy que siguen en proceso no cuentan (ni a favor ni en
    contra): el día no ha terminado. Regresa (pct o None, medibles, a_tiempo).
    """
    from apps.pedidos.models import Pedido

    hoy = timezone.localdate()
    desde = ahora - timedelta(days=7)
    medibles = 0
    a_tiempo = 0
    pedidos = Pedido.objects.filter(creado__gte=desde).exclude(
        estado__in=[Pedido.CANCELADO, Pedido.CANCELACION_PENDIENTE]
    )
    for pedido in pedidos:
        creado_local = timezone.localtime(pedido.creado)
        corte = pedido.corte_vigente_al_ingreso
        if corte is None or creado_local.time() > corte:
            continue  # post-corte: la promesa es el siguiente día hábil
        if creado_local.date() == hoy and pedido.ts_recolectado is None:
            continue  # todavía en proceso hoy: aún puede salir a tiempo
        medibles += 1
        if (
            pedido.ts_recolectado is not None
            and timezone.localtime(pedido.ts_recolectado).date() == creado_local.date()
        ):
            a_tiempo += 1
    pct = round(a_tiempo * 100 / medibles) if medibles else None
    return pct, medibles, a_tiempo


def _exactitud_inventario():
    """% de conteos exactos (contado == esperado) en los últimos 30 días."""
    from apps.inventario.models import Conteo
    from django.db.models import F

    desde = timezone.now() - timedelta(days=30)
    total = Conteo.objects.filter(ts__gte=desde).count()
    if not total:
        return None, 0
    exactos = Conteo.objects.filter(ts__gte=desde, contado=F("esperado")).count()
    return round(exactos * 100 / total, 1), total


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def dashboard(request):
    from apps.incidencias.models import Incidencia
    from apps.incidencias.services import abiertas_fuera_de_sla
    from apps.pedidos.models import Pedido

    ahora = timezone.now()
    hoy = timezone.localdate()

    # Pedidos de hoy, por estado (orden canónico del pipeline).
    conteos = dict(
        Pedido.objects.filter(creado__date=hoy)
        .values_list("estado")
        .annotate(n=Count("id"))
    )
    total_hoy = sum(conteos.values())
    pedidos_hoy = [
        {
            "estado": clave,
            "nombre": nombre,
            "n": conteos[clave],
            "pct": round(conteos[clave] * 100 / total_hoy) if total_hoy else 0,
            "pill": PILL_PEDIDO.get(clave, ""),
        }
        for clave, nombre in Pedido.ESTADOS
        if conteos.get(clave)
    ]

    # Incidencias abiertas ordenadas por su reloj SLA (el más apretado arriba).
    abiertas = [
        _anotar_reloj_sla(inc, ahora)
        for inc in Incidencia.objects.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
        .select_related("cliente", "pedido")
    ]
    abiertas.sort(key=lambda i: (i.reloj_limite is None, i.reloj_limite or ahora))
    fuera_de_sla = abiertas_fuera_de_sla().count()

    # Detenidos en piso (Chema 2026-10-06): pedidos fuera de la cola con su incidencia DET/PAQ abierta.
    from apps.incidencias.services import TIPOS_QUE_DETIENEN, resolucion_de  # lazy por contrato
    detenidos = []
    for inc in (
        Incidencia.objects.filter(pedido__detenido=True, tipo__in=TIPOS_QUE_DETIENEN, estado__in=Incidencia.ESTADOS_ABIERTOS)
        .select_related("pedido", "pedido__cliente").order_by("pedido__creado")
    ):
        ultimo = inc.mensajes.order_by("-pk").first()
        detenidos.append({
            "incidencia": _anotar_reloj_sla(inc, ahora), "pedido": inc.pedido,
            "motivo": (ultimo.texto if ultimo else "") or resolucion_de(inc) or "",
        })
    salida_pct, salida_medibles, salida_a_tiempo = _pct_salida_mismo_dia(ahora)
    exactitud_pct, conteos_30d = _exactitud_inventario()
    retrasadas = _guias_con_retraso(hoy)
    eventos = EventoAuditoria.objects.select_related("cliente")[:15]

    # Banner semáforo del día.
    if fuera_de_sla or retrasadas:
        problemas = []
        if fuera_de_sla:
            problemas.append(f"{fuera_de_sla} incidencia(s) fuera de SLA")
        if retrasadas:
            problemas.append(f"{len(retrasadas)} guía(s) con retraso")
        banner = {"nivel": "crit", "texto": "Atención: " + " y ".join(problemas) + ". La pelota es nuestra."}
    elif any(i.reloj_pill == "warn" for i in abiertas):
        banner = {
            "nivel": "warn",
            "texto": f"Relojes apretados: {sum(1 for i in abiertas if i.reloj_pill == 'warn')} incidencia(s) por vencer. Hoy: {total_hoy} pedido(s).",
        }
    else:
        banner = {
            "nivel": "ok",
            "texto": f"Todo en orden. {total_hoy} pedido(s) hoy, corte a las {settings.TORRE['CORTE_CONTRACTUAL']}.",
        }

    return render(request, "mesa/dashboard.html", {
        "seccion": "dashboard",
        "banner": banner,
        "total_hoy": total_hoy,
        "pedidos_hoy": pedidos_hoy,
        "incidencias_abiertas": abiertas,
        "fuera_de_sla": fuera_de_sla,
        "salida_pct": salida_pct,
        "salida_medibles": salida_medibles,
        "salida_a_tiempo": salida_a_tiempo,
        "exactitud_pct": exactitud_pct,
        "conteos_30d": conteos_30d,
        "guias_retrasadas": retrasadas,
        "detenidos": detenidos,
        "eventos": eventos,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Bodega en vivo: el plano real del Local 380 E con datos por zona
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def bodega(request):
    """El plano real del Local 380 E con la carga de trabajo de cada zona, ahora.

    Los querysets viven en apps.mesa.plano.zonas_bodega (compartido con el
    portal, que lo llama filtrado por tenant); aquí solo se decora con el
    vocabulario de la Mesa (pills y relojes SLA).
    """
    from apps.incidencias.models import Incidencia
    from apps.inventario.services import buscar_en_bodega  # lazy por contrato
    from apps.mesa.plano import racks_bodega, zonas_bodega

    busqueda = buscar_en_bodega(request.GET.get("q"))  # Chema 2026-10-06: ¿dónde está X?
    zonas, badges, zona_activa = zonas_bodega()
    badges["oficina"] = Incidencia.objects.filter(
        estado__in=Incidencia.ESTADOS_ABIERTOS
    ).count()

    for orden in zonas["ordenes"]:
        _decorar_asn(orden)
    for grupo in zonas["corrales"]:
        for pedido in grupo["pedidos"]:
            pedido.pill = PILL_PEDIDO.get(pedido.estado, "")

    return render(request, "mesa/bodega.html", {
        "seccion": "bodega",
        "actualizado": timezone.localtime(),
        "badges": badges,
        "racks": racks_bodega(resaltar=busqueda["racks"] if busqueda else None),
        "zona_activa": zona_activa,
        "busqueda": busqueda,
        **zonas,
    })




def _orden_posicion(codigo):
    """Nombres por pasillo primero (letra, número), luego los códigos viejos y al final las tarimas."""
    m = re.match(r"^([A-Z])-(\d{1,2})$", codigo)
    if m:
        return (0, m.group(1), int(m.group(2)), codigo)
    if codigo.startswith("TAR-"):
        return (2, "", 0, codigo)
    return (1, "", 0, codigo)


def _entero_positivo(valor, mensaje):
    try:
        n = int(str(valor or "").strip())
    except ValueError:
        raise ValueError(mensaje) from None
    if n <= 0:
        raise ValueError(mensaje)
    return n


@rol_requerido("mesa")
def bodega_posiciones(request):
    """Mesa → Bodega → Posiciones (Chema 2026-10-13): todas las posiciones de
    almacén (racks y tarimas) con lo que hay en cada una y, por posición,
    **Editar** (cambiar nombre / mover existencias / ajustar conteo /
    producto fijo), **Imprimir posición** (QR `U|nombre`) e **Imprimir
    etiquetas de las cajas** (una por caja de cada producto y lote,
    cantidades prellenadas con lo que el sistema dice). POST `crear` da de
    alta una posición nueva (2026-10-14)."""
    from apps.catalogo.models import SKU, Ubicacion  # lazy: modelos de otra app
    from apps.catalogo.services import asignaciones_de  # lazy por contrato
    from apps.inventario.models import Ajuste  # lazy: modelo de otra app
    from apps.inventario.services import capacidad_asignada, existencias_de_posicion, ocupaciones  # lazy por contrato

    if request.method == "POST" and request.POST.get("accion") == "crear":
        # Nueva posición (Chema 2026-10-14): nombre, tipo, medidas y prioridad; nace activa.
        from apps.inventario.services import crear_posicion  # lazy por contrato

        try:
            nueva = crear_posicion(
                request.POST.get("codigo"), request.POST.get("tipo"), request.POST.get("largo_cm"), request.POST.get("ancho_cm"),
                request.POST.get("alto_cm"), request.POST.get("prioridad"), actor=request.user,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("mesa:bodega_posiciones")
        messages.success(request, f"Posición {nueva.codigo} creada ({nueva.get_tipo_display().lower()}). Imprime y pega su etiqueta.")
        return redirect(f"{reverse('mesa:bodega_posiciones')}#pos-{nueva.pk}")
    posiciones = list(_posiciones_almacen())
    ocupacion = ocupaciones(posiciones)
    filas = []
    for p in posiciones:
        existencias = existencias_de_posicion(p)
        fijos = [{"sku": a.sku, "orden": a.orden, "capacidad": capacidad_asignada(p, a.sku)} for a in asignaciones_de(p)]
        filas.append({
            "ubicacion": p, "existencias": existencias, "ocupacion": ocupacion.get(p.codigo, {}), "fijos": fijos,
            "vendible": sum(e["vendible"] for e in existencias), "apartado": sum(e["apartado"] for e in existencias),
            "es_tarima": p.codigo.startswith("TAR-"), "datos_id": f"pos-datos-{p.pk}",
            "datos": {
                "pk": p.pk, "codigo": p.codigo,
                "filas": [{
                    "sku": e["sku"].pk, "sku_codigo": e["sku"].codigo, "descripcion": e["sku"].descripcion,
                    "codigo_barras": e["sku"].codigo_barras or "",
                    "lote": e["lote"].pk if e["lote"] else 0, "lote_codigo": e["lote"].codigo if e["lote"] else "",
                    "caducidad": e["lote"].fecha_caducidad.isoformat() if e["lote"] and e["lote"].fecha_caducidad else "",
                    "vendible": e["vendible"], "apartado": e["apartado"],
                } for e in existencias],
                "fijos": [{"sku_codigo": f["sku"].codigo, "descripcion": f["sku"].descripcion, "capacidad": f["capacidad"]} for f in fijos],
            },
        })
    filas.sort(key=lambda f: _orden_posicion(f["ubicacion"].codigo))
    return render(request, "mesa/bodega_posiciones.html", {
        "seccion": "bodega", "filas": filas, "destinos": sorted(posiciones, key=lambda p: _orden_posicion(p.codigo)),
        "n_posiciones": len(filas), "n_con_producto": sum(1 for f in filas if f["existencias"]),
        "skus": SKU.objects.filter(activo=True).select_related("cliente").order_by("cliente__nombre", "codigo"),
        "motivos_ajuste": Ajuste.MOTIVOS,
        "tipos_posicion": [(Ubicacion.PICKING, "Rack de picking"), (Ubicacion.RESERVA, "Reserva / tarima")],
    })


def _sku_por_codigo_o_barras(texto):
    """SKU por código exacto (sin distinguir mayúsculas) o por código de barras completo; None si no hay."""
    from apps.catalogo.models import SKU  # lazy: modelo de otra app

    texto = (texto or "").strip()
    if not texto:
        return None
    return SKU.objects.filter(Q(codigo__iexact=texto) | Q(codigo_barras=texto)).select_related("cliente").first()


@rol_requerido("mesa")
def bodega_lotes_json(request):
    """Lotes conocidos de un producto para el texto predictivo de Ajustar
    conteo (Chema 2026-10-14): `?sku=` por código o código de barras →
    {sku, descripcion, requiere_lote, lotes: [{codigo, caducidad}]} ordenados
    por caducidad; al elegir uno, la pantalla llena su caducidad."""
    from apps.catalogo.models import Lote  # lazy: modelo de otra app

    sku = _sku_por_codigo_o_barras(request.GET.get("sku"))
    if sku is None:
        return JsonResponse({"sku": None, "descripcion": "", "requiere_lote": True, "lotes": []})
    lotes = [
        {"codigo": l.codigo, "caducidad": l.fecha_caducidad.isoformat() if l.fecha_caducidad else ""}
        for l in Lote.objects.filter(sku=sku).order_by("fecha_caducidad", "codigo")
    ]
    return JsonResponse({"sku": sku.codigo, "descripcion": sku.descripcion, "requiere_lote": sku.requiere_lote, "lotes": lotes})


@rol_requerido("mesa")
def bodega_posicion_accion(request, pk):
    """POST sobre una posición: `renombrar` (nuevo), `mover` (sku_id, lote_id,
    cantidad, destino, motivo), `ajustar` (conteo por lote, 2026-10-14),
    `asignar` / `desasignar` (producto fijo), `imprimir_posicion` e
    `imprimir_cajas` (`n_<sku>_<lote>` = cuántas etiquetas por renglón).
    Vuelve a la lista anclada en la posición; los errores van como mensaje."""
    from apps.catalogo.models import SKU, Lote, Ubicacion  # lazy: modelos de otra app
    from apps.catalogo.services import asignar_producto, desasignar_producto  # lazy por contrato
    from apps.inventario.services import mover_existencias, renombrar_posicion  # lazy por contrato
    from apps.piso.etiquetas import imprimir_etiqueta_posicion, imprimir_etiquetas_posicion  # lazy por contrato

    ubicacion = get_object_or_404(Ubicacion, pk=pk, activo=True, tipo__in=(Ubicacion.PICKING, Ubicacion.RESERVA))
    volver = redirect(f"{reverse('mesa:bodega_posiciones')}#pos-{ubicacion.pk}")
    if request.method != "POST":
        return volver
    accion = request.POST.get("accion")
    try:
        if accion == "renombrar":
            anterior = renombrar_posicion(ubicacion, request.POST.get("nuevo"), actor=request.user)
            messages.success(request, f"{anterior} ahora se llama {ubicacion.codigo}. Imprime y pega su etiqueta nueva.")
        elif accion == "mover":
            sku = get_object_or_404(SKU, pk=request.POST.get("sku_id") or 0)
            lote_pk = (request.POST.get("lote_id") or "0").strip()
            lote = get_object_or_404(Lote, pk=lote_pk, sku=sku) if lote_pk not in ("", "0") else None
            destino = _posicion(request.POST.get("destino"))
            cantidad = _entero_positivo(request.POST.get("cantidad"), "Captura cuántas piezas mover.")
            r = mover_existencias(sku, lote, ubicacion, destino, cantidad, actor=request.user, motivo=request.POST.get("motivo") or "")
            messages.success(
                request,
                f"{r['cantidad']} × {sku.codigo}{' lote ' + lote.codigo if lote else ''}: de {ubicacion.codigo} a {destino.codigo}"
                + (f" (con {r['apartadas']} apartada(s))" if r["apartadas"] else "") + ".",
            )
        elif accion == "ajustar":
            # Ajustar conteo por lote (Chema 2026-10-14): `sku_id`, `lotes` (ids separados por coma, 0 = sin
            # lote) con `contado_<id>`, y opcionalmente un lote nuevo (`nuevo_lote`, `nuevo_caducidad`, `nuevo_contado`).
            from apps.inventario.services import ajustar_conteo_posicion  # lazy por contrato

            if request.POST.get("sku_id"):
                sku = get_object_or_404(SKU, pk=request.POST.get("sku_id"))
            else:
                codigo = (request.POST.get("sku") or "").strip()
                sku = _sku_por_codigo_o_barras(codigo)
                if sku is None:
                    raise ValueError(f"No existe el producto {codigo or '(vacío)'}: elígelo de la lista.")
            renglones = []
            for clave in (request.POST.get("lotes") or "").split(","):
                clave = clave.strip()
                valor = (request.POST.get(f"contado_{clave}") or "").strip()
                if not clave or valor == "":
                    continue
                lote = None if clave == "0" else get_object_or_404(Lote, pk=clave, sku=sku)
                renglones.append((lote, valor, None))
            nuevo_codigo = (request.POST.get("nuevo_lote") or "").strip()
            nuevo_contado = (request.POST.get("nuevo_contado") or "").strip()
            if nuevo_codigo or nuevo_contado:
                if nuevo_contado == "":
                    raise ValueError(f"Captura cuántas piezas hay del lote {nuevo_codigo}.")
                # Sin código: producto sin lote (el servicio lo rechaza si el producto sí lleva lote).
                renglones.append((nuevo_codigo, nuevo_contado, _fecha_o_none(request.POST.get("nuevo_caducidad"))))
            r = ajustar_conteo_posicion(
                ubicacion, sku, renglones, request.POST.get("motivo") or "", request.user, nota=request.POST.get("nota") or "",
            )
            partes = [f"lote {a['lote'] or 'sin lote'}: {a['esperado']} → {a['contado']} ({a['delta']:+d}, {a['folio']})" for a in r["ajustes"]]
            texto = f"{sku.codigo} en {ubicacion.codigo}: " + ("; ".join(partes) if partes else "sin diferencias") + f" · {r['conteos']} conteo(s)."
            if r["liberadas"]:
                texto += " Reserva liberada por falta de físico: " + ", ".join(f"{folio} ({n})" for folio, n in r["liberadas"]) + "; esos pedidos quedan sin inventario hasta que entre stock."
            if r["sin_respaldo"]:
                texto += f" Ojo: {r['sin_respaldo']} pieza(s) apartadas por pedidos ya en picking siguen sin físico."
            if r.get("reubicadas"):
                texto += " Apartadas que ya no tenían físico aquí pasan a " + ", ".join(f"{codigo} ({n})" for codigo, n in r["reubicadas"]) + "."
            (messages.warning if r["liberadas"] or r["sin_respaldo"] else messages.success)(request, texto)
        elif accion == "asignar":
            codigo = (request.POST.get("sku") or "").strip()
            sku = SKU.objects.filter(codigo__iexact=codigo).first()
            if sku is None:
                raise ValueError(f"No existe el producto {codigo or '(vacío)'}.")
            asignar_producto(ubicacion, sku, actor=request.user)
            messages.success(request, f"{sku.codigo} queda fijo en {ubicacion.codigo}.")
        elif accion == "desasignar":
            sku = get_object_or_404(SKU, codigo=(request.POST.get("sku") or "").strip())
            desasignar_producto(sku, actor=request.user)
            messages.success(request, f"{sku.codigo} deja de ser fijo en {ubicacion.codigo}.")
        elif accion == "imprimir_posicion":
            imprimir_etiqueta_posicion(ubicacion, request.user)
            messages.success(request, f"La etiqueta de {ubicacion.codigo} va a la impresora.")
        elif accion == "imprimir_cajas":
            filas = []
            for clave, valor in request.POST.items():
                if not clave.startswith("n_") or not str(valor).strip() or str(valor).strip() == "0":
                    continue
                _n, sku_pk, lote_pk = clave.split("_")
                cantidad = _entero_positivo(valor, "Las cantidades deben ser números enteros.")
                sku = get_object_or_404(SKU, pk=sku_pk)
                lote = get_object_or_404(Lote, pk=lote_pk, sku=sku) if lote_pk != "0" else None
                filas.append((sku, lote, cantidad))
            trabajos = imprimir_etiquetas_posicion(ubicacion, filas, request.user)
            total = sum(cantidad for _sku, _lote, cantidad in filas)
            messages.success(request, f"{total} etiqueta(s) de caja de {ubicacion.codigo} van a la impresora en {len(trabajos)} tanda(s).")
        else:
            raise ValueError("No entendí la acción.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return volver


# ─────────────────────────────────────────────────────────────────────────────
# Incidencias: lista y gestión completa
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def incidencias(request):
    """Mesa → Incidencias (Chema 2026-09-29): una fila por PEDIDO con
    incidencias, con sus casos abiertos como píldoras; las cerradas no
    aparecen salvo con "Mostrar cerradas"; las incidencias sin pedido
    (descuadres) van en su propia tabla. El expediente por pedido vive en
    incidencias_pedido."""
    from django.db.models import Max

    from apps.incidencias.models import Incidencia
    from apps.incidencias.services import agrupar_por_pedido

    ahora = timezone.now()
    qs = (
        Incidencia.objects.select_related("cliente", "pedido", "sku")
        .prefetch_related("compensaciones").annotate(ultimo_mensaje=Max("mensajes__ts"))
    )
    estado = request.GET.get("estado", "").strip()
    tipo = request.GET.get("tipo", "").strip()
    cliente_id = request.GET.get("cliente", "").strip()
    origen = request.GET.get("origen", "").strip()  # Chema 2026-09-24: distinguir automáticas de las del cliente/comprador
    q = request.GET.get("q", "").strip()  # Chema 2026-09-28: folio, pedido, orden de Shopify, comprador o dueño
    cerradas = request.GET.get("cerradas") == "on"
    if q:
        nombre = q if q.startswith("#") else f"#{q}"
        qs = qs.filter(
            Q(folio__icontains=q) | Q(pedido__folio__icontains=q) | Q(pedido__shopify_order_name__iexact=nombre)
            | Q(pedido__shopify_order_name__icontains=q) | Q(pedido__comprador_nombre__icontains=q)
            | Q(dueno__icontains=q)
        )
    if estado == "abiertas":
        qs = qs.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
    elif estado:
        qs = qs.filter(estado=estado)
    elif not cerradas:
        qs = qs.exclude(estado=Incidencia.CERRADA)
    if tipo:
        qs = qs.filter(tipo=tipo)
    if cliente_id:
        qs = qs.filter(cliente_id=cliente_id)
    if origen:
        qs = qs.filter(origen=origen)
    filas = [_anotar_reloj_sla(inc, ahora) for inc in qs]
    grupos, sueltas = agrupar_por_pedido(filas)
    for g in grupos:
        con_reloj = [i for i in g["abiertas"] if i.reloj_limite is not None]
        g["reloj"] = min(con_reloj, key=lambda i: i.reloj_limite) if con_reloj else (g["abiertas"][0] if g["abiertas"] else None)
    return render(request, "mesa/incidencias.html", {
        "seccion": "incidencias",
        "grupos": grupos,
        "sueltas": sueltas,
        "tipos": Incidencia.TIPOS,
        "estados": Incidencia.ESTADOS,
        "origenes": Incidencia.ORIGENES,
        "clientes_filtro": Cliente.objects.all(),
        "filtro": {"estado": estado, "tipo": tipo, "cliente": cliente_id, "origen": origen, "q": q, "cerradas": cerradas},
        "abiertas_n": sum(1 for i in filas if i.abierta),
        "total_n": len(filas),
        "pausas": Cliente.objects.filter(incidencias_auto_pausadas_hasta__gte=timezone.localdate()).order_by("nombre"),
    })


@rol_requerido("mesa")
def incidencias_pedido(request, pk):
    """Expediente de incidencias de UN pedido (Chema 2026-09-29): todas sus
    incidencias, cerradas al fondo, estado en la segunda columna; guías con
    su estado real y "Nueva incidencia" con el pedido puesto."""
    from django.db.models import Max

    from apps.envios.services import guias_del_pedido  # lazy por contrato
    from apps.incidencias.models import Incidencia
    from apps.incidencias.services import agrupar_por_pedido
    from apps.pedidos.models import Pedido
    from apps.pedidos.reportes import url_orden_shopify  # lazy por contrato

    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=pk)
    ahora = timezone.now()
    qs = (
        Incidencia.objects.filter(pedido=pedido).select_related("cliente", "sku")
        .prefetch_related("compensaciones").annotate(ultimo_mensaje=Max("mensajes__ts"))
    )
    grupos, _ = agrupar_por_pedido([_anotar_reloj_sla(inc, ahora) for inc in qs])
    grupo = grupos[0] if grupos else {"pedido": pedido, "incidencias": [], "abiertas": [], "cerradas": 0}
    return render(request, "mesa/incidencias_pedido.html", {
        "seccion": "incidencias",
        "pedido": pedido,
        "grupo": grupo,
        "guias": guias_del_pedido(pedido),
        "url_shopify": url_orden_shopify(pedido),
    })


@rol_requerido("mesa")
def incidencia_nueva(request):
    """Levantar una incidencia desde Mesa (Chema 2026-09-28): la puerta que
    faltaba (antes solo el cliente, el comprador y las automáticas). Pasa por
    incidencias.abrir_incidencia como todas: folio, SLA, aviso al cliente y
    auditoría; con `?pedido=<pk>` llega con el pedido puesto desde la lista."""
    from apps.incidencias.models import Incidencia
    from apps.incidencias.services import abrir_incidencia
    from apps.mesa.forms import FormNuevaIncidenciaMesa
    from apps.pedidos.models import Pedido

    pedido_id = request.GET.get("pedido", "")
    prellenado = Pedido.objects.filter(pk=int(pedido_id)).first() if pedido_id.isdigit() else None
    # "Rehacer entrega" desde el pedido (2026-10-10): ?tipo=ERR&reponer=1 llega con todo puesto.
    form = FormNuevaIncidenciaMesa(
        request.POST or None, pedido_inicial=prellenado,
        tipo_inicial=request.GET.get("tipo", ""), reponer_inicial=request.GET.get("reponer") == "1",
    )
    if request.method == "POST" and form.is_valid():
        datos = form.cleaned_data
        if datos["tipo"] == Incidencia.TIPO_SKU and datos["pedido_obj"] is not None:
            # Producto no registrado a mano (Chema 2026-10-07, para capacitar a OPS): el
            # pedido se detiene igual que si lo hubiera detectado la ingesta.
            from apps.pedidos.services import detener_por_producto_no_registrado  # lazy por contrato

            pedido_sku = datos["pedido_obj"]
            incidencia = detener_por_producto_no_registrado(
                pedido_sku, [{"titulo": datos["texto"] or "producto sin registrar", "cantidad": "?"}], usuario=request.user,
            )
            messages.success(request, f"Incidencia {incidencia.folio} abierta: {pedido_sku.folio} queda detenido hasta que el producto exista y se vuelva a leer la orden.")
            return redirect("mesa:incidencia_detalle", pk=incidencia.pk)
        incidencia = abrir_incidencia(
            datos["cliente_obj"], datos["tipo"], Incidencia.ORIGEN_MANUAL,
            pedido=datos["pedido_obj"], sku=datos["sku_obj"], texto=datos["texto"],
            prioridad=datos.get("prioridad") or None, interna=bool(datos.get("interna")),
        )
        if getattr(incidencia, "agrupada", False):
            messages.warning(
                request,
                f"El pedido ya tenía abierto un caso de ese tipo: tu reporte quedó en {incidencia.folio}.",
            )
        else:
            messages.success(request, f"Incidencia {incidencia.folio} abierta ({incidencia.get_tipo_display()}, {incidencia.prioridad}).")
        if datos.get("reponer") and incidencia.pedido_id:
            # "Reponer producto" (2026-09-29): cae en la compensación ya elegida.
            return redirect(f"{reverse('mesa:incidencia_detalle', args=[incidencia.pk])}?reponer=1#compensaciones")
        return redirect("mesa:incidencia_detalle", pk=incidencia.pk)
    return render(request, "mesa/incidencia_form.html", {"seccion": "incidencias", "form": form})


def _rehacer_entrega(incidencia, lineas, caja_param):
    """"Rehacer la entrega de la caja N" (Chema 2026-10-10): las cajas del
    pedido que ya salieron, con su contenido, para los botones del expediente;
    y, cuando viene ?caja=<pk>, el precargado de la reposición: cada línea
    que viajó en esa caja queda marcada con las piezas que llevó
    (`linea.prefill`). ?caja=todas (Chema 2026-10-22, PED-00613) precarga
    TODAS las cajas en una sola reposición. Cada línea recibe además
    `por_caja` = [{pk, numero, piezas, prefill}] para la casilla por caja del
    formulario. Regresa (cajas, caja_pk | "todas" | None)."""
    if incidencia.pedido_id is None or not lineas:
        return [], None
    from apps.pedidos.services import piezas_reponibles  # lazy por contrato

    reponibles = piezas_reponibles(incidencia.pedido)
    cajas = {}
    for linea in lineas:
        info = reponibles.get(linea.pk)
        for pk, c in (info["por_caja"].items() if info else []):
            caja = cajas.setdefault(pk, {"pk": pk, "numero": c["numero"], "contenido": []})
            caja["contenido"].append(f"{c['piezas']}× {linea.sku.codigo}")
    if str(caja_param) == "todas" and cajas:
        caja_pk = "todas"
    else:
        caja_pk = int(caja_param) if str(caja_param).isdigit() and int(caja_param) in cajas else None
    for linea in lineas:
        info = reponibles.get(linea.pk)
        por_caja = info["por_caja"] if info else {}
        linea.por_caja = [
            {"pk": pk, "numero": c["numero"], "piezas": c["piezas"],
             "prefill": c["piezas"] if caja_pk == "todas" or pk == caja_pk else 0}
            for pk, c in por_caja.items()
        ]
        if caja_pk is not None:
            linea.prefill = sum(c["prefill"] for c in linea.por_caja)
    return sorted(cajas.values(), key=lambda c: c["numero"]), caja_pk


def _contenido_cajas_err(incidencia):
    """Tarjeta "Contenido de las cajas" (Chema 2026-10-10), solo en incidencias
    de producto erróneo con pedido: cada caja que ya salió con sus renglones y
    cuáles se pueden corregir (sin corregir aún, sin fracción, sin kit)."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.incidencias.models import Incidencia  # lazy por contrato

    if incidencia.tipo != Incidencia.TIPO_ERR or incidencia.pedido_id is None:
        return []
    cajas = []
    for caja in (
        incidencia.pedido.paquetes.filter(estado=Paquete.DESPACHADO).order_by("numero")
        .prefetch_related("lineas__linea_pedido__sku", "lineas__sku_real")
    ):
        renglones = list(caja.lineas.all())
        cajas.append({
            "caja": caja, "renglones": renglones,
            "corregibles": [pl for pl in renglones if pl.sku_real_id is None and pl.fraccion_de <= 1 and not pl.linea_pedido.sku.es_kit],
        })
    return cajas


def _gestionar_incidencia(request, incidencia):
    """Despacha la acción POST del detalle. Regresa mensaje de éxito o lanza ValueError."""
    from apps.incidencias.models import Compensacion, ReclamacionCarrier
    from apps.incidencias.services import cerrar, resolver, responder

    accion = request.POST.get("accion", "")
    usuario = request.user.username
    texto = (request.POST.get("texto") or "").strip()

    if accion == "salida_sin_registro":
        # Caja sin salida (2026-10-12): Mesa la da por salida; sale como con el manifiesto.
        from apps.envios.models import Paquete
        from apps.pedidos.services import salida_sin_registro

        if incidencia.pedido is None:
            raise ValueError("La incidencia no tiene pedido.")
        caja = None
        if request.POST.get("caja_id"):
            caja = Paquete.objects.filter(pk=request.POST.get("caja_id"), pedido=incidencia.pedido).first()
            if caja is None:
                raise ValueError("Esa caja no es de este pedido.")
        salida_sin_registro(incidencia.pedido, caja, request.user)
        que = f"La caja {caja.numero}" if caja is not None else f"El pedido {incidencia.pedido.folio}"
        return f"{que} quedó como salida sin registro: despachada, con kardex y aviso, igual que con el manifiesto."

    if accion == "corregir_caja":
        # Producto erróneo (2026-10-10): la caja llevaba otro producto.
        from apps.catalogo.models import SKU
        from apps.envios.models import Paquete
        from apps.incidencias.services import corregir_contenido_caja
        from apps.pedidos.models import LineaPedido

        caja = Paquete.objects.filter(pk=request.POST.get("caja_id") or 0, pedido=incidencia.pedido).first()
        linea = LineaPedido.objects.filter(pk=request.POST.get("linea_id") or 0, pedido=incidencia.pedido).select_related("sku").first()
        if caja is None or linea is None:
            raise ValueError("Elige la caja y el producto que debía ir.")
        codigo = (request.POST.get("sku_real") or "").strip()
        sku_real = SKU.objects.filter(cliente=incidencia.cliente, codigo__iexact=codigo).first()
        if sku_real is None:
            raise ValueError(f"{incidencia.cliente.nombre} no tiene el SKU {codigo or '(vacío)'}: captura el producto que salió en su lugar.")
        try:
            piezas = int((request.POST.get("piezas") or "").strip())
        except ValueError:
            raise ValueError("Captura las piezas como número entero.") from None
        ajustar = bool(request.POST.get("ajustar"))
        firmas = (
            (request.POST.get("autorizo_1") or "").strip(), request.POST.get("pin_1") or "",
            (request.POST.get("autorizo_2") or "").strip(), request.POST.get("pin_2") or "",
        ) if ajustar else None
        _, ajustes = corregir_contenido_caja(incidencia, caja, linea, piezas, sku_real, request.user, ajustar=ajustar, firmas=firmas)
        folios = ", ".join(a.folio for a in ajustes)
        return (
            f"Caja {caja.numero} corregida: salió {sku_real.codigo} en lugar de {linea.sku.codigo} ({piezas} pza). "
            + (f"Ajustes {folios}." if ajustes else "Sin ajuste de inventario.")
            + " La caja queda en Reingresos por decidir."
        )

    if accion == "responder":
        if not texto:
            raise ValueError("Escribe la respuesta antes de enviarla.")
        responder(incidencia, autor=usuario, rol_autor="mesa", texto=texto)
        return "Respuesta publicada en el timeline (visible para el cliente)."

    if accion == "releer_orden":
        from apps.integraciones.services import releer_orden  # lazy por contrato

        if incidencia.pedido is None:
            raise ValueError("Esta incidencia no tiene pedido.")
        try:
            return releer_orden(incidencia.pedido, actor=request.user)["mensaje"]
        except Exception as exc:  # noqa: BLE001 — Shopify caído: aviso, no traceback
            raise ValueError(f"No se pudo leer la orden en Shopify: {exc}") from exc

    if accion == "nota_interna":
        if not texto:
            raise ValueError("Escribe la nota interna antes de guardarla.")
        responder(incidencia, autor=usuario, rol_autor="mesa", texto=texto, interno=True)
        return "Nota interna guardada. No se muestra en el portal, pero queda en el registro."

    if accion == "tomar":
        incidencia.transicionar("EN_CURSO", actor=request.user, motivo="La Mesa toma la incidencia")
        incidencia.dueno = usuario
        incidencia.save(update_fields=["dueno"])
        return f"Incidencia en curso. La pelota la tiene {usuario}."

    if accion == "resolver":
        if not texto:
            raise ValueError("Describe la resolución antes de marcar resuelta.")
        _exigir_plan_para_paq(incidencia)
        resolver(incidencia, texto, request.user)
        return "Incidencia resuelta. El cliente puede confirmarla o reabrirla desde su portal."

    if accion == "replanear_carrier":
        # "Sin paquetería que cotice" (Chema 2026-09-24): Mesa elige la
        # paquetería y el pedido se replanea con ella; la incidencia se
        # resuelve sola si cotiza.
        from apps.envios.services import etiqueta_paqueteria  # lazy por contrato
        from apps.pedidos.services import replanear_con_carrier  # lazy por contrato
        pedido = incidencia.pedido
        if pedido is None:
            raise ValueError("La incidencia no está ligada a un pedido.")
        carrier = (request.POST.get("carrier") or "").strip()
        resultado = replanear_con_carrier(pedido, carrier, request.user, incidencia=incidencia)
        cajas = resultado["cajas"]
        detalle = ", ".join(f"caja {c.numero} {c.carrier} {_precio_caja(c)}" for c in cajas)
        que = "recotizadas" if resultado["modo"] == "recotizadas" else "planeadas"
        return (
            f"{pedido.folio} con {etiqueta_paqueteria(carrier)}: {len(cajas)} caja{'s' if len(cajas) != 1 else ''} "
            f"{que} ({detalle}). La incidencia quedó cerrada; el piso sigue con el empaque."
        )
    if accion == "cambiar_paqueteria_caja":
        # "Sin paquetería" por CAJA (Chema 2026-10-01): solo la que falló.
        from apps.envios.models import Paquete  # lazy: modelo de otra app
        from apps.envios.services import etiqueta_paqueteria  # lazy por contrato
        from apps.pedidos.services import cambiar_paqueteria_caja  # lazy por contrato
        pedido = incidencia.pedido
        if pedido is None:
            raise ValueError("La incidencia no está ligada a un pedido.")
        caja = get_object_or_404(Paquete, pk=request.POST.get("caja"), pedido=pedido)
        carrier = (request.POST.get("carrier") or "").strip()
        caja = cambiar_paqueteria_caja(pedido, caja, carrier, request.user)
        etiqueta = etiqueta_paqueteria(carrier) if carrier else "las reglas del pedido"
        cerrada = _resolver_paq_si_cotiza(incidencia, request.user)
        return (
            f"Caja {caja.numero} de {pedido.folio} con {etiqueta}: {_precio_caja(caja)}. "
            + ("Todas las cajas tienen paquetería: la incidencia quedó cerrada." if cerrada else "La incidencia sigue abierta: aún hay cajas sin paquetería.")
        )
    if accion == "regresar_a_empaque":
        # Cambio de dirección con guía comprada (Chema 2026-09-23): solo si
        # nada ha salido y Shopify ya mandó una dirección distinta (pendiente).
        from apps.pedidos.services import direccion_en_una_linea, regresar_a_empaque
        pedido = incidencia.pedido
        if pedido is None:
            raise ValueError("La incidencia no está ligada a un pedido.")
        if not pedido.direccion_pendiente:
            raise ValueError(
                "Torre aún no recibe una dirección distinta desde Shopify para este pedido: corrígela "
                "en la orden y espera el sync (o dispáralo en Salud de sync) antes de cancelar la guía."
            )
        fresco = regresar_a_empaque(pedido, request.user, motivo=f"Cambio de dirección ({incidencia.folio})")
        responder(
            incidencia, usuario, "mesa",
            f"Guía cancelada y {pedido.folio} regresado a empaquetado con la dirección nueva "
            f"({direccion_en_una_linea(fresco.direccion)}): etiqueta y foto de cierre nuevas.",
            interno=True,
        )
        return f"{pedido.folio} regresó a empaquetado: el piso compra la guía nueva con la dirección corregida."
    if accion == "cerrar":
        _exigir_plan_para_paq(incidencia)
        cerrar(incidencia, request.user)
        return "Incidencia cerrada. Si el pedido ya no tiene incidencias abiertas, quedó liberado."

    if accion == "compensacion_crear":
        from apps.incidencias.services import crear_compensacion, resumen_ejecucion, seleccion_desde_post
        comp = crear_compensacion(
            incidencia, request.POST.get("tipo", ""), request.user, "mesa",
            lineas=seleccion_desde_post(request.POST, incidencia), monto=request.POST.get("monto") or None,
            reembolsar_envio=bool(request.POST.get("reembolsar_envio")),
            avisar_comprador=bool(request.POST.get("avisar_comprador")),
            aprobar=bool(request.POST.get("aprobar")), motivo=request.POST.get("motivo", ""),
        )
        return resumen_ejecucion(comp)
    if accion in ("compensacion_aprobar", "compensacion_ejecutar"):
        from apps.incidencias.services import aprobar_compensacion, ejecutar_reembolso, resumen_ejecucion
        comp = get_object_or_404(Compensacion, pk=request.POST.get("compensacion_id"), incidencia=incidencia)
        if accion == "compensacion_aprobar":
            aprobar_compensacion(comp, request.user)
        else:
            ejecutar_reembolso(comp, request.user)
        return resumen_ejecucion(comp)
    if accion == "compensacion_avanzar":
        from apps.incidencias.services import aprobar_compensacion, resumen_ejecucion
        comp = get_object_or_404(
            Compensacion, pk=request.POST.get("compensacion_id"), incidencia=incidencia
        )
        nuevo = request.POST.get("nuevo_estado", "")
        if nuevo not in dict(Compensacion.ESTADOS):
            raise ValueError("Estado de compensación desconocido.")
        if nuevo == Compensacion.APROBADA:
            aprobar_compensacion(comp, request.user)
            return resumen_ejecucion(comp)
        if nuevo == Compensacion.CANCELADA:
            from apps.incidencias.services import cancelar_reposicion  # lazy por contrato
            cancelar_reposicion(comp, request.user)
            return f"Reposición cancelada: {comp.nota}"
        if nuevo == Compensacion.PAGADA:
            referencia = (request.POST.get("referencia_pago") or "").strip()
            if referencia:
                comp.referencia_pago = referencia
                comp.save(update_fields=["referencia_pago"])
        comp.transicionar(nuevo, actor=request.user, motivo=f"Gestión desde Mesa ({incidencia.folio})")
        return f"Compensación actualizada a {comp.get_estado_display()}."
    if accion == "compensacion_cancelar":
        # Cancelar una reposición que el piso no ha tomado (Chema 2026-10-14): no es un reembolso.
        from apps.incidencias.services import cancelar_reposicion  # lazy por contrato
        comp = get_object_or_404(Compensacion, pk=request.POST.get("compensacion_id"), incidencia=incidencia)
        cancelar_reposicion(comp, request.user)
        return f"Reposición cancelada ({comp.resumen_lineas}): {comp.nota}"

    if accion == "reclamacion_crear":
        carrier = (request.POST.get("carrier") or "").strip()
        if not carrier:
            raise ValueError("Indica el carrier al que se reclama.")
        try:
            monto = Decimal(str(request.POST.get("monto_reclamado", "")).strip())
        except InvalidOperation:
            raise ValueError("Captura el monto reclamado en MXN.")
        if monto <= 0:
            raise ValueError("El monto reclamado debe ser mayor a cero.")
        rec = ReclamacionCarrier.objects.create(
            incidencia=incidencia, carrier=carrier, monto_reclamado=monto
        )
        registrar_evento(
            "reclamacion_carrier", rec.pk, "creada", actor=request.user, cliente=incidencia.cliente,
            delta={"incidencia": incidencia.folio, "carrier": carrier, "monto_reclamado": str(monto)},
        )
        return f"Expediente de reclamación preparado contra {carrier} por ${monto} MXN."

    if accion == "reclamacion_avanzar":
        rec = get_object_or_404(
            ReclamacionCarrier, pk=request.POST.get("reclamacion_id"), incidencia=incidencia
        )
        nuevo = request.POST.get("nuevo_estado", "")
        if nuevo not in dict(ReclamacionCarrier.ESTADOS):
            raise ValueError("Estado de reclamación desconocido.")
        monto_crudo = (request.POST.get("monto_recuperado") or "").strip()
        if nuevo in (ReclamacionCarrier.ACEPTADA, ReclamacionCarrier.PAGADA) and monto_crudo:
            try:
                rec.monto_recuperado = Decimal(monto_crudo)
            except InvalidOperation:
                raise ValueError("El monto recuperado no es un número válido.")
            rec.save(update_fields=["monto_recuperado"])
        rec.transicionar(nuevo, actor=request.user, motivo=f"Gestión desde Mesa ({incidencia.folio})")
        return f"Reclamación actualizada a {rec.get_estado_display()}."

    raise ValueError("Acción desconocida. Recarga la página e intenta de nuevo.")


def _exigir_plan_para_paq(incidencia):
    """Una PAQ no se resuelve ni se cierra a mano con el pedido sin plan de
    cajas (Chema 2026-10-20): reanudarlo así lo mandaba a empacar sin plan.
    Elegir paquetería en la misma incidencia la resuelve sola con plan."""
    from apps.incidencias.models import Incidencia  # lazy: modelo de otra app
    from apps.incidencias.services import pedido_sin_plan  # lazy por contrato

    pedido = incidencia.pedido
    if incidencia.tipo == Incidencia.TIPO_PAQ and pedido is not None and pedido_sin_plan(pedido):
        raise ValueError(
            f"{pedido.folio} sigue sin plan de cajas: elige la paquetería aquí mismo y la incidencia se resuelve sola."
        )


@redirigir_rol(portal="portal:incidencia_detalle")
@rol_requerido("mesa")
def incidencia_detalle(request, pk):
    from apps.envios.models import Guia  # lazy: modelo de otra app
    from apps.incidencias.models import Compensacion, Incidencia, ReclamacionCarrier
    from apps.incidencias.services import lineas_para_compensar, opciones_compensacion  # lazy por contrato
    from apps.envios.services import guias_del_pedido  # lazy por contrato
    from apps.pedidos.reportes import url_orden_shopify  # lazy por contrato

    incidencia = get_object_or_404(
        Incidencia.objects.select_related("cliente", "pedido", "sku"), pk=pk
    )

    if request.method == "POST":
        try:
            exito = _gestionar_incidencia(request, incidencia)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, exito)
        return redirect("mesa:incidencia_detalle", pk=incidencia.pk)

    ahora = timezone.now()
    _anotar_reloj_sla(incidencia, ahora)

    from apps.incidencias.services import reposicion_cancelable  # lazy por contrato

    compensaciones = list(incidencia.compensaciones.all())
    for comp in compensaciones:
        comp.cancelable = reposicion_cancelable(comp)
        comp.siguientes = [
            (estado, dict(Compensacion.ESTADOS)[estado])
            for estado in sorted(Compensacion.TRANSICIONES.get(comp.estado, set()))
        ]
    reclamaciones = list(incidencia.reclamaciones.all())
    for rec in reclamaciones:
        rec.siguientes = [
            (estado, dict(ReclamacionCarrier.ESTADOS)[estado])
            for estado in sorted(ReclamacionCarrier.TRANSICIONES.get(rec.estado, set()))
        ]

    lineas_pedido = lineas_para_compensar(incidencia)
    cajas_rehacer, caja_prefill = _rehacer_entrega(incidencia, lineas_pedido, request.GET.get("caja", ""))
    cajas_contenido = _contenido_cajas_err(incidencia)

    fotos_ids = set()
    if incidencia.pedido is not None:
        fotos_ids = {str(incidencia.pedido.pk), incidencia.pedido.folio}
    fotos = list(
        EvidenciaFoto.objects.filter(
            Q(entidad="incidencia", entidad_id__in={str(incidencia.pk), incidencia.folio})
            | Q(entidad="pedido", entidad_id__in=fotos_ids)
        )
    )

    return render(request, "mesa/incidencia_detalle.html", {
        "seccion": "incidencias",
        "incidencia": incidencia,
        "mensajes_timeline": incidencia.mensajes.all(),
        "compensaciones": compensaciones,
        "reclamaciones": reclamaciones,
        "tipos_compensacion": Compensacion.TIPOS,
        "opciones_compensacion": opciones_compensacion(incidencia),
        "lineas_pedido": lineas_pedido,
        "motivos_sustitucion": Guia.MOTIVOS_SUSTITUCION,
        "reponer": request.GET.get("reponer") == "1" or caja_prefill is not None,
        "cajas_rehacer": cajas_rehacer,
        "caja_prefill": caja_prefill,
        "cajas_contenido": cajas_contenido,
        "motivo_inicial": "producto_erroneo" if incidencia.tipo == Incidencia.TIPO_ERR else "",
        "fotos": fotos,
        "puede_tomar": incidencia.estado == Incidencia.ABIERTA,
        "puede_resolver": incidencia.estado in Incidencia.ESTADOS_ABIERTOS,
        "puede_releer": incidencia.tipo == Incidencia.TIPO_SKU and incidencia.abierta and incidencia.pedido_id is not None,
        **_contexto_cambio_direccion(incidencia),
        **_contexto_sin_paqueteria(incidencia),
        **_contexto_caja_sin_salida(incidencia),
        "url_shopify": url_orden_shopify(incidencia.pedido) if incidencia.pedido else "",
        "guias": guias_del_pedido(incidencia.pedido) if incidencia.pedido else [],
        "puede_cerrar": incidencia.estado == Incidencia.RESUELTA,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Pedidos globales
# ─────────────────────────────────────────────────────────────────────────────


def _pedidos_desasignar_operador(request):
    """Quita el pedido de las manos de su operador (pedidos.desasignar_operador): vuelve a la cola."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import desasignar_operador  # lazy por contrato

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente", "asignado_a", "transferencia_a"), folio=folio)
    quien = pedido.asignado_a or pedido.transferencia_a
    desasignar_operador(pedido, request.user, motivo=(request.POST.get("motivo") or "").strip())
    return f"{quien.username if quien else 'El operador'} ya no tiene {pedido.folio}; vuelve a la cola de Mi turno."


def _pedidos_cancelar(request):
    """Cancela un pedido por folio vía la matriz de pedidos.services.cancelar.

    Regresa el mensaje de éxito según lo que de verdad pasó: cancelado directo,
    restock pendiente en piso o incidencia CAN por cancelación tardía.
    """
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import cancelar  # lazy por contrato

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    motivo = (request.POST.get("motivo") or "").strip()
    if not motivo:
        raise ValueError(
            "Escribe el motivo antes de cancelar: queda en el expediente del pedido."
        )
    cancelar(pedido, actor=request.user, motivo=motivo)
    registrar_evento(
        "pedido", pedido.folio, "cancelacion_solicitada_mesa",
        actor=request.user, cliente=pedido.cliente, motivo=motivo,
    )
    if pedido.estado == Pedido.CANCELADO:
        reingreso = pedido.reingresos.order_by("-creado").first()
        if reingreso is not None:
            return (
                f"{pedido.folio} cancelado: la mercancía pickeada entra como reingreso "
                f"{reingreso.folio} para que el piso la ubique; el resto liberó su reserva."
            )
        return f"{pedido.folio} cancelado y stock liberado."
    return (
        f"{pedido.folio} ya está en la calle: se abrió incidencia CAN. Cuando vuelva, "
        "decide en Recepciones si se reingresa o se da por no recuperado."
    )


def _pedidos_cancelar_guias(request):
    """Botón por renglón (Chema 2026-09-24): cancela las guías de un pedido que
    aún no sale y lo regresa a empaque sin dueño (pedidos.regresar_a_empaque:
    cancelación con el carrier, cierre de cajas borrado, recotización). Para
    rehacer guías compradas con la paquetería equivocada (PED-00108/109)."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import regresar_a_empaque

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    fresco = regresar_a_empaque(pedido, request.user, motivo="Guías canceladas desde Mesa")
    canceladas = [g.numero for g in fresco.guias.all() if g.estado == "CANCELADA"]
    return (
        f"{pedido.folio}: guía{'s' if len(canceladas) != 1 else ''} {', '.join(canceladas) or '—'} cancelada"
        f"{'s' if len(canceladas) != 1 else ''}; el pedido regresó a empaquetado sin dueño. "
        "El piso recompra la guía con 'Reintentar guía' y vuelve a cerrar la caja."
    )


# Estados en los que Mesa puede cambiar la paquetería de un pedido (2026-09-28).
ESTADOS_CAMBIO_PAQUETERIA = ("PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA")


def _precio_caja(caja):
    """"$150.00" para el mensaje de Mesa; la entrega propia no lleva precio (sin
    flota, su costo lo captura Mesa en el pedido: Pedido.costo_entrega_propia)."""
    return "sin costo de carrier" if caja.carrier == "local" else f"${caja.precio_cotizado or 0}"


def _pedidos_costo_entrega_propia(request):
    """Costo de la entrega propia del pedido (Chema 2026-10-06): monto sin IVA,
    vacío = $0; es costo para finanzas, no cobro al cliente."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import fijar_costo_entrega_propia

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    pedido = fijar_costo_entrega_propia(pedido, request.POST.get("costo"), request.user)
    if pedido.costo_entrega_propia is None:
        return f"{pedido.folio}: costo de entrega propia en $0."
    return f"{pedido.folio}: costo de entrega propia ${pedido.costo_entrega_propia} (sin IVA), solo para finanzas."


def _pedidos_cambiar_paqueteria(request):
    """Selector por renglón (Chema 2026-09-28): fuerza la paquetería del pedido
    antes de salir (pedidos.replanear_con_carrier), incluida la salida SIN
    guía de carrier ("local": guía interna, corral SAL-LOCAL, POD en Entregas
    locales) para reposiciones que entrega el cliente o alguien propio."""
    from apps.envios.services import etiqueta_paqueteria
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import replanear_con_carrier

    folio = (request.POST.get("folio") or "").strip()
    carrier = (request.POST.get("carrier") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    resultado = replanear_con_carrier(pedido, carrier, request.user)
    cajas = resultado["cajas"]
    detalle = ", ".join(f"caja {c.numero} {c.carrier} {_precio_caja(c)}" for c in cajas)
    que = "recotizadas" if resultado["modo"] == "recotizadas" else "planeadas"
    etiqueta = etiqueta_paqueteria(carrier) if carrier else "las reglas normales del pedido"
    return f"{pedido.folio} con {etiqueta}: {len(cajas)} caja{'s' if len(cajas) != 1 else ''} {que} ({detalle})."


def _pedidos_reintentar_reservas(request):
    """Botón por renglón: reintenta las reservas de UN pedido (palanca de prioridad)."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import reintentar_reservas_pedido

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido, folio=folio)
    resultado = reintentar_reservas_pedido(pedido, request.user)
    completo = not pedido.lineas.filter(reservada=False).exists()
    return resultado, completo


def _caja_del_post(request):
    """(pedido, caja) de una acción por caja del detalle (folio + caja pk)."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.pedidos.models import Pedido

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    caja = get_object_or_404(Paquete, pk=request.POST.get("caja"), pedido=pedido)
    return pedido, caja


def _pedidos_cambiar_paqueteria_caja(request):
    """Selector por CAJA en el detalle (Chema 2026-09-30): fuerza la paquetería
    de una caja que aún no sale (pedidos.cambiar_paqueteria_caja)."""
    from apps.envios.services import etiqueta_paqueteria
    from apps.pedidos.services import cambiar_paqueteria_caja

    pedido, caja = _caja_del_post(request)
    carrier = (request.POST.get("carrier") or "").strip()  # "" = la caja vuelve a seguir al pedido
    caja = cambiar_paqueteria_caja(pedido, caja, carrier, request.user)
    etiqueta = etiqueta_paqueteria(carrier) if carrier else "las reglas del pedido"
    return (
        f"Caja {caja.numero} de {pedido.folio} con {etiqueta}: {_precio_caja(caja)}"
        + (" · guía cancelada, el piso la recompra." if pedido.estado == "EMPACADO" else ".")
    )


def _pedidos_cancelar_guia_caja(request):
    """Cancela la guía de UNA caja. Caja aún sin salir: se recotiza con las
    reglas del pedido y el pedido regresa a empaque sin dueño para
    recomprarla. Caja ya despachada cuya guía el carrier nunca registró
    (PED-00031, iMile la perdió; Chema 2026-09-30): solo se cancela con el
    carrier para que no la cobre; la caja sigue como despachada."""
    from apps.envios.services import cancelar_guia
    from apps.pedidos.services import cambiar_paqueteria_caja

    pedido, caja = _caja_del_post(request)
    vieja = caja.guia_activa
    if vieja is None:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} no tiene guía que cancelar.")
    if caja.estado == "DESPACHADO":
        cancelar_guia(vieja, request.user, motivo=f"Caja {caja.numero} despachada: el carrier nunca registró la guía; se cancela para que no la cobre.")
        return f"Guía {vieja.numero} de la caja {caja.numero} cancelada con el carrier; la caja sigue como despachada."
    caja = cambiar_paqueteria_caja(pedido, caja, None, request.user)
    return f"Guía {vieja.numero} de la caja {caja.numero} cancelada; {pedido.folio} regresó a empaquetado sin dueño y el piso la recompra."


def _pedidos_cancelar_reposicion(request):
    """Cancelar una reposición desde la tarjeta Cajas del pedido (Chema
    2026-10-14): mientras el piso no la tome (incidencias.cancelar_reposicion)."""
    from apps.incidencias.models import Compensacion  # lazy: modelo de otra app
    from apps.incidencias.services import cancelar_reposicion  # lazy por contrato
    from apps.pedidos.models import Pedido  # lazy: modelo de otra app

    pedido = get_object_or_404(Pedido, folio=request.POST.get("folio"))
    comp = get_object_or_404(Compensacion, pk=request.POST.get("compensacion_id"), incidencia__pedido=pedido)
    cancelar_reposicion(comp, request.user)
    return f"Reposición cancelada ({comp.resumen_lineas}): {comp.nota}"


def _pedidos_cancelar_reposicion_caja(request):
    """Cancelar una reposición ya pickeada o empacada, por caja y pieza por
    pieza (Chema 2026-10-20, PED-00281): la caja queda cancelada en el
    expediente y su contenido va a donde Mesa decidió, o a Reingresos por
    decidir con "Generar reingreso" (incidencias.cancelar_reposicion_empacada)."""
    from apps.incidencias.models import Compensacion  # lazy: modelo de otra app
    from apps.incidencias.services import cancelar_reposicion_empacada, compensaciones_de_caja  # lazy por contrato

    pedido, caja = _caja_del_post(request)
    if request.POST.get("compensacion_id"):
        comp = get_object_or_404(Compensacion, pk=request.POST.get("compensacion_id"), incidencia__pedido=pedido)
    else:
        candidatas = compensaciones_de_caja(caja)
        if not candidatas:
            raise ValueError(f"La caja {caja.numero} no tiene una reposición aprobada que cancelar.")
        comp = candidatas[0]
    generar = request.POST.get("generar_reingreso") == "1"
    motivo = (request.POST.get("motivo") or "").strip()[:300]
    decisiones = [] if generar else _decisiones_del_post(request, pedido)
    cancelar_reposicion_empacada(comp, caja, request.user, decisiones=decisiones, motivo=motivo, generar_reingreso=generar)
    return (
        f"Reposición cancelada ({comp.incidencia.folio}, caja {caja.numero}): "
        + ("su contenido queda en Reingresos por decidir." if generar else "cada pieza quedó donde decidiste.")
    )


def _pedidos_sustituir_guia_caja(request):
    """"Sustituir guía para esta caja" (Chema 2026-10-14): guía nueva para una
    caja que ya salió, con la paquetería y el motivo del modal
    (envios.sustituir_guia); solo Mesa."""
    from apps.envios.services import etiqueta_paqueteria, sustituir_guia  # lazy por contrato

    pedido, caja = _caja_del_post(request)
    nueva = sustituir_guia(caja, request.POST.get("carrier", ""), request.POST.get("motivo", ""), request.user)
    return (
        f"Caja {caja.numero} de {pedido.folio}: guía nueva {nueva.numero} ({etiqueta_paqueteria(nueva.carrier)}, "
        f"${nueva.costo_preferencial}); la guía {nueva.sustituye_a.numero} queda sustituida. La caja no regresa a picking "
        "ni a empaque; el rastreo en Shopify se actualiza en segundo plano."
    )


def _pedidos_quitar_de_salida_caja(request):
    """Botón "Quitar de salida" por caja (Chema 2026-10-05, PED-00319): la
    caja firmó manifiesto pero nunca se fue (guía cancelada, el carrier no la
    recogió) y regresa a bodega (pedidos.quitar_de_salida)."""
    from apps.pedidos.services import quitar_de_salida

    pedido, caja = _caja_del_post(request)
    caja = quitar_de_salida(pedido, caja, request.user, motivo=(request.POST.get("motivo") or "").strip())
    pedido.refresh_from_db()
    if pedido.estado == "EMPACADO":
        que = "no queda ninguna caja fuera: el pedido volvió a empaquetado sin dueño"
    else:
        que = "las demás cajas siguen fuera (parcialmente despachado)"
    return (
        f"Caja {caja.numero} de {pedido.folio} quitada de salida: su contenido regresó al kardex y {que}. "
        "Cámbiale la paquetería aquí mismo y el piso recompra la guía."
    )


def _guia_del_post(request):
    """(pedido, guía) de una acción por guía del detalle (folio + guia pk)."""
    from apps.envios.models import Guia  # lazy: modelo de otra app
    from apps.pedidos.models import Pedido

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    guia = get_object_or_404(Guia.objects.select_related("pedido__cliente"), pk=request.POST.get("guia"), pedido=pedido)
    return pedido, guia


def _pedidos_registrar_reembolso(request):
    """Registra que la paquetería nos reembolsó UNA guía (Chema 2026-10-01):
    monto (default: la tarifa que el cliente pagó), fecha (default hoy; decide
    el corte en que se descuenta), origen y nota."""
    from datetime import date, datetime, time

    from apps.finanzas.cortes import corte_de
    from apps.finanzas.services import registrar_reembolso

    pedido, guia = _guia_del_post(request)
    monto = (request.POST.get("monto") or "").strip() or None
    if monto is not None:
        try:
            monto = Decimal(monto)
        except InvalidOperation:
            raise ValueError("El monto del reembolso no es un número válido.")
    fecha = None
    texto_fecha = (request.POST.get("fecha") or "").strip()
    if texto_fecha:
        try:
            dia = date.fromisoformat(texto_fecha)
        except ValueError:
            raise ValueError("La fecha del reembolso no se entiende (usa AAAA-MM-DD).")
        if dia != timezone.localdate():
            fecha = timezone.make_aware(datetime.combine(dia, time(12, 0)))
    origen = request.POST.get("origen") or "ajuste"
    if origen not in ("reclamacion", "ajuste"):
        raise ValueError("Origen del reembolso desconocido.")
    reembolso = registrar_reembolso(
        guia, request.user, origen=origen, monto=monto, fecha=fecha, nota=(request.POST.get("nota") or "").strip(),
    )
    return (
        f"Reembolso de ${reembolso.monto} de la guía {guia.numero} registrado: se descuenta a "
        f"{pedido.cliente.nombre} en el corte {corte_de(reembolso.fecha).etiqueta_corta}."
    )


def _pedidos_quitar_reembolso(request):
    """Quita un reembolso registrado por error (auditado en la guía)."""
    from apps.finanzas.models import ReembolsoGuia
    from apps.finanzas.services import quitar_reembolso
    from apps.pedidos.models import Pedido

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido, folio=folio)
    reembolso = get_object_or_404(
        ReembolsoGuia.objects.select_related("guia"), pk=request.POST.get("reembolso"), guia__pedido=pedido,
    )
    numero, monto = reembolso.guia.numero, reembolso.monto
    quitar_reembolso(reembolso, request.user, motivo=(request.POST.get("motivo") or "").strip())
    return f"Reembolso de ${monto} de la guía {numero} quitado."


def _pedidos_replanear_cajas(request):
    """Botón "Replanear cajas" del detalle: rehace el plan de la ola en bodega."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import replanear_cajas

    folio = (request.POST.get("folio") or "").strip()
    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), folio=folio)
    estado_antes = pedido.estado
    nuevas = replanear_cajas(pedido, request.user)
    detalle = ", ".join(f"caja {c.numero} {c.carrier} {_precio_caja(c)}" for c in nuevas)
    mensaje = f"{pedido.folio} replaneado: {len(nuevas)} caja{'s' if len(nuevas) != 1 else ''} ({detalle})."
    if estado_antes != pedido.estado:
        mensaje += " Regresó a empaquetado sin dueño: el piso empaca las cajas nuevas y su guía se compra al terminar."
    return mensaje


def _pedidos_reimprimir_caja(request):
    """Reimprime la etiqueta del carrier y la interna de la guía de una caja."""
    from apps.piso.etiquetas import imprimir_etiqueta  # lazy por contrato

    pedido, caja = _caja_del_post(request)
    guia = caja.guia_activa
    if guia is None:
        raise ValueError(f"La caja {caja.numero} de {pedido.folio} no tiene guía que imprimir.")
    mensajes = [imprimir_etiqueta(guia), imprimir_etiqueta(guia, interna=True)]
    return f"Caja {caja.numero} de {pedido.folio}: " + " · ".join(m for m in mensajes if m)


def _ejecutar_accion_pedido(request):
    """Acciones POST de Mesa sobre un pedido (lista y detalle comparten):
    cancelar, cancelar guías, cambiar paquetería (pedido o caja), reintentar
    reservas, cancelar guía de una caja, quitar una caja de salida,
    reimprimir etiqueta de una caja."""
    accion = request.POST.get("accion")
    simples = {
        "cancelar_guias": _pedidos_cancelar_guias,
        "cancelar": _pedidos_cancelar,
        "desasignar_operador": _pedidos_desasignar_operador,
        "cambiar_paqueteria": _pedidos_cambiar_paqueteria,
        "cambiar_paqueteria_caja": _pedidos_cambiar_paqueteria_caja,
        "cancelar_guia_caja": _pedidos_cancelar_guia_caja,
        "quitar_de_salida_caja": _pedidos_quitar_de_salida_caja,
        "cancelar_reposicion": _pedidos_cancelar_reposicion,
        "cancelar_reposicion_caja": _pedidos_cancelar_reposicion_caja,
        "reingreso_caja": _pedidos_reingreso_caja,
        "parar_en_piso": _pedidos_parar_en_piso,
        "reanudar_det": _pedidos_reanudar_det,
        "sustituir_guia_caja": _pedidos_sustituir_guia_caja,
        "costo_entrega_propia": _pedidos_costo_entrega_propia,
        "reimprimir_caja": _pedidos_reimprimir_caja,
        "replanear_cajas": _pedidos_replanear_cajas,
        "registrar_reembolso_guia": _pedidos_registrar_reembolso,
        "quitar_reembolso_guia": _pedidos_quitar_reembolso,
    }
    if accion in simples:
        try:
            exito = simples[accion](request)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, exito)
    elif accion == "reintentar_reservas":
        try:
            exito, completo = _pedidos_reintentar_reservas(request)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            # Verde solo si el pedido quedó completo; parcial = warning
            # (amarillo): sigue faltando stock y Mesa debe verlo como tal.
            (messages.success if completo else messages.warning)(request, exito)
    else:
        messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")


def _filtrar_pedidos(GET):
    """Queryset de Mesa → Pedidos con los filtros de la URL (cliente, estado,
    canal, q), del más nuevo al más viejo; lo comparten la lista y el
    Anterior / Siguiente del detalle."""
    from apps.pedidos.models import Pedido

    qs = Pedido.objects.select_related("cliente", "tienda").order_by("-creado", "-pk")
    filtro = {k: (GET.get(k) or "").strip() for k in ("cliente", "estado", "canal", "q")}
    if filtro["cliente"]:
        qs = qs.filter(cliente_id=filtro["cliente"])
    if filtro["estado"]:
        qs = qs.filter(estado=filtro["estado"])
    if filtro["canal"]:
        qs = qs.filter(canal=filtro["canal"])
    if filtro["q"]:
        q = filtro["q"]
        qs = qs.filter(
            Q(folio__icontains=q)
            | Q(comprador_nombre__icontains=q)
            | Q(shopify_order_id__icontains=q)
            | Q(shopify_order_name__icontains=q)  # "#4074", como lo busca servicio al cliente
            | Q(pk__in=_pedidos_con_guia(q))  # número de guía, viva o cancelada (Chema 2026-10-05)
        )
    return qs, filtro


def _pedidos_con_guia(q):
    """Ids de pedidos con alguna guía cuyo número contiene `q` (subconsulta:
    no duplica filas ni infla los conteos de la lista)."""
    from apps.envios.models import Guia

    return Guia.objects.filter(numero__icontains=q).values("pedido_id")


def _resumen_cajas(pedido):
    """"2 cajas · 1 en tránsito · 1 empacada" para la lista: el estatus de
    cada caja es el de su guía viva o, sin guía, el de la caja."""
    cajas = list(pedido.paquetes.all())
    if not cajas:
        return ""
    conteo = {}
    for caja in cajas:
        guia = next((g for g in caja.guias.all() if g.es_activa), None)
        etiqueta = guia.get_estado_display() if guia is not None else caja.get_estado_display()
        conteo[etiqueta] = conteo.get(etiqueta, 0) + 1
    partes = " · ".join(f"{n} {e.lower()}" for e, n in conteo.items())
    return f"{len(cajas)} caja{'s' if len(cajas) != 1 else ''} · {partes}"


@rol_requerido("mesa")
def pedidos(request):
    """Lista de pedidos de Mesa (2026-09-30: una fila limpia por pedido, solo
    lectura; el detalle, las guías y TODAS las acciones viven en
    pedido_detalle). El POST se conserva para llamadas viejas."""
    from apps.pedidos.models import Pedido

    if request.method == "POST":
        _ejecutar_accion_pedido(request)
        # PRG conservando los filtros activos (patrón _redirect_inventario):
        # cancelar en tanda no debe rearmar el filtro cada vez.
        qs_filtros = request.GET.urlencode()
        destino = reverse("mesa:pedidos")
        return redirect(f"{destino}?{qs_filtros}" if qs_filtros else destino)

    qs, filtro = _filtrar_pedidos(request.GET)
    qs = qs.prefetch_related("lineas__sku", "paquetes__guias")
    filas = list(qs[:200])
    from apps.incidencias.services import sin_paqueteria_abierta  # lazy por contrato

    sufijo = f"?{request.GET.urlencode()}" if request.GET.urlencode() else ""
    for pedido in filas:
        pedido.pill = PILL_PEDIDO.get(pedido.estado, "")
        pedido.url_detalle = reverse("mesa:pedido_detalle", args=[pedido.pk]) + sufijo
        pedido.resumen_cajas = _resumen_cajas(pedido)
        # Fulfillment parcial: piezas que esperan inventario (tag "Sin inventario").
        pedido.piezas_sin_inventario = sum(l.pendiente_sin_stock for l in pedido.lineas_faltantes)
        # Incidencia interna "Sin paquetería que cotice" abierta: tag con link (2026-09-24).
        pedido.sin_paqueteria = sin_paqueteria_abierta(pedido)

    return render(request, "mesa/pedidos.html", {
        "seccion": "pedidos",
        "pedidos": filas,
        "total": qs.count(),
        "estados": Pedido.ESTADOS,
        "canales": Pedido.CANALES,
        "clientes_filtro": Cliente.objects.all(),
        "filtro": filtro,
    })


def _cajas_para_parar(pedido, cajas):
    """Tarjetas del modal "Parar en piso" (Chema 2026-10-21): por caja en
    bodega, qué lleva, en qué va, si tiene guía comprada (se puede cancelar)
    y, si está empacada, qué regresaría a bodega y a dónde si se desarma."""
    from apps.pedidos.services import contenido_caja_por_pieza, contenido_que_regresa  # lazy por contrato

    etiquetas = {"PLANEADO": "planeada (sin pickear)", "EN_EMPAQUE": "en empaque", "EMPACADO": "empacada"}
    tarjetas = []
    for caja in cajas:
        if caja.estado not in etiquetas:
            continue
        guia = caja.guia
        situacion = etiquetas[caja.estado]
        if pedido.estado == "EN_PICKING" and caja.estado == "EMPACADO":
            situacion = "pesada antes de la última: vuelve a planeada si regresas lo escaneado en picking"
        elif caja.estado == "EMPACADO" and caja.ts_listo_salida:
            situacion = "en el corral, lista para el chofer"
        elif caja.estado == "EMPACADO" and guia is not None:
            situacion = "empacada con guía"
        regreso = []
        # En picking el pick físico aún no corre (se confirma al pesar la última caja): nada que desarmar del kardex.
        if caja.estado in ("EN_EMPAQUE", "EMPACADO") and pedido.estado != "EN_PICKING":
            for _, sku, piezas, ubicacion, lote in contenido_que_regresa(pedido, caja):
                regreso.append({
                    "sku": sku.codigo, "piezas": piezas,
                    "donde": ubicacion.codigo if ubicacion is not None else "sin posición: decide en Reingresos",
                    "lote": lote.codigo if lote is not None else "",
                })
        tarjetas.append({
            "caja": caja, "situacion": situacion,
            "contenido": [f"{unidades} × {sku.codigo}" for _, sku, unidades in contenido_caja_por_pieza(caja)],
            "guia": guia, "puede_cancelar_guia": guia is not None and guia.estado == "GUIA_CREADA",
            "puede_desarmar": caja.estado in ("EN_EMPAQUE", "EMPACADO") and regreso and all(r["donde"] and not r["donde"].startswith("sin posición") for r in regreso),
            "regreso": regreso,
        })
    return tarjetas


def _regreso_picking_para_parar(pedido):
    """Lo escaneado en picking que Mesa puede regresar a sus racks al parar el
    pedido (pedidos.regreso_al_detener, Chema 2026-10-21); None si no está en picking."""
    from apps.pedidos.services import regreso_al_detener  # lazy por contrato

    return regreso_al_detener(pedido) if pedido.estado == "EN_PICKING" else None


def _pedidos_parar_en_piso(request):
    """"Parar en piso" desde Mesa (Chema 2026-10-20): motivo obligatorio y,
    por caja seleccionada, cancelar su guía y sacarla del corral o desarmarla
    para que su contenido vuelva a bodega (pedidos.parar_en_piso)."""
    from apps.pedidos.models import Pedido  # lazy: modelo de otra app
    from apps.pedidos.services import parar_en_piso  # lazy por contrato

    pedido = get_object_or_404(Pedido, folio=request.POST.get("folio"))
    motivo = (request.POST.get("motivo") or "").strip()[:120]
    detalle = (request.POST.get("detalle") or "").strip()[:300]
    if motivo == "Otro" or not motivo:
        motivo = detalle or motivo
    elif detalle:
        motivo = f"{motivo}: {detalle}"
    cajas_guia = [k.split("-")[1] for k, v in request.POST.items() if k.startswith("caja-") and k.endswith("-guia") and v]
    cajas_contenido = [k.split("-")[1] for k, v in request.POST.items() if k.startswith("caja-") and k.endswith("-contenido") and v]
    regresar_picking = bool(request.POST.get("regresar_picking"))
    incidencia = parar_en_piso(
        pedido, request.user, motivo, cajas_guia=cajas_guia, cajas_contenido=cajas_contenido, regresar_picking=regresar_picking,
    )
    folio = f" ({incidencia.folio})" if incidencia is not None else ""
    return (
        f"{pedido.folio} parado en piso{folio}: sale de la cola; Reanudar lo regresa."
        + (f" Guías canceladas: {len(cajas_guia) + len(cajas_contenido)} caja(s) tocada(s)." if cajas_guia or cajas_contenido else "")
        + (" Lo escaneado regresó a sus racks." if regresar_picking else "")
    )


def _posiciones_para_modal():
    """Códigos de las posiciones activas donde vive producto vendible (picking y reserva), para el modal por pieza."""
    from apps.catalogo.models import Ubicacion  # lazy: modelo de otra app

    return list(
        Ubicacion.objects.filter(activo=True, tipo__in=(Ubicacion.PICKING, Ubicacion.RESERVA))
        .order_by("codigo").values_list("codigo", flat=True)
    )


def _piezas_para_modal(pedido, caja):
    """Renglones del modal pieza por pieza (Chema 2026-10-22): por producto de
    la caja, cuántas piezas viajan, qué dice el kardex de ellas (en empaque y
    de dónde se tomaron / ya descontadas de más / ya salieron), la posición y
    el lote prellenados (lo anotado al escanear, o el rack fijo) y los lotes
    del producto para elegir."""
    from django.db.models import Sum

    from apps.catalogo.services import rack_de  # lazy por contrato
    from apps.inventario.models import Saldo  # lazy: modelo de otra app
    from apps.pedidos.models import PickLinea  # lazy: modelo de otra app
    from apps.pedidos.services import contenido_caja_por_pieza, sobre_descontadas  # lazy por contrato

    salio = caja.estado == "DESPACHADO"
    piezas = []
    for linea, sku, unidades in contenido_caja_por_pieza(caja):
        estado_pick = PickLinea.DESPACHADO if salio else PickLinea.EN_EMPAQUE
        picks = list(PickLinea.objects.filter(linea=linea, estado=estado_pick).select_related("ubicacion", "lote").order_by("pk"))
        if salio:
            picks = [p for p in picks if p.caja_id == caja.pk] or picks
        anotado = [p for p in picks if p.ubicacion_id is not None or p.lote_id is not None]
        prefill = anotado[0] if anotado else None
        rack = rack_de(sku)
        en_empaque = Saldo.objects.filter(sku=sku, estado=Saldo.EN_EMPAQUE).aggregate(t=Sum("cantidad"))["t"] or 0
        fuera = unidades if salio else min(unidades, sobre_descontadas(linea))
        origen = ", ".join(
            f"{p.cantidad} de {p.ubicacion.codigo if p.ubicacion_id else 'posición sin registro'}" + (f" lote {p.lote.codigo}" if p.lote_id else "")
            for p in anotado
        )
        if salio:
            kardex = "Ya salió del kardex: lo que regrese entra como retorno a la posición y lote que elijas."
        elif fuera >= unidades:
            kardex = f"Ya descontada{'s' if unidades > 1 else ''} del kardex: su salida se registró de más (manifiesto anterior). Si regresa, entra como retorno; si no regresa, no hay nada que descontar."
        elif fuera:
            kardex = f"{fuera} de {unidades} ya descontada{'s' if fuera > 1 else ''} del kardex (salida registrada de más); el resto sigue en empaque" + (f", tomada de {origen}" if origen else "") + f". En empaque de este producto: {en_empaque}."
        else:
            kardex = "En empaque, no descontada" + ("s" if unidades > 1 else "") + (f": tomada{'s' if unidades > 1 else ''} de {origen}" if origen else " (sin registro de posición)") + f". En empaque de este producto: {en_empaque}."
        piezas.append({
            "sku_id": sku.pk, "codigo": sku.codigo, "descripcion": sku.descripcion, "unidades": unidades,
            "requiere_lote": sku.requiere_lote, "kardex": kardex, "en_empaque": not salio and fuera < unidades,
            "descontar_default": not salio and fuera < unidades,
            "prefill_ubicacion": (prefill.ubicacion.codigo if prefill is not None and prefill.ubicacion_id else (rack.codigo if rack is not None else "")),
            "prefill_lote": (prefill.lote.codigo if prefill is not None and prefill.lote_id else ""),
            "lotes": list(sku.lotes.order_by("fecha_caducidad", "codigo").values_list("codigo", flat=True)),
        })
    return piezas


def _decisiones_del_post(request, pedido):
    """[{"sku", "cantidad", "destino", "ubicacion", "lote", "descontar", "motivo"}]
    a partir de los renglones del modal pieza por pieza (pieza-<sku>-<n>-*)."""
    import re

    from apps.catalogo.models import SKU, Lote, Ubicacion  # lazy: modelos de otra app

    patron = re.compile(r"^pieza-(\d+)-(\d+)-cantidad$")
    motivo = (request.POST.get("motivo") or "").strip()[:300]
    decisiones = []
    for clave in sorted(request.POST, key=lambda k: tuple(int(x) for x in patron.match(k).groups()) if patron.match(k) else (0, 0)):
        m = patron.match(clave)
        if not m:
            continue
        pre = f"pieza-{m.group(1)}-{m.group(2)}-"
        sku = get_object_or_404(SKU, pk=m.group(1), cliente=pedido.cliente)
        destino = (request.POST.get(pre + "destino") or "").strip()
        try:
            cantidad = int(request.POST.get(pre + "cantidad") or 0)
        except ValueError:
            raise ValueError("Captura las piezas como número entero.") from None
        codigo_ubic = (request.POST.get(pre + "ubicacion") or "").strip()
        codigo_lote = (request.POST.get(pre + "lote") or "").strip()
        ubicacion = Ubicacion.objects.filter(codigo=codigo_ubic).first() if destino == "bodega" and codigo_ubic else None
        if destino == "bodega" and codigo_ubic and ubicacion is None:
            raise ValueError(f"La posición {codigo_ubic} no existe.")
        lote = Lote.objects.filter(sku=sku, codigo=codigo_lote).first() if destino != "merma" and codigo_lote else None
        if destino != "merma" and codigo_lote and lote is None:
            raise ValueError(f"El lote {codigo_lote} no existe para {sku.codigo}.")
        decisiones.append({
            "sku": sku, "cantidad": cantidad, "destino": destino, "ubicacion": ubicacion, "lote": lote,
            "descontar": request.POST.get(pre + "descontar") == "1", "motivo": motivo,
        })
    if not decisiones:
        raise ValueError("No llegó ninguna decisión por pieza: vuelve a abrir el modal.")
    return decisiones


def _lineas_con_lotes(pedido):
    """Líneas del pedido con `.lotes`: resumen de sus registros de pick (lote
    por pedido, Chema 2026-10-22): de qué posición y lote salió cada pieza y
    en qué va (pickeada, en empaque, salió en la caja N, regresó). Lee
    `lineas.all()` para respetar el prefetch del detalle."""
    from apps.pedidos.models import PickLinea  # lazy: modelo de otra app

    etapas = {
        PickLinea.PICKEADO: "pickeada", PickLinea.EN_EMPAQUE: "en empaque",
        PickLinea.DESPACHADO: "salió", PickLinea.REGRESADO: "regresó",
    }
    lineas = list(pedido.lineas.all())
    resumen = {}
    for p in (
        PickLinea.objects.filter(linea__in=lineas).select_related("ubicacion", "lote", "caja").order_by("pk")
    ):
        clave = (p.estado, p.ubicacion_id, p.lote_id, p.caja_id)
        fila = resumen.setdefault(p.linea_id, {}).setdefault(clave, {
            "piezas": 0,
            "donde": p.ubicacion.codigo if p.ubicacion_id else "sin posición",
            "lote": p.lote.codigo if p.lote_id else "",
            "etapa": etapas.get(p.estado, p.estado) + (f" en la caja {p.caja.numero}" if p.caja_id and p.estado == PickLinea.DESPACHADO else ""),
        })
        fila["piezas"] += p.cantidad
    for linea in lineas:
        linea.lotes = list(resumen.get(linea.pk, {}).values())
    return lineas


def _reposiciones_del_pedido(pedido, compensaciones):
    """Reposiciones aprobadas del pedido para la tarjeta Cajas (Chema
    2026-10-14): se ven desde que se aprueban, aunque sin existencias no haya
    caja planeada; con qué reponen, a qué caja, cuándo se aprobaron, en qué
    van (en espera de existencias / planeada en la caja N / en manos del
    piso) y si todavía se pueden cancelar. Lee `paquetes.all()` y
    `lineas.all()` para respetar el prefetch del detalle."""
    from apps.incidencias.models import Compensacion  # lazy: modelo de otra app
    from apps.incidencias.services import reposicion_cancelable  # lazy por contrato
    from apps.pedidos.services import ESTADOS_OLA_OCUPADA  # lazy por contrato

    aprobadas = [c for c in compensaciones if c.tipo == Compensacion.TIPO_REPOSICION and c.estado == Compensacion.APROBADA]
    if not aprobadas:
        return []
    lineas = {l.pk: l for l in pedido.lineas.all()}
    aprobada_ts = {
        e.entidad_id: e.ts
        for e in EventoAuditoria.objects.filter(
            entidad="compensacion", entidad_id__in=[str(c.pk) for c in aprobadas], accion="transicion_aprobada",
        ).order_by("ts")
    }
    filas = []
    for comp in aprobadas:
        detalle, origen, origen_ids, sin_stock = [], set(), set(), []
        for x in comp.lineas or []:
            detalle.append(f"{x.get('cantidad')}× {x.get('sku')}")
            if x.get("caja"):
                origen.add(x["caja"])
            if x.get("caja_id"):
                origen_ids.add(x["caja_id"])
            linea = lineas.get(x.get("linea_id"))
            if linea is not None and linea.repuestas_sin_stock > 0:
                sin_stock.append(x.get("sku"))
        planeadas = sorted({
            c.numero for c in pedido.paquetes.all() if c.estado != "DESPACHADO"
            for pl in c.lineas.all() if pl.repone_a_id and (not origen_ids or pl.repone_a_id in origen_ids)
        })
        # Estado real por caja (2026-10-22): planeada / empacada / en el corral / salió / cancelada.
        por_caja = {}
        for c in pedido.paquetes.all():
            if any(pl.repone_a_id and (not origen_ids or pl.repone_a_id in origen_ids) for pl in c.lineas.all()):
                por_caja[c.numero] = c
        partes = []
        if pedido.estado in ESTADOS_OLA_OCUPADA and pedido.asignado_a_id:
            partes.append("en manos del piso")
        elif pedido.estado in ESTADOS_OLA_OCUPADA and not por_caja:
            partes.append("en manos del piso")
        if planeadas and pedido.estado not in ESTADOS_OLA_OCUPADA:
            partes.append(f"planeada en la caja {', '.join(str(n) for n in planeadas)} (en bodega, sin picking)")
        for numero, c in sorted(por_caja.items()):
            if c.estado == "EMPACADO":
                partes.append(f"caja {numero} empacada" + (" en el corral" if c.ts_listo_salida else ", en bodega"))
            elif c.estado == "EN_EMPAQUE":
                partes.append(f"caja {numero} en empaque")
            elif c.estado == "DESPACHADO":
                partes.append(f"caja {numero} salió")
            elif c.estado == "CANCELADO":
                partes.append(f"caja {numero} cancelada")
        if sin_stock:
            partes.append(f"en espera de existencias ({', '.join(sin_stock)}): la caja nace cuando entre stock")
        filas.append({
            "comp": comp, "detalle": ", ".join(detalle), "origen": sorted(origen),
            "aprobada": aprobada_ts.get(str(comp.pk)), "estado": " · ".join(partes) or "aprobada",
            "cancelable": reposicion_cancelable(comp),
        })
    return filas


@redirigir_rol(portal="portal:pedido_detalle")
@rol_requerido("mesa")
def pedido_detalle(request, pk):
    """Detalle de UN pedido en Mesa (Chema 2026-09-30): encabezado, cajas con
    su guía y acciones por caja (cambiar paquetería, cancelar guía,
    reimprimir), líneas, línea de tiempo por caja, incidencias y
    compensaciones, fotos, auditoría y las acciones de orden completa.
    Anterior / Siguiente recorren la lista con los mismos filtros."""
    from django.db.models import Max

    from apps.core.models import EvidenciaFoto
    from apps.envios.services import guia_sustituible, intentos_de_entrega, opciones_paqueteria, url_rastreo_carrier
    from apps.incidencias.models import Incidencia
    from apps.incidencias.services import agrupar_por_pedido, cajas_cancelables_de, compensaciones_de_caja, sin_paqueteria_abierta
    from apps.pedidos.linea_tiempo import construir
    from apps.envios.cotizador import unidades_sin_caja  # lazy por contrato
    from apps.pedidos.services import ESTADOS_EN_BODEGA, ESTADOS_OLA_OCUPADA, piezas_reponibles
    from apps.pedidos.models import Pedido
    from apps.pedidos.reportes import url_orden_shopify
    from apps.pedidos.services import direccion_en_una_linea, motivo_no_quitable_de_salida, motivo_sin_pod

    pedido = get_object_or_404(
        Pedido.objects.select_related("cliente", "tienda", "asignado_a")
        .prefetch_related(
            "lineas__sku", "paquetes__guias__sustituye_a", "paquetes__guias__eventos",
            "paquetes__lineas__linea_pedido__sku", "paquetes__lineas__repone_a",
        ),
        pk=pk,
    )
    if request.method == "POST":
        _ejecutar_accion_pedido(request)
        qs_filtros = request.GET.urlencode()
        destino = reverse("mesa:pedido_detalle", args=[pedido.pk])
        return redirect(f"{destino}?{qs_filtros}" if qs_filtros else destino)

    qs, filtro = _filtrar_pedidos(request.GET)
    sufijo = f"?{request.GET.urlencode()}" if request.GET.urlencode() else ""
    anterior = qs.filter(creado__gt=pedido.creado).order_by("creado", "pk").first()
    siguiente = qs.filter(creado__lt=pedido.creado).first()

    puede_cambiar = pedido.estado in ESTADOS_CAMBIO_PAQUETERIA
    # Por caja también con el pedido PARCIALMENTE_DESPACHADO (2026-10-05): la
    # caja que se quedó (o que se quitó de salida) sí cambia de paquetería;
    # las despachadas no (el servicio lo valida igual).
    puede_cambiar_caja = puede_cambiar or pedido.estado == Pedido.PARCIALMENTE_DESPACHADO
    cajas = sorted(pedido.paquetes.all(), key=lambda c: c.numero)
    for caja in cajas:
        caja.guia = next((g for g in caja.guias.all() if g.es_activa), None)
        caja.historial = sorted((g for g in caja.guias.all() if not g.es_activa), key=lambda g: g.pk)
        for g in caja.historial:
            g.url_rastreo = url_rastreo_carrier(g.carrier, g.numero)
        caja.url_rastreo = url_rastreo_carrier(caja.guia.carrier, caja.guia.numero) if caja.guia else ""
        # Salió sin escaneo de Salida (2026-10-12): despachada sin línea de manifiesto.
        caja.sin_escaneo = caja.estado == "DESPACHADO" and not caja.lineas_manifiesto.exists()
        caja.puede_cambiar = puede_cambiar_caja and caja.estado != "DESPACHADO" and (
            caja.guia is None or caja.guia.estado == "GUIA_CREADA"
        )
        # Guía de una caja ya despachada que el carrier nunca registró (perdida): se cancela para que no la cobre.
        caja.guia_perdida = caja.estado == "DESPACHADO" and caja.guia is not None and caja.guia.estado == "GUIA_CREADA"
        caja.puede_cancelar_guia = (caja.puede_cambiar and caja.guia is not None) or caja.guia_perdida
        caja.puede_reimprimir = caja.guia is not None and caja.estado != "DESPACHADO"
        # Firmó manifiesto pero nunca se fue (guía cancelada, sin recolección del carrier): vuelve a bodega.
        caja.puede_quitar_de_salida = not motivo_no_quitable_de_salida(pedido, caja)
        # Entrega propia en reparto: su POD (foto + quién recibió) se registra en Entregas locales.
        caja.puede_pod = caja.guia is not None and caja.guia.carrier == "local" and not motivo_sin_pod(caja.guia)
        # Sustituir guía para esta caja (Chema 2026-10-14): ya salió y el carrier
        # necesita otra guía para entregarla; la vieja queda sustituida.
        caja.guia_sustituible = guia_sustituible(caja)
        caja.puede_sustituir = caja.guia_sustituible is not None
        caja.intentos = intentos_de_entrega(caja.guia)  # intentos de entrega fallidos que reportó el carrier (2026-10-14)
        # Cancelar reposición ya pickeada/empacada (Chema 2026-10-20): por caja, con el modal pieza por pieza.
        caja.compensaciones_cancelables = (
            [c for c in compensaciones_de_caja(caja) if caja.pk in {x.pk for x in cajas_cancelables_de(c)}]
            if caja.estado in ("PLANEADO", "EN_EMPAQUE", "EMPACADO") else []
        )
        caja.puede_cancelar_reposicion = bool(caja.compensaciones_cancelables) and not (pedido.asignado_a_id and not pedido.detenido)
        caja.modal_cancelar_id = f"modal-cancelar-reposicion-{caja.pk}"
        caja.modal_cancelar_titulo = f"Cancelar la reposición de la caja {caja.numero}"
        # Registrar reingreso por caja (Chema 2026-10-20, PED-00151): salió y no se ha entregado ni está ya por decidir.
        caja.puede_reingreso = (
            caja.estado == "DESPACHADO" and caja.reingreso_estado == "" and not caja.regresada
            and caja.regreso_reportado is None and not caja.contenido_erroneo and not pedido.cancelacion_tardia
            and (caja.guia is None or caja.guia.estado != "ENTREGADO")
        )
        caja.modal_reingreso_id = f"modal-reingreso-{caja.pk}"
        caja.modal_reingreso_titulo = f"Registrar reingreso de la caja {caja.numero}"
        caja.piezas_reingreso = _piezas_para_modal(pedido, caja) if caja.puede_reingreso else []
        caja.piezas_modal = _piezas_para_modal(pedido, caja) if caja.puede_cancelar_reposicion else []
        for g in caja.historial:  # `sustituta`: la guía que la reemplazó (sin pegarle a la BD por guía)
            g.sustituta = next((n for n in caja.guias.all() if n.sustituye_a_id == g.pk), None)
    guias_sueltas = [
        (g, url_rastreo_carrier(g.carrier, g.numero))
        for g in sorted(pedido.guias.all(), key=lambda g: g.pk) if g.paquete_id is None
    ]
    # Reembolsos de paquetería por guía (Chema 2026-10-01): toda guía del
    # pedido con la tarifa que el cliente pagó, el corte en que se cobró y
    # los reembolsos registrados; Mesa registra o quita desde aquí.
    from apps.finanzas.cortes import corte_de
    from apps.finanzas.services import tarifa_envio_de, tarifario_de

    tarifas = tarifario_de(pedido.cliente)
    reembolsos_guias = [
        {
            "guia": g, "caja": g.paquete, "tarifa": tarifa_envio_de(g, tarifas),
            "url_rastreo": url_rastreo_carrier(g.carrier, g.numero),
            "cobrada": corte_de(g.creado).etiqueta_corta, "reembolsos": list(g.reembolsos.all()),
        }
        for g in sorted(pedido.guias.select_related("paquete").prefetch_related("reembolsos"), key=lambda g: (g.paquete.numero if g.paquete_id else 0, g.pk))
    ]
    ahora = timezone.now()
    incidencias = (
        Incidencia.objects.filter(pedido=pedido).select_related("cliente", "sku")
        .prefetch_related("compensaciones").annotate(ultimo_mensaje=Max("mensajes__ts"))
    )
    from apps.incidencias.services import bloqueos_de  # lazy por contrato
    bloqueos_pedido = bloqueos_de(pedido) if pedido.detenido else []
    det_abierta = next((b["incidencia"] for b in bloqueos_pedido if b["reanudable"]), None)
    grupos, _ = agrupar_por_pedido([_anotar_reloj_sla(inc, ahora) for inc in incidencias])
    grupo = grupos[0] if grupos else {"pedido": pedido, "incidencias": [], "abiertas": [], "cerradas": 0}
    compensaciones = [c for inc in grupo["incidencias"] for c in inc.compensaciones.all()]
    reposiciones = _reposiciones_del_pedido(pedido, compensaciones)
    fotos = list(EvidenciaFoto.objects.filter(entidad="pedido", entidad_id__in=[str(pedido.pk), pedido.folio]).order_by("ts"))
    eventos = list(
        EventoAuditoria.objects.filter(entidad="pedido", entidad_id__in=[str(pedido.pk), pedido.folio]).order_by("-ts")[:40]
    )
    return render(request, "mesa/pedido_detalle.html", {
        "seccion": "pedidos",
        "pedido": pedido,
        "pill": PILL_PEDIDO.get(pedido.estado, ""),
        "url_shopify": url_orden_shopify(pedido),
        "direccion": pedido.direccion or {},
        "direccion_pendiente": direccion_en_una_linea(pedido.direccion_pendiente) if pedido.direccion_pendiente else "",
        "cajas": cajas,
        "reposiciones": reposiciones,
        "guias_sueltas": guias_sueltas,
        "reembolsos_guias": reembolsos_guias,
        # Entrega propia (2026-10-06): el costo se captura por pedido cuando alguna caja o guía es "local".
        "entrega_propia": any(c.carrier == "local" for c in cajas) or any(g.carrier == "local" for g in pedido.guias.all()),
        "hoy": timezone.localdate().isoformat(),
        "lineas": _lineas_con_lotes(pedido),
        "posiciones": _posiciones_para_modal(),
        # Parar en piso (Chema 2026-10-20): mientras el pedido esté en el piso y no esté ya detenido.
        "puede_parar": pedido.estado in ("PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA", "PARCIALMENTE_DESPACHADO") and not pedido.detenido,
        "motivos_detener": __import__("apps.pedidos.services", fromlist=["MOTIVOS_DETENER"]).MOTIVOS_DETENER,
        "cajas_parar": _cajas_para_parar(pedido, cajas),
        "regreso_picking": _regreso_picking_para_parar(pedido),
        # Reanudar DET desde el pedido (Chema 2026-10-20): solo con una DET abierta; el modal avisa qué seguirá bloqueando.
        "bloqueos": bloqueos_pedido,
        "det_abierta": det_abierta,
        "bloqueos_restantes": [b for b in bloqueos_pedido if not b["reanudable"]],
        "piezas_sin_inventario": sum(l.pendiente_sin_stock for l in pedido.lineas_faltantes),
        # "Rehacer entrega" (2026-10-10): misma regla que reponer_lineas: hay piezas que ya
        # salieron en una caja y ninguna ola en manos del piso (un PENDIENTE que espera
        # inventario para otra reposición también cuenta).
        "puede_rehacer": pedido.estado not in ESTADOS_OLA_OCUPADA and bool(piezas_reponibles(pedido)),
        "filas": construir(Pedido.objects.filter(pk=pedido.pk)),
        "es_mesa": True,
        "sin_pedido": True,
        "grupo": grupo,
        "compensaciones": compensaciones,
        "fotos": fotos,
        "eventos": eventos,
        "sin_paqueteria": sin_paqueteria_abierta(pedido),
        "opciones_paqueteria": opciones_paqueteria(),
        "cancelable": pedido.estado in ESTADOS_CANCELABLES_MESA,
        "puede_desasignar": pedido.estado in ESTADOS_EN_BODEGA and bool(pedido.asignado_a_id or pedido.transferencia_a_id),
        "operador_asignado": (pedido.asignado_a or pedido.transferencia_a).username if (pedido.asignado_a_id or pedido.transferencia_a_id) else "",
        "puede_cambiar_paqueteria": puede_cambiar,
        # Replanear (2026-10-19, PED-00606): también cuando alguna línea no va en ninguna caja.
        "puede_replanear": puede_cambiar and (
            any(c.estado == "PLANEADO" and c.guia is None for c in cajas) or bool(unidades_sin_caja(pedido))
        ),
        "puede_cancelar_guias": (
            pedido.estado == Pedido.GUIA_GENERADA and not pedido.tiene_despachadas
            and not any(c.estado == "DESPACHADO" for c in cajas)
        ),
        "puede_reintentar_reservas": pedido.estado == Pedido.PENDIENTE or (
            pedido.estado == Pedido.PARCIALMENTE_DESPACHADO and pedido.tiene_faltantes
        ),
        "url_lista": reverse("mesa:pedidos") + sufijo,
        "url_anterior": reverse("mesa:pedido_detalle", args=[anterior.pk]) + sufijo if anterior else "",
        "url_siguiente": reverse("mesa:pedido_detalle", args=[siguiente.pk]) + sufijo if siguiente else "",
    })


def _contexto_caja_sin_salida(incidencia):
    """Incidencia interna "Caja sin salida" (Chema 2026-10-12): las cajas del
    pedido que siguen listas en Salida, cada una con "Salió sin registro"
    (la caja ya no está en el corral: se la llevaron sin escanear)."""
    from apps.incidencias.models import Incidencia
    from apps.pedidos.services import cajas_por_salir  # lazy por contrato
    pedido = incidencia.pedido
    if incidencia.tipo != Incidencia.TIPO_CSS or pedido is None or not incidencia.abierta:
        return {}
    if pedido.estado not in ("GUIA_GENERADA", "PARCIALMENTE_DESPACHADO"):
        return {"css_motivo": f"{pedido.folio} está {pedido.get_estado_display().lower()}: ya no tiene cajas listas en Salida."}
    cajas = cajas_por_salir(pedido)
    for caja in cajas:
        caja.guia = caja.guia_activa
    return {"css_cajas": cajas, "css_entero": not cajas}


def _contexto_sin_paqueteria(incidencia):
    """Selector de paquetería en la incidencia interna "Sin paquetería que
    cotice" (Chema 2026-10-01: detalle por paquete, como en el pedido): cada
    caja en bodega con su estatus, guía y su propio selector, para rehacer
    SOLO la que se quedó sin guía; y el selector del pedido entero, que
    replanea la ola en bodega. Con el pedido cancelado o ya en la calle se
    explica por qué no hay nada que replanear."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.envios.services import opciones_paqueteria, url_rastreo_carrier  # lazy por contrato
    from apps.incidencias.models import Incidencia
    pedido = incidencia.pedido
    if incidencia.tipo != Incidencia.TIPO_PAQ or pedido is None or not incidencia.abierta:
        return {}
    en_bodega = pedido.estado in ("PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA", "PARCIALMENTE_DESPACHADO")
    if not en_bodega:
        return {"paq_motivo": f"{pedido.folio} está {pedido.get_estado_display().lower()}: ya no hay cajas en bodega que replanear."}
    cajas = sorted(pedido.paquetes.prefetch_related("guias"), key=lambda c: c.numero)
    for caja in cajas:
        caja.guia = next((g for g in caja.guias.all() if g.es_activa), None)
        caja.url_rastreo = url_rastreo_carrier(caja.guia.carrier, caja.guia.numero) if caja.guia else ""
        caja.puede_cambiar = caja.estado != Paquete.DESPACHADO and (caja.guia is None or caja.guia.estado == "GUIA_CREADA")
        caja.sin_cotizar = caja.estado != Paquete.DESPACHADO and caja.guia is None and not (caja.precio_cotizado or 0)
    return {
        "puede_replanear": pedido.estado != "PARCIALMENTE_DESPACHADO" or bool(pedido.lineas_por_surtir),
        "paq_cajas": cajas,
        "opciones_paqueteria": opciones_paqueteria(),
        "paq_carrier_forzado": pedido.carrier_forzado,
    }


def _resolver_paq_si_cotiza(incidencia, actor):
    """Tras un cambio por caja: si ya ninguna caja en bodega está sin
    paquetería, la incidencia "Sin paquetería" se resuelve y cierra sola."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.incidencias.services import cerrar, resolver  # lazy por contrato

    pedido = incidencia.pedido
    cajas = [c for c in pedido.paquetes.prefetch_related("guias") if c.estado != Paquete.DESPACHADO]
    if not cajas or not incidencia.abierta:
        return False
    sin = [c.numero for c in cajas if c.guia_activa is None and not (c.precio_cotizado or 0)]
    if sin:
        return False
    detalle = ", ".join(f"caja {c.numero} {c.carrier} {_precio_caja(c)}" for c in cajas)
    resolver(incidencia, f"Paquetería elegida por caja desde Mesa: {detalle}.", actor)
    cerrar(incidencia, actor)
    return True


def _contexto_cambio_direccion(incidencia):
    """Qué puede hacer Mesa con una incidencia de dirección (CDR/DIR): regresar
    a empaque si la guía no ha salido, o el aviso de que ya salió y con qué."""
    from apps.envios.models import Paquete  # lazy: modelo de otra app
    from apps.incidencias.models import Incidencia
    pedido = incidencia.pedido
    if incidencia.tipo not in (Incidencia.TIPO_CDR, Incidencia.TIPO_DIR) or pedido is None:
        return {}
    if incidencia.estado not in Incidencia.ESTADOS_ABIERTOS:
        return {}
    from apps.pedidos.services import direccion_en_una_linea
    fuera = [c for c in pedido.paquetes.all() if c.estado == Paquete.DESPACHADO]
    en_calle = pedido.estado not in ("PENDIENTE", "EN_PICKING", "EMPACADO", "GUIA_GENERADA")
    direcciones = {
        "direccion_guia": direccion_en_una_linea(pedido.direccion),
        "direccion_pendiente": direccion_en_una_linea(pedido.direccion_pendiente) if pedido.direccion_pendiente else "",
    }
    if pedido.estado == "GUIA_GENERADA" and not fuera and not pedido.tiene_despachadas:
        return {**direcciones, "puede_regresar_a_empaque": True}
    if en_calle or fuera:
        guias = [g for g in pedido.guias.all() if g.es_activa]
        return {
            **direcciones,
            "direccion_ya_salio": True,
            "cajas_fuera": [c.numero for c in fuera],
            "cajas_dentro": [c.numero for c in pedido.paquetes.all() if c.estado != Paquete.DESPACHADO],
            "guias_fuera": [f"{g.carrier} {g.numero}" for g in guias],
        }
    return {}


@rol_requerido("mesa")
def linea_tiempo(request):
    """Línea de tiempo de pedidos, todos los clientes (Chema 2026-09-25):
    pasos con hora por pedido y, desplegado, cada caja con su manifiesto,
    recolección del carrier, compromiso y eventos de la paquetería."""
    from apps.pedidos.linea_tiempo import construir, filtrar  # lazy por contrato
    from apps.pedidos.models import Pedido

    from apps.envios.models import Guia  # lazy: modelo de otra app

    filtros = {k: (request.GET.get(k) or "").strip() for k in ("desde", "hasta", "estado", "guia_estado", "q", "cliente")}
    qs = Pedido.objects.all().order_by("-creado")
    if filtros["cliente"]:
        qs = qs.filter(cliente_id=filtros["cliente"])
    qs = filtrar(qs, filtros)
    return render(request, "pedidos/linea_tiempo.html", {
        "seccion": "linea_tiempo", "es_mesa": True, "filas": construir(qs[:300], filtros["guia_estado"]),
        "total": qs.count(), "estados": Pedido.ESTADOS, "estados_guia": Guia.ESTADOS,
        "clientes_filtro": Cliente.objects.all(), "filtro": filtros,
        "url_base": reverse("mesa:linea_tiempo"), "url_pedido": "mesa:pedidos",
    })


@rol_requerido("mesa")
def manifiestos(request):
    """Manifiestos de salida (Chema 2026-09-22: se consultan en Mesa): lista
    por fecha y carrier con sus pedidos y el link a la hoja imprimible."""
    from datetime import date as _date

    from apps.envios.models import Manifiesto  # lazy: modelo de otra app

    qs = Manifiesto.objects.select_related("operador").prefetch_related("lineas__pedido")
    carrier = (request.GET.get("carrier") or "").strip()
    fecha = (request.GET.get("fecha") or "").strip()
    if carrier:
        qs = qs.filter(carrier=carrier)
    if fecha:
        try:
            qs = qs.filter(ts__date=_date.fromisoformat(fecha))
        except ValueError:
            fecha = ""
    filas = list(qs[:200])
    firmas = {
        e.entidad_id: e for e in EvidenciaFoto.objects.filter(
            entidad="manifiesto", tipo="firma", entidad_id__in=[h.folio for h in filas],
        ).order_by("ts")
    }
    for hoja in filas:
        lineas = list(hoja.lineas.all())
        hoja.cajas = len(lineas)
        hoja.no_salieron = sum(1 for l in lineas if l.no_salio)  # quitadas de salida después de firmar
        hoja.folios = sorted({l.pedido.folio for l in lineas})
        hoja.firma = firmas.get(hoja.folio)
    return render(request, "mesa/manifiestos.html", {
        "seccion": "manifiestos",
        "manifiestos": filas,
        "total": qs.count(),
        "carriers": sorted(set(Manifiesto.objects.values_list("carrier", flat=True))),
        "filtro": {"carrier": carrier, "fecha": fecha},
    })


def _pods_del_pedido(pedido):
    """{número de guía: {receptor, ts, evidencia}} de los POD registrados
    (eventos pod_entrega_local) para pintar lo ya entregado."""
    pods = {}
    for e in EventoAuditoria.objects.filter(
        entidad="pedido", entidad_id=str(pedido.pk), accion="pod_entrega_local",
    ).order_by("ts"):
        pods[e.delta.get("guia") or ""] = {
            "receptor": e.delta.get("receptor", ""), "ts": e.ts, "evidencia": e.delta.get("evidencia_id"),
        }
    return pods


@rol_requerido("mesa")
def entregas_locales(request):
    """Mesa → Operación → Entregas locales (Chema 2026-10-05: el POD es
    administrativo y vive aquí; Piso ya no lo tiene). Cajas de entrega propia
    (guía interna "local") en reparto, por salir y entregadas hoy; cada caja
    cierra con su propio POD en entrega_local_pedido."""
    from apps.envios.models import Guia, Paquete  # lazy: modelos de otra app
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import motivo_sin_pod

    hoy = timezone.localdate()
    guias = list(
        Guia.objects.filter(carrier="local").exclude(estado__in=list(Guia.ESTADOS_INACTIVOS))
        .select_related("pedido__cliente", "paquete").order_by("pedido_id", "paquete__numero", "pk")
    )
    en_reparto, entregadas_hoy = {}, []
    for g in guias:
        if g.estado == Guia.ENTREGADO:
            if g.ts_ultimo_movimiento and timezone.localtime(g.ts_ultimo_movimiento).date() == hoy:
                entregadas_hoy.append(g)
            continue
        if not motivo_sin_pod(g):
            en_reparto.setdefault(g.pedido_id, {"pedido": g.pedido, "cajas": []})["cajas"].append(g)
    # Por salir: cajas de entrega propia aún en bodega (con o sin guía) y
    # pedidos sin plan con guía local sin salir. Salen al firmar SAL-LOCAL.
    en_bodega = (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA, Pedido.PARCIALMENTE_DESPACHADO)
    por_salir = {}
    for caja in (
        Paquete.objects.filter(carrier="local", pedido__estado__in=en_bodega).exclude(estado=Paquete.DESPACHADO)
        .select_related("pedido__cliente").prefetch_related("guias").order_by("pedido_id", "numero")
    ):
        grupo = por_salir.setdefault(caja.pedido_id, {"pedido": caja.pedido, "cajas": [], "sin_guia": []})
        grupo["cajas"].append(caja.numero)
        if caja.guia_activa is None:
            grupo["sin_guia"].append(caja.numero)
    for g in guias:
        if g.estado == Guia.GUIA_CREADA and g.paquete_id is None and g.pedido.estado in en_bodega:
            por_salir.setdefault(g.pedido_id, {"pedido": g.pedido, "cajas": [], "sin_guia": []})
    receptores = {}
    for e in EventoAuditoria.objects.filter(accion="pod_entrega_local", ts__date=hoy):
        receptores[(e.entidad_id, e.delta.get("guia") or "")] = e.delta.get("receptor", "")
    for g in entregadas_hoy:
        g.receptor = receptores.get((str(g.pedido_id), g.numero), "")
    return render(request, "mesa/entregas_locales.html", {
        "seccion": "entregas_locales",
        "en_reparto": list(en_reparto.values()),
        "por_salir": list(por_salir.values()),
        "entregadas_hoy": entregadas_hoy,
    })


@rol_requerido("mesa")
def entrega_local_pedido(request, pk):
    """POD por caja de un pedido de entrega propia (Chema 2026-10-05): cada
    caja en reparto cierra con su foto y quién recibió (sin verificación de
    edad); la foto del POD anterior se puede reutilizar para la siguiente
    caja. POST guia + recibio + foto_pod (o misma_foto + reusar) →
    pedidos.registrar_pod_caja."""
    from apps.envios.models import Guia  # lazy: modelo de otra app
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import guias_entrega_propia, motivo_sin_pod, registrar_pod_caja

    pedido = get_object_or_404(
        Pedido.objects.select_related("cliente").prefetch_related("guias__paquete__lineas__linea_pedido__sku"), pk=pk,
    )
    guias = guias_entrega_propia(pedido)
    if not guias:
        messages.error(request, f"{pedido.folio} no tiene cajas de entrega propia.")
        return redirect("mesa:entregas_locales")
    fotos_pod = EvidenciaFoto.objects.filter(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod")
    if request.method == "POST":
        guia = get_object_or_404(Guia.objects.select_related("pedido__cliente", "paquete"), pk=request.POST.get("guia"), pedido=pedido)
        reusar = None
        if request.POST.get("misma_foto") == "si":
            reusar = get_object_or_404(fotos_pod, pk=request.POST.get("reusar"))
        try:
            registrar_pod_caja(
                guia, request.user, foto=request.FILES.get("foto_pod"),
                recibio=request.POST.get("recibio"), reusar=reusar,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("mesa:entrega_local_pedido", pk=pedido.pk)
        pedido.refresh_from_db()
        donde = f"Caja {guia.paquete.numero}" if guia.paquete_id else "Pedido"
        recibio = (request.POST.get("recibio") or "").strip()
        if pedido.estado == Pedido.ENTREGADO:
            messages.success(request, f"{donde} de {pedido.folio} entregada a {recibio}. Todas las cajas entregadas: el pedido queda entregado.")
            return redirect("mesa:entregas_locales")
        messages.success(request, f"{donde} de {pedido.folio} entregada a {recibio}; faltan cajas por entregar.")
        return redirect("mesa:entrega_local_pedido", pk=pedido.pk)

    pods = _pods_del_pedido(pedido)
    filas = []
    for g in guias:
        filas.append({
            "guia": g, "caja": g.paquete, "motivo": motivo_sin_pod(g),
            "entregada": g.estado == Guia.ENTREGADO, "pod": pods.get(g.numero),
        })
    return render(request, "mesa/entrega_local_pedido.html", {
        "seccion": "entregas_locales",
        "pedido": pedido,
        "filas": filas,
        "pendientes": [f for f in filas if not f["motivo"]],
        "ultima_foto": fotos_pod.first(),  # la más reciente (ordering -ts)
        "url_detalle": reverse("mesa:pedido_detalle", args=[pedido.pk]),
    })


@rol_requerido("mesa")
def pedido_nuevo(request):
    """Alta manual de un pedido: clientes sin Shopify (mayoreo, B2B, almacenaje).

    Dos pasos porque los SKUs dependen del cliente: primero se elige el
    cliente activo y luego se captura el pedido completo.
    """
    slug = (request.POST.get("cliente") or request.GET.get("cliente") or "").strip()
    if not slug:
        return render(request, "mesa/pedido_form.html", {
            "seccion": "pedidos",
            "cliente_sel": None,
            "clientes_activos": Cliente.objects.filter(activo=True).order_by("nombre"),
        })

    cliente = get_object_or_404(Cliente, slug=slug, activo=True)
    form = FormPedidoManual(cliente, request.POST or None)
    if request.method == "POST" and form.is_valid():
        from apps.pedidos.services import crear_pedido_manual  # lazy por contrato

        try:
            pedido = crear_pedido_manual(
                cliente,
                comprador_nombre=form.cleaned_data["comprador_nombre"],
                comprador_tel=form.cleaned_data.get("comprador_tel", ""),
                comprador_email=form.cleaned_data.get("comprador_email", ""),
                direccion=form.direccion(),
                cp=form.cleaned_data["cp"],
                valor_declarado=form.cleaned_data.get("valor_declarado"),
                nota_regalo=form.cleaned_data.get("nota_regalo", ""),
                lineas=form.cleaned_data["lineas"],
                actor=request.user,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(f"{reverse('mesa:pedido_nuevo')}?cliente={cliente.slug}")
        sin_stock = [linea for linea in pedido.lineas.all() if not linea.reservada]
        ruta = "entrega local" if pedido.es_local else "foráneo"
        exito = (
            f"{pedido.folio} creado para {cliente.nombre} ({ruta}, "
            f"peso estimado {pedido.peso_esperado_gr} g)."
        )
        if sin_stock:
            plural = "renglón" if len(sin_stock) == 1 else "renglones"
            exito += f" OJO: {len(sin_stock)} {plural} sin stock — se abrió incidencia FAL."
        messages.success(request, exito)
        return redirect(f"{reverse('mesa:pedidos')}?q={pedido.folio}")

    return render(request, "mesa/pedido_form.html", {
        "seccion": "pedidos",
        "cliente_sel": cliente,
        "form": form,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Inventario: stock por cliente, ajustes con doble firma y ubicaciones
# ─────────────────────────────────────────────────────────────────────────────

# Últimos movimientos del kardex visibles en el detalle de SKU (solo UI).
KARDEX_MESA_MOVIMIENTOS = 30


def _redirect_inventario(**params):
    """Redirect PRG a mesa:inventario conservando cliente/sku/ver."""
    from urllib.parse import urlencode

    url = reverse("mesa:inventario")
    limpios = {clave: valor for clave, valor in params.items() if valor}
    if limpios:
        url += "?" + urlencode(limpios)
    return redirect(url)


def _filas_inventario_mesa(cliente):
    """Resumen por SKU activo del cliente (servicio resumen_skus, por lote) + resurtido."""
    from apps.catalogo.models import SKU
    from apps.inventario.services import resumen_skus  # lazy por contrato

    skus = list(SKU.objects.filter(cliente=cliente, activo=True).select_related("cliente").order_by("codigo"))
    resumenes = resumen_skus(skus)  # dos consultas para todo el catálogo, no dos por SKU
    filas = []
    for sku in skus:
        fila = resumenes[sku.pk]
        fila["bajo_reorden"] = sku.punto_reorden > 0 and fila["disponible"] <= sku.punto_reorden
        fila["ultimo_conteo_hace"] = (
            _frescura(fila["ultimo_conteo"]) if fila["ultimo_conteo"] else "sin conteo aún"
        )
        filas.append(fila)
    return filas


@rol_requerido("mesa")
def inventario(request):
    """Inventario operativo: resumen por cliente, detalle de SKU con kardex y
    ajuste con doble firma, y catálogo de ubicaciones del Local 380 E.

    El stock solo se LEE aquí; toda mutación pasa por inventario.services.
    """
    from apps.catalogo.models import SKU, Ubicacion
    from apps.inventario.models import Ajuste, Movimiento, Saldo

    if request.method == "POST":
        accion = request.POST.get("accion", "")
        if accion == "ajustar":
            return _inventario_ajustar(request)
        if accion == "ubicacion_nueva":
            return _inventario_ubicacion_nueva(request)
        if accion == "ubicacion_toggle":
            return _inventario_ubicacion_toggle(request)
        if accion == "ubicacion_carriers":
            return _inventario_ubicacion_carriers(request)
        if accion == "ubicacion_medidas":
            return _inventario_ubicacion_medidas(request)
        messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
        return redirect("mesa:inventario")

    contexto = {
        "seccion": "inventario",
        "clientes_activos": Cliente.objects.filter(activo=True).order_by("nombre"),
    }

    if request.GET.get("ver") == "ubicaciones":
        vivos = dict(
            Saldo.objects.filter(cantidad__gt=0)
            .values_list("ubicacion_id")
            .annotate(n=Count("id"))
            .values_list("ubicacion_id", "n")
        )
        ubicaciones = list(Ubicacion.objects.order_by("codigo"))
        for ubic in ubicaciones:
            ubic.saldos_vivos = vivos.get(ubic.pk, 0)
        contexto.update({
            "ver_ubicaciones": True,
            "ubicaciones": ubicaciones,
            "tipos_ubicacion": Ubicacion.TIPOS,
        })
        return render(request, "mesa/inventario.html", contexto)

    slug = (request.GET.get("cliente") or "").strip()
    if not slug:
        return render(request, "mesa/inventario.html", contexto)

    cliente = get_object_or_404(Cliente, slug=slug)
    contexto["cliente_sel"] = cliente

    codigo = (request.GET.get("sku") or "").strip()
    if codigo:
        from apps.inventario.services import resumen_sku  # lazy por contrato

        sku = get_object_or_404(SKU, cliente=cliente, codigo=codigo)
        contexto.update({
            "sku_detalle": sku,
            "resumen": resumen_sku(sku),
            "saldos": list(
                Saldo.objects.filter(sku=sku)
                .select_related("ubicacion", "lote")
                .order_by("ubicacion__codigo", "estado")
            ),
            "movimientos": list(
                Movimiento.objects.filter(sku=sku)
                .select_related("lote")[:KARDEX_MESA_MOVIMIENTOS]
            ),
            "motivos_ajuste": Ajuste.MOTIVOS,
        })
        return render(request, "mesa/inventario.html", contexto)

    filas = _filas_inventario_mesa(cliente)
    totales = {
        clave: sum(fila[clave] for fila in filas)
        for clave in (
            "fisico", "en_recepcion", "vendible", "apartado",
            "en_empaque", "cuarentena", "disponible",
        )
    }
    contexto.update({"filas": filas, "totales": totales})
    return render(request, "mesa/inventario.html", contexto)


def _inventario_ajustar(request):
    """Ajuste con doble firma desde el detalle de SKU (inventario.aplicar_ajuste)."""
    from apps.catalogo.models import SKU

    slug = (request.POST.get("cliente") or "").strip()
    codigo = (request.POST.get("sku") or "").strip()
    cliente = get_object_or_404(Cliente, slug=slug)
    sku = get_object_or_404(SKU, cliente=cliente, codigo=codigo)
    destino = _redirect_inventario(cliente=slug, sku=codigo)

    from apps.inventario.services import aplicar_ajuste  # lazy por contrato
    try:
        try:
            delta = int((request.POST.get("delta") or "").strip())
        except ValueError:
            raise ValueError("Captura el ajuste como número entero con signo (ej. -2 o 5).")
        ajuste = aplicar_ajuste(
            sku, delta, request.POST.get("motivo") or "",
            (request.POST.get("autorizo_1") or "").strip(), request.POST.get("pin_1") or "",
            (request.POST.get("autorizo_2") or "").strip(), request.POST.get("pin_2") or "",
            incidencia_ref=(request.POST.get("incidencia_ref") or "").strip(),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    messages.success(
        request,
        f"Ajuste {ajuste.folio} aplicado: {ajuste.delta:+d} {sku.codigo} ({ajuste.motivo}). "
        f"Firmaron {ajuste.autorizo_1} y {ajuste.autorizo_2}.",
    )
    return destino


@rol_requerido("mesa")
def inventario_exportar_conteo(request):
    """CSV de conteo del cliente (?cliente=slug): plantilla de la reconciliación,
    una fila por anaquel/lote con `contado` prellenado con el vendible actual."""
    from apps.inventario.services import COLUMNAS_CSV_CONTEO, exportar_conteo

    cliente = get_object_or_404(Cliente, slug=(request.GET.get("cliente") or "").strip())
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    fecha = timezone.localdate().isoformat()
    respuesta["Content-Disposition"] = f'attachment; filename="conteo-{cliente.slug}-{fecha}.csv"'
    respuesta.write("\ufeff")
    escritor = csv.writer(respuesta)
    escritor.writerow(COLUMNAS_CSV_CONTEO)
    for fila in exportar_conteo(cliente):
        escritor.writerow([fila[c] for c in COLUMNAS_CSV_CONTEO])
    return respuesta


@rol_requerido("mesa")
def inventario_reacomodo_csv(request):
    """Plan de reacomodo TOTAL del cliente (?cliente=slug) como si la bodega
    estuviera vacía de sus productos, lote por lote: la hoja con la que el piso
    reacomoda (sku, nombre, código de barras, lote, rack, cantidad, desde).
    Solo planea; mover el inventario del sistema es el comando
    replanear_acomodo --aplicar (Chema 2026-09-21)."""
    from apps.catalogo.models import SKU
    from apps.inventario.services import replanear_bodega

    cliente = get_object_or_404(Cliente, slug=(request.GET.get("cliente") or "").strip())
    pasos = replanear_bodega(cliente)["pasos"]
    skus = {s.pk: s for s in SKU.objects.filter(pk__in={p["sku_id"] for p in pasos})}
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    fecha = timezone.localdate().isoformat()
    respuesta["Content-Disposition"] = f'attachment; filename="reacomodo-{cliente.slug}-{fecha}.csv"'
    respuesta.write("\ufeff")
    escritor = csv.writer(respuesta)
    escritor.writerow(["sku", "nombre", "codigo_barras", "lote", "rack", "cantidad", "desde"])
    for p in pasos:
        sku = skus[p["sku_id"]]
        escritor.writerow([
            p["sku"], sku.descripcion, sku.codigo_barras, p["lote"],
            p["ubicacion"] or "SIN ESPACIO (se queda donde está)", p["cantidad"],
            " · ".join(f"{codigo}: {n}" for codigo, n in p["desde"]),
        ])
    return respuesta


@rol_requerido("mesa")
def inventario_reconciliar(request):
    """Reconciliación de inventario por CSV (?cliente=slug), en dos pasos:
    subir el archivo → previa con deltas y avisos → motivo + modal de confirmación
    con el resumen → aplicar firmado por el usuario de Mesa (sin doble PIN).
    El CSV viaja en un hidden entre la previa y el aplicar (sin estado en servidor).
    Toda la lógica vive en inventario.services (previa_reconciliacion, reconciliar_conteo)."""
    from apps.inventario.models import Ajuste
    from apps.inventario.services import leer_csv_conteo, previa_reconciliacion, reconciliar_conteo

    slug = (request.POST.get("cliente") or request.GET.get("cliente") or "").strip()
    cliente = get_object_or_404(Cliente, slug=slug)
    contexto = {
        "seccion": "inventario",
        "cliente": cliente,
        "motivos_ajuste": Ajuste.MOTIVOS,
        "motivo_default": Ajuste.MOTIVO_RECONCILIACION_INV,
    }
    if request.method != "POST":
        return render(request, "mesa/inventario_reconciliar.html", contexto)

    accion = request.POST.get("accion", "")
    if accion == "previa":
        archivo = request.FILES.get("archivo")
        try:
            if archivo is None:
                raise ValueError("Adjunta el CSV de conteo (usa «Exportar conteo» como plantilla).")
            max_mb = settings.TORRE["IMPORT_CSV_MAX_MB"]
            if archivo.size > max_mb * 1024 * 1024:
                raise ValueError(f"El CSV no debe pasar de {max_mb} MB.")
            try:
                texto = archivo.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("El archivo no se pudo leer. Guárdalo como CSV UTF-8 y súbelo de nuevo.")
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, "mesa/inventario_reconciliar.html", contexto)
        nombre = archivo.name
    elif accion == "aplicar":
        texto = request.POST.get("csv_conteo") or ""
        nombre = (request.POST.get("archivo_nombre") or "").strip()
        if not texto.strip():
            messages.error(request, "No llegó el CSV de la previa; vuelve a subirlo.")
            return redirect(f"{reverse('mesa:inventario_reconciliar')}?cliente={cliente.slug}")
    else:
        messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
        return redirect(f"{reverse('mesa:inventario_reconciliar')}?cliente={cliente.slug}")

    filas, errores_archivo = leer_csv_conteo(texto)
    if errores_archivo:
        for error in errores_archivo:
            messages.error(request, error)
        return render(request, "mesa/inventario_reconciliar.html", contexto)

    if accion == "aplicar":
        try:
            resumen = reconciliar_conteo(
                cliente, filas, request.POST.get("motivo") or "", request.user,
                nota=(request.POST.get("nota") or "").strip(), archivo=nombre,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Reconciliación aplicada: {resumen['ajustes']} ajuste(s) "
                f"({', '.join(resumen['folios'][:8])}{'…' if len(resumen['folios']) > 8 else ''}), "
                f"{resumen['omitidos']} renglón(es) sin cambio; {resumen['conteos']} conteo(s) registrados.",
            )
            return _redirect_inventario(cliente=cliente.slug)

    previa = previa_reconciliacion(cliente, filas)
    deltas = [r["delta"] for r in previa["aplicables"]]
    contexto.update({
        "previa": previa,
        "csv_conteo": texto,
        "archivo_nombre": nombre,
        "total_filas": len(filas),
        "resumen": {
            "suben": sum(d for d in deltas if d > 0),
            "bajan": -sum(d for d in deltas if d < 0),
            "lotes_nuevos": sum(1 for r in previa["aplicables"] if r["lote"] and not r["lote_obj"]),
        },
    })
    return render(request, "mesa/inventario_reconciliar.html", contexto)


def _inventario_ubicacion_nueva(request):
    """Alta de una ubicación física del Local 380 E, con auditoría."""
    from apps.catalogo.models import Ubicacion

    destino = _redirect_inventario(ver="ubicaciones")
    codigo = (request.POST.get("codigo") or "").strip().upper()
    tipo = request.POST.get("tipo") or ""
    carriers = _normalizar_carriers(request.POST.get("carriers"))
    try:
        if not codigo:
            raise ValueError("Captura el código de la ubicación (ej. A-02-1).")
        if tipo not in dict(Ubicacion.TIPOS):
            raise ValueError("Elige el tipo de ubicación del catálogo.")
        if carriers and tipo != Ubicacion.SALIDA:
            raise ValueError("Los carriers solo aplican a corrales (tipo salida).")
        if Ubicacion.objects.filter(codigo__iexact=codigo).exists():
            raise ValueError(f"Ya existe la ubicación {codigo}.")
    except ValueError as exc:
        messages.error(request, str(exc))
        return destino
    ubicacion = Ubicacion.objects.create(codigo=codigo, tipo=tipo, carriers=carriers)
    registrar_evento(
        "ubicacion", ubicacion.codigo, "alta", actor=request.user,
        delta={"tipo": tipo, "carriers": carriers},
        motivo="Alta de ubicación desde Mesa de Control",
    )
    messages.success(
        request,
        f"Ubicación {ubicacion.codigo} dada de alta ({ubicacion.get_tipo_display()}).",
    )
    return destino


def _normalizar_carriers(crudo):
    """"a, b ,," → "a,b" (el orden se respeta)."""
    return ",".join(p.strip() for p in (crudo or "").split(",") if p.strip())


def _inventario_ubicacion_medidas(request):
    """Medidas libres (cm) y prioridad de acceso de un anaquel picking/reserva,
    con auditoría. Alto 0 = sin tope; prioridad vacía = no se sugiere."""
    from apps.catalogo.models import Ubicacion

    destino = _redirect_inventario(ver="ubicaciones")
    ubicacion = get_object_or_404(Ubicacion, pk=request.POST.get("ubicacion_id"))
    if ubicacion.tipo not in (Ubicacion.PICKING, Ubicacion.RESERVA):
        messages.error(request, "Las medidas y la prioridad solo aplican a anaqueles de picking o reserva.")
        return destino
    nuevos = {}
    for campo in ("largo_cm", "ancho_cm", "alto_cm", "prioridad"):
        crudo = (request.POST.get(campo) or "").strip()
        if not crudo:
            nuevos[campo] = None if campo == "prioridad" else 0
            continue
        try:
            nuevos[campo] = int(crudo)
        except ValueError:
            messages.error(request, f"{campo} debe ser un número entero.")
            return destino
        if nuevos[campo] < 0 or (campo == "prioridad" and nuevos[campo] < 1):
            messages.error(request, f"{campo} no puede ser negativo (prioridad mínimo 1).")
            return destino
    delta = {c: [getattr(ubicacion, c), v] for c, v in nuevos.items() if getattr(ubicacion, c) != v}
    if not delta:
        messages.info(request, f"{ubicacion.codigo} ya tenía esas medidas y prioridad.")
        return destino
    for campo, valor in nuevos.items():
        setattr(ubicacion, campo, valor)
    ubicacion.save(update_fields=list(nuevos))
    registrar_evento(
        "ubicacion", ubicacion.codigo, "medidas_actualizadas", actor=request.user,
        delta=delta, motivo="Medidas y prioridad del anaquel desde Mesa de Control",
    )
    messages.success(request, f"{ubicacion.codigo}: medidas y prioridad guardadas.")
    return destino


def _inventario_ubicacion_carriers(request):
    """Edita los carriers de un corral (tipo salida), con auditoría."""
    from apps.catalogo.models import Ubicacion

    destino = _redirect_inventario(ver="ubicaciones")
    ubicacion = get_object_or_404(Ubicacion, pk=request.POST.get("ubicacion_id"))
    if ubicacion.tipo != Ubicacion.SALIDA:
        messages.error(request, "Los carriers solo aplican a corrales (tipo salida).")
        return destino
    nuevos = _normalizar_carriers(request.POST.get("carriers"))
    viejos = ubicacion.carriers
    if nuevos == viejos:
        messages.info(request, f"{ubicacion.codigo} ya tenía esos carriers.")
        return destino
    ubicacion.carriers = nuevos
    ubicacion.save(update_fields=["carriers"])
    registrar_evento(
        "ubicacion", ubicacion.codigo, "carriers_actualizados", actor=request.user,
        delta={"antes": viejos, "ahora": nuevos},
        motivo="Editor de corrales de Mesa de Control",
    )
    messages.success(
        request,
        f"Corral {ubicacion.codigo}: {nuevos or 'comodín (todos los carriers no asignados)'}.",
    )
    return destino


def _inventario_ubicacion_toggle(request):
    """Prende/apaga una ubicación. Apagar exige que no queden saldos vivos."""
    from apps.catalogo.models import Ubicacion
    from apps.inventario.models import Saldo

    destino = _redirect_inventario(ver="ubicaciones")
    ubicacion = get_object_or_404(Ubicacion, pk=request.POST.get("ubicacion_id"))
    if ubicacion.activo:
        piezas = (
            Saldo.objects.filter(ubicacion=ubicacion, cantidad__gt=0)
            .aggregate(t=Sum("cantidad"))["t"] or 0
        )
        if piezas > 0:
            messages.error(
                request,
                f"Esa ubicación todavía tiene {piezas} piezas; muévelas antes de apagarla.",
            )
            return destino
    ubicacion.activo = not ubicacion.activo
    ubicacion.save(update_fields=["activo"])
    accion = "activada" if ubicacion.activo else "desactivada"
    registrar_evento(
        "ubicacion", ubicacion.codigo, accion, actor=request.user,
        motivo=f"Ubicación {accion} desde Mesa de Control",
    )
    messages.success(request, f"Ubicación {ubicacion.codigo} {accion}.")
    return destino


# ─────────────────────────────────────────────────────────────────────────────
# Recepciones (ASN): tablero global + captura de avisos que llegan por WhatsApp
# ─────────────────────────────────────────────────────────────────────────────

# Proyección visual del estado de la ASN a la pill del sistema WOP.
PILL_ASN = {
    "ANUNCIADA": "",
    "EN_RECEPCION": "accent",
    "RECIBIDA": "warn",
    "CERRADA": "ok",
}


def _sla_recepcion(orden):
    """(texto, tono) del reloj SLA de la orden; ('', '') si aún no corre.

    Mismo cálculo simple que usa el piso, sobre las properties del modelo.
    """
    restante = orden.sla_restante
    if restante is None:
        return "", ""
    minutos = int(restante.total_seconds() // 60)
    if minutos < 0:
        horas, mins = divmod(-minutos, 60)
        return f"SLA vencido hace {horas} h {mins:02d} min", "crit"
    horas, mins = divmod(minutos, 60)
    tono = "warn" if minutos <= 120 else "ok"
    return f"Quedan {horas} h {mins:02d} min para quedar vendible", tono


def _decorar_asn(orden):
    orden.pill = PILL_ASN.get(orden.estado, "")
    orden.sla_texto, orden.sla_tono = _sla_recepcion(orden)
    lineas = list(orden.lineas.all())
    orden.total_anunciado = sum(l.cantidad_anunciada for l in lineas)
    orden.total_recibido = sum(l.cantidad_recibida + l.cantidad_danada for l in lineas)
    return orden


def _avisar_piso_asn(orden):
    """Web Push al piso: se anunció una recepción. Best-effort TOTAL: un push
    caído o sin VAPID JAMÁS rompe la captura de la ASN."""
    try:
        from apps.mensajeria import push  # lazy por contrato

        cita = (
            orden.fecha_compromiso.strftime("%d/%m")
            if orden.fecha_compromiso else "por confirmar"
        )
        push.enviar_push_a_rol(
            "piso",
            f"🚚 Recepción anunciada {orden.folio}",
            f"{orden.tarimas} tarimas · cita {cita}",
            url="/piso/recepciones/",
        )
    except Exception:
        pass


@rol_requerido("mesa")
def recepciones(request):
    """Tablero de ASNs de TODOS los clientes + alta en dos pasos.

    El aviso que llega por WhatsApp se captura aquí en ≤15 min: primero se
    elige el cliente (los SKUs dependen de él) y luego se llena el anuncio.
    """
    from apps.catalogo.services import lotes_recientes_cliente  # lazy por contrato
    from apps.inventario.models import OrdenEntrada  # lazy: modelo de otra app
    from apps.inventario.services import anunciar_asn, detalle_recepciones, skus_recibibles  # lazy por contrato

    if request.method == "POST" and request.POST.get("accion") in ("reingreso", "no_recuperado"):
        return _recepciones_decidir_reingreso(request)
    if request.method == "POST" and request.POST.get("accion") == "reiniciar_recepcion":
        return _recepciones_reiniciar(request)

    slug = (request.POST.get("cliente") or request.GET.get("cliente") or "").strip()
    cliente = None
    if slug:
        cliente = get_object_or_404(Cliente, slug=slug, activo=True)

    form = None
    lotes_recientes = []
    skus_plantilla = []
    if cliente is not None:
        form = FormAnuncioASNMesa(cliente, request.POST or None, request.FILES or None)
        lotes_recientes = lotes_recientes_cliente(cliente)
        skus_plantilla = list(skus_recibibles(cliente))
        if request.method == "POST" and form.is_valid():
            fecha = form.cleaned_data["fecha_compromiso"]
            tarimas = form.cleaned_data.get("tarimas") or 0
            orden = anunciar_asn(
                cliente, fecha, tarimas, form.cleaned_data["lineas"], request.user,
                origen="mesa", motivo="Capturada por Mesa (aviso fuera del portal)",
            )
            _avisar_piso_asn(orden)
            cita = fecha.strftime("%d/%m/%Y")
            messages.success(
                request,
                f"ASN {orden.folio} capturada para {cliente.nombre} con cita el {cita}. "
                "El piso ya la ve en su lista de recepciones.",
            )
            return redirect("mesa:recepciones")

    estados_abiertos = [
        OrdenEntrada.ANUNCIADA, OrdenEntrada.EN_RECEPCION, OrdenEntrada.RECIBIDA,
    ]
    abiertas = detalle_recepciones(
        _decorar_asn(orden)
        for orden in OrdenEntrada.objects.filter(estado__in=estados_abiertos)
        .select_related("cliente", "pedido").prefetch_related("lineas__sku", "incidencias")
    )
    cerradas = detalle_recepciones(
        _decorar_asn(orden)
        for orden in OrdenEntrada.objects.filter(estado=OrdenEntrada.CERRADA)
        .select_related("cliente").prefetch_related("lineas__sku", "incidencias")[:10]
    )

    # Los reingresos por decidir viven en su propia pantalla desde 2026-10-22 (mesa:reingresos).
    return render(request, "mesa/recepciones.html", {
        "seccion": "recepciones",
        "skus_plantilla": skus_plantilla,
        # codigo → pk para que el navegador vuelque un CSV elegido en los renglones
        "codigos_sku": {s.codigo: s.pk for s in skus_plantilla},
        "abiertas": abiertas,
        "cerradas": cerradas,
        "cliente_captura": cliente,
        "form": form,
        "lotes_recientes": lotes_recientes,
        "clientes_activos": Cliente.objects.filter(activo=True).order_by("nombre"),
    })


def _reporte_dia_datos(request):
    """(fecha, cliente, renglones) del reporte del día de Mesa: toda la bodega o
    un cliente (?cliente=slug), con operador por etapa."""
    from apps.pedidos.reportes import armar_reporte, fecha_desde_get, pedidos_con_actividad  # lazy por contrato

    fecha, valida = fecha_desde_get(request.GET.get("fecha"))
    if not valida:
        messages.warning(request, "Esa fecha no se entiende; se muestra hoy.")
    slug = (request.GET.get("cliente") or "").strip()
    cliente = get_object_or_404(Cliente, slug=slug) if slug else None
    return fecha, cliente, armar_reporte(pedidos_con_actividad(fecha, cliente), con_operador=True)


@rol_requerido("mesa")
def reporte_dia(request):
    """Reporte del día: pedidos con actividad en la fecha, evidencia por etapa
    y quién hizo cada paso. Misma plantilla que el portal (reportes/reporte_dia.html)."""
    from apps.pedidos.reportes import contexto_fecha, resumen_estados  # lazy por contrato

    fecha, cliente, renglones = _reporte_dia_datos(request)
    params = f"?fecha={fecha.isoformat()}" + (f"&cliente={cliente.slug}" if cliente else "")
    contexto = {
        "seccion": "reportes", "es_mesa": True, "cliente": cliente,
        "clientes": Cliente.objects.filter(activo=True).order_by("nombre"),
        "renglones": renglones, "resumen": resumen_estados(renglones),
        "url_base": reverse("mesa:reporte_dia"), "url_csv": reverse("mesa:reporte_dia_csv") + params,
        "url_incidencia": "mesa:incidencia_detalle",
    }
    contexto.update(contexto_fecha(fecha))
    return render(request, "reportes/reporte_dia.html", contexto)


@rol_requerido("mesa")
def reporte_dia_csv(request):
    """El reporte del día como CSV, mismos filtros que la página."""
    from apps.pedidos.reportes import COLUMNAS_CSV, filas_csv  # lazy por contrato

    fecha, cliente, renglones = _reporte_dia_datos(request)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    sufijo = f"-{cliente.slug}" if cliente else ""
    respuesta["Content-Disposition"] = f'attachment; filename="reporte-{fecha.isoformat()}{sufijo}.csv"'
    respuesta.write("\ufeff")
    escritor = csv.writer(respuesta)
    escritor.writerow(COLUMNAS_CSV)
    escritor.writerows(filas_csv(
        renglones, lambda foto: request.build_absolute_uri(reverse("core:evidencia", args=[foto.pk])),
    ))
    return respuesta


@rol_requerido("mesa")
def reporte_reparto(request):
    """Reparto de carriers por porcentajes: por cliente y mes (?mes=AAAA-MM,
    ?cliente=slug), cartas asignadas por carrier contra guías efectivas,
    forzados y bloque en curso. Base para revisar los pesos."""
    from datetime import date, datetime

    from apps.envios.reparto import reporte_mes  # lazy por contrato

    hoy = timezone.localdate()
    try:
        mes = date.fromisoformat((request.GET.get("mes") or "").strip() + "-01")
    except ValueError:
        mes = hoy.replace(day=1)
    siguiente = date(mes.year + (mes.month == 12), mes.month % 12 + 1, 1)
    anterior = date(mes.year - (mes.month == 1), (mes.month - 2) % 12 + 1, 1)
    inicio = timezone.make_aware(datetime.combine(mes, datetime.min.time()))
    fin = timezone.make_aware(datetime.combine(siguiente, datetime.min.time()))
    slug = (request.GET.get("cliente") or "").strip()
    cliente = get_object_or_404(Cliente, slug=slug) if slug else None
    return render(request, "mesa/reporte_reparto.html", {
        "seccion": "reporte_reparto",
        "mes": mes, "anterior": anterior,
        "siguiente": siguiente if siguiente <= hoy else None,
        "cliente": cliente,
        "clientes": Cliente.objects.filter(activo=True).order_by("nombre"),
        "bloques": reporte_mes(inicio, fin, cliente),
    })


def _alcance_reportes(request):
    """(cliente elegido o None = todos, clientes activos) para los reportes de Mesa."""
    slug = (request.GET.get("cliente") or "").strip()
    cliente = get_object_or_404(Cliente, slug=slug) if slug else None
    return cliente, Cliente.objects.filter(activo=True).order_by("nombre")


@rol_requerido("mesa")
def reportes(request):
    """Índice de reportes (apps.reportes) más el reporte del día y el de reparto."""
    from apps.reportes.views import indice  # lazy por contrato

    return render(request, "reportes/indice.html", {
        "seccion": "reportes", "cliente": None, "url_dia": reverse("mesa:reporte_dia"),
        "reportes": indice(request, True, lambda clave: reverse("mesa:reporte", args=[clave])),
    })


@rol_requerido("mesa")
def reporte(request, clave):
    """Un reporte de apps.reportes para todos los clientes o el elegido (?cliente=slug)."""
    from apps.reportes.views import render_reporte  # lazy por contrato

    cliente, clientes = _alcance_reportes(request)
    return render_reporte(
        request, clave, cliente=cliente, clientes=clientes, es_mesa=True,
        url_base=reverse("mesa:reporte", args=[clave]), url_csv=reverse("mesa:reporte_csv", args=[clave]),
        url_indice=reverse("mesa:reportes"), seccion="reportes",
    )


@rol_requerido("mesa")
def reporte_csv(request, clave):
    from apps.reportes.views import csv_reporte  # lazy por contrato

    cliente, clientes = _alcance_reportes(request)
    return csv_reporte(request, clave, cliente=cliente, clientes=clientes, es_mesa=True)


@rol_requerido("mesa")
def recepciones_plantilla(request):
    """Formato CSV del anuncio de ASN de un cliente (?cliente=slug&sku=<pk>&sku=…);
    sin SKUs marcados baja solo el encabezado. Lógica en inventario.services."""
    from apps.inventario.services import COLUMNAS_PLANTILLA_ASN, filas_plantilla_asn  # lazy por contrato

    cliente = get_object_or_404(Cliente, slug=(request.GET.get("cliente") or "").strip(), activo=True)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    hoy = timezone.localdate().strftime("%Y%m%d")
    respuesta["Content-Disposition"] = f'attachment; filename="asn_{cliente.slug}_{hoy}.csv"'
    respuesta.write("\ufeff")
    escritor = csv.writer(respuesta)
    escritor.writerow(COLUMNAS_PLANTILLA_ASN)
    escritor.writerows(filas_plantilla_asn(cliente, request.GET.getlist("sku")))
    return respuesta


def _recepciones_reiniciar(request):
    """Reinicia una recepción abierta (inventario.reiniciar_recepcion): las
    cajas contadas se anulan, el stock recibido se revierte y el piso vuelve
    a escanear todo desde cero; la foto de llegada se queda."""
    from apps.inventario.models import OrdenEntrada
    from apps.inventario.services import reiniciar_recepcion

    orden = get_object_or_404(OrdenEntrada, pk=request.POST.get("orden_id"))
    try:
        r = reiniciar_recepcion(orden, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.warning(
            request,
            f"{orden.folio}: recepción reiniciada. {r['anuladas']} caja(s) anuladas, "
            f"{r['recibidas_revertidas']} regresaron de su tarima"
            + (f"; {r['no_encontradas']} ya no estaban donde se recibieron (apartadas, movidas o dictaminadas)" if r["no_encontradas"] else "")
            + ". El piso vuelve a escanear TODO desde cero.",
        )
    return redirect(reverse("mesa:recepciones") + f"?cliente={orden.cliente.slug}")


def _fecha_o_none(crudo):
    """Fecha ISO del form (vacío = None); ValueError con copy claro."""
    crudo = (crudo or "").strip()
    if not crudo:
        return None
    try:
        return date.fromisoformat(crudo)
    except ValueError:
        raise ValueError("La caducidad no es válida: usa el calendario (AAAA-MM-DD).") from None


def _posiciones_almacen():
    """Posiciones donde Mesa puede contar o mover cajas: picking, reserva y tarimas activas."""
    from apps.catalogo.models import Ubicacion  # lazy: modelo de otra app

    return Ubicacion.objects.filter(tipo__in=(Ubicacion.PICKING, Ubicacion.RESERVA), activo=True).order_by("codigo")


def _posicion(codigo, obligatoria=True):
    from apps.catalogo.models import Ubicacion  # lazy: modelo de otra app

    codigo = (codigo or "").strip()
    if not codigo:
        if obligatoria:
            raise ValueError("Elige una posición de la lista.")
        return None
    ubicacion = Ubicacion.objects.filter(codigo=codigo, activo=True, tipo__in=(Ubicacion.PICKING, Ubicacion.RESERVA)).first()
    if ubicacion is None:
        raise ValueError(f"{codigo} no es una posición de almacén activa.")
    return ubicacion


def _recepcion_editar_accion(request, orden):
    """Despacha la acción del editor de recepción; regresa el mensaje de éxito
    o lanza ValueError. Todas exigen `motivo` (queda en el expediente)."""
    from apps.inventario.models import LineaASN
    from apps.inventario.services import (  # lazy por contrato
        agregar_linea_recepcion, ajustar_conteo, cerrar_recepcion, editar_linea_recepcion, marcar_danadas,
        mover_cajas, skus_recibibles,
    )

    P = request.POST
    accion = P.get("accion", "")
    motivo = P.get("motivo", "")
    actor = request.user
    if accion == "cerrar":
        crudo = (P.get("tarimas_recibidas") or "").strip()
        tarimas = int(crudo) if crudo.isdigit() else orden.tarimas
        cerrar_recepcion(orden, actor, tarimas_recibidas=tarimas)
        return f"{orden.folio} cerrada: todo lo recibido quedó vendible."
    if accion == "agregar":
        sku = skus_recibibles(orden.cliente).filter(pk=P.get("sku_id")).first()
        if sku is None:
            raise ValueError("Elige un producto del cliente (activo, no kit).")
        tarima = _posicion(P.get("tarima"), obligatoria=False)
        linea = agregar_linea_recepcion(
            orden, sku, P.get("lote"), _fecha_o_none(P.get("caducidad")), P.get("anunciadas") or 0, actor, motivo,
            contadas=P.get("contadas") or 0, danadas=P.get("danadas") or 0, tarima=tarima,
        )
        return f"{sku.codigo}{' lote ' + linea.lote_codigo if linea.lote_codigo else ''} agregado a {orden.folio}."
    linea = get_object_or_404(LineaASN.objects.select_related("sku"), pk=P.get("linea_id"), orden=orden)
    etiqueta = f"{linea.sku.codigo}{' lote ' + linea.lote_codigo if linea.lote_codigo else ''}"
    if accion == "linea":
        editar_linea_recepcion(orden, linea, P.get("lote"), _fecha_o_none(P.get("caducidad")), P.get("anunciadas") or 0, actor, motivo)
        return f"Línea de {linea.sku.codigo} corregida."
    if accion == "conteo":
        tarima = _posicion(P.get("tarima"))
        d_c, d_d = ajustar_conteo(orden, linea, tarima, P.get("contadas") or 0, P.get("danadas") or 0, actor, motivo)
        return f"{etiqueta} en {tarima.codigo}: contadas {d_c:+d}, dañadas {d_d:+d}."
    cantidad = P.get("cantidad") or 0
    if accion == "mover":
        origen, destino = _posicion(P.get("origen")), _posicion(P.get("destino"))
        n = mover_cajas(orden, linea, origen, destino, cantidad, actor, motivo)
        return f"{n} de {etiqueta}: {origen.codigo} → {destino.codigo}."
    if accion == "danar":
        origen = _posicion(P.get("origen"))
        n = marcar_danadas(orden, linea, origen, cantidad, actor, motivo)
        return f"{n} de {etiqueta} marcadas dañadas (de {origen.codigo} a cuarentena)."
    raise ValueError("No entendí la acción.")


@rol_requerido("mesa")
def recepcion_editar(request, pk):
    """Editor de recepción (Chema 2026-10-06; por escaneo desde 2026-10-13):
    la tabla "Realidad" de la orden (por producto · lote: anunciadas,
    contadas, dañadas, recibidas y, por tarima, cuántas cajas hay y cuántas
    siguen sin confirmar) con correcciones por fila: contadas/dañadas de una
    tarima, lote/caducidad/anunciadas, mover cajas a otra posición, marcar
    dañadas, agregar un producto que no venía en la orden y cerrar.
    Posiciones siempre de lista. Cada corrección pide motivo y queda en el
    historial de abajo. Orden cerrada = solo lectura. Es el único lugar
    donde se corrige una recepción: el piso solo escanea."""
    from apps.inventario.models import OrdenEntrada
    from apps.inventario.services import correcciones_recepcion, realidad_recepcion, skus_recibibles

    orden = get_object_or_404(OrdenEntrada.objects.select_related("cliente"), pk=pk)
    if request.method == "POST":
        try:
            exito = _recepcion_editar_accion(request, orden)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, exito)
        return redirect("mesa:recepcion_editar", pk=orden.pk)
    filas = realidad_recepcion(orden)
    por_confirmar = sum(f["por_confirmar"] for f in filas)
    return render(request, "mesa/recepcion_editar.html", {
        "seccion": "recepciones", "orden": orden, "filas": filas,
        "editable": orden.estado != OrdenEntrada.CERRADA,
        "puede_cerrar": orden.estado not in (OrdenEntrada.CERRADA, OrdenEntrada.ANUNCIADA) and por_confirmar == 0,
        "por_confirmar": por_confirmar,
        "posiciones": _posiciones_almacen(),
        "skus": skus_recibibles(orden.cliente),
        "correcciones": correcciones_recepcion(orden),
        "con_diferencias": sum(1 for f in filas if f["diferencia"]),
    })


@rol_requerido("mesa")
def recepcion_realidad_csv(request, pk):
    """La realidad de la orden en CSV (hermano del plan): sku, nombre, barras,
    lote, caducidad, posición, estado, piezas. Para la conciliación del cliente."""
    from apps.inventario.models import OrdenEntrada
    from apps.inventario.services import realidad_csv_filas

    orden = get_object_or_404(OrdenEntrada, pk=pk)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = f'attachment; filename="realidad-{orden.folio}.csv"'
    respuesta.write("\ufeff")
    escritor = csv.writer(respuesta)
    escritor.writerow(["sku", "nombre", "codigo_barras", "lote", "caducidad", "posicion", "estado", "piezas"])
    for fila in realidad_csv_filas(orden):
        escritor.writerow(fila)
    return respuesta


def _recepciones_decidir_reingreso(request):
    """Mesa decide qué pasa con la mercancía de un pedido que ya salió y se
    canceló o retornó: registrar_reingreso (nace la orden de reingreso para el
    piso) o marcar_no_recuperado (queda en el expediente, sin stock)."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import marcar_no_recuperado, registrar_reingreso  # lazy

    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=request.POST.get("pedido_id"))
    motivo = (request.POST.get("motivo") or "").strip()
    try:
        if request.POST.get("accion") == "reingreso":
            orden = registrar_reingreso(pedido, request.user, motivo=motivo)
            messages.success(
                request,
                f"Reingreso {orden.folio} creado para {pedido.folio}: el piso lo recibe y lo ubica.",
            )
        else:
            if not motivo:
                raise ValueError("Escribe el motivo: queda en el expediente del pedido.")
            marcar_no_recuperado(pedido, request.user, motivo=motivo)
            messages.success(request, f"{pedido.folio}: inventario dado por no recuperado.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("mesa:recepciones")


# ─────────────────────────────────────────────────────────────────────────────
# Bloqueados (Chema 2026-10-21): pedidos detenidos con lo que los detiene; Reanudar resuelve solo las DET
# ─────────────────────────────────────────────────────────────────────────────


def _pedidos_reanudar_det(request):
    """"Reanudar pedido" desde el pedido o desde Bloqueados: resuelve SOLO las
    DET abiertas con el motivo fijo "pedido reanudado" (incidencias.
    reanudar_det_desde_pedido); si otra cosa lo detiene, lo dice."""
    from apps.incidencias.services import reanudar_det_desde_pedido  # lazy por contrato
    from apps.pedidos.models import Pedido  # lazy: modelo de otra app

    pedido = get_object_or_404(Pedido, folio=request.POST.get("folio"))
    reanudado, bloqueos = reanudar_det_desde_pedido(pedido, request.user)
    if reanudado:
        return f"{pedido.folio} reanudado: vuelve a la cola del piso, el primero por antigüedad."
    restantes = "; ".join(b["texto"] + (f" ({b['incidencia'].folio})" if b["incidencia"] else "") for b in bloqueos)
    return f"DET resuelta, pero {pedido.folio} sigue detenido por: {restantes}."


@rol_requerido("mesa")
def bloqueados(request):
    """Pedidos detenidos del más viejo al más nuevo con lo que los detiene
    (DET, sin paquetería, producto no registrado, sin plan, sin inventario,
    cancelación pendiente) y "Reanudar" cuando el único bloqueo es la DET."""
    from apps.incidencias.services import pedidos_bloqueados  # lazy por contrato

    if request.method == "POST":
        _ejecutar_accion_pedido(request)
        return redirect("mesa:bloqueados")
    ahora = timezone.now()
    filas = pedidos_bloqueados()
    for fila in filas:
        if fila["det"] is not None:
            _anotar_reloj_sla(fila["det"], ahora)
    return render(request, "mesa/bloqueados.html", {"seccion": "bloqueados", "filas": filas})


# ─────────────────────────────────────────────────────────────────────────────
# Reingresos (Chema 2026-10-21): cajas cuyo contenido hay que acomodar o dar por perdido
# ─────────────────────────────────────────────────────────────────────────────


def _por_que_por_decidir(pedido, caja):
    """Texto corto de por qué la caja está por decidir."""
    if caja.estado == "CANCELADO":
        return "reposición cancelada"
    if caja.regresada:
        return "regresada por el carrier"
    if caja.regreso_reportado:
        return "regreso reportado"
    if caja.contenido_erroneo:
        return "producto erróneo: salió otro producto"
    if pedido.cancelacion_tardia:
        return "cancelación tardía"
    return "por decidir"


def _reingresos_decorados():
    """Pedidos con mercancía por decidir, cada uno con sus cajas por decidir
    (con el modal pieza por pieza listo) y su incidencia abierta."""
    from apps.incidencias.models import Incidencia  # lazy: modelo de otra app
    from apps.pedidos.services import cajas_por_reingresar, reingresos_por_decidir  # lazy por contrato

    pedidos = list(reingresos_por_decidir().prefetch_related("paquetes__guias", "paquetes__lineas__linea_pedido__sku"))
    incidencias = {}
    for inc in Incidencia.objects.filter(
        pedido__in=pedidos, tipo__in=["CAN", "RET", "RF"], estado__in=Incidencia.ESTADOS_ABIERTOS,
    ).order_by("ts_apertura"):
        incidencias[inc.pedido_id] = inc
    for p in pedidos:
        p.incidencia = incidencias.get(p.pk)
        p.cajas_reingreso = cajas_por_reingresar(p)
        p.total_cajas = len(p.paquetes.all())
        for caja in p.cajas_reingreso:
            caja.por_que = _por_que_por_decidir(p, caja)
            caja.modal_id = f"modal-reingreso-{caja.pk}"
            caja.modal_titulo = f"Registrar reingreso de la caja {caja.numero} de {p.folio}"
            caja.piezas_modal = _piezas_para_modal(p, caja)
            caja.guia = caja.guia_activa
    return pedidos


def _reingresos_decidir(request):
    """Decisiones por PEDIDO desde la pantalla de Reingresos: "Crear
    recepción" (registrar_reingreso: nace la orden que el piso recibe y
    acomoda) o "Inventario no recuperado" (queda en el expediente, sin stock)."""
    from apps.pedidos.models import Pedido
    from apps.pedidos.services import marcar_no_recuperado, registrar_reingreso  # lazy

    pedido = get_object_or_404(Pedido.objects.select_related("cliente"), pk=request.POST.get("pedido_id"))
    motivo = (request.POST.get("motivo") or "").strip()
    try:
        if request.POST.get("accion") == "reingreso":
            orden = registrar_reingreso(pedido, request.user, motivo=motivo)
            messages.success(request, f"Recepción {orden.folio} creada para {pedido.folio}: el piso la recibe y la acomoda.")
        else:
            if not motivo:
                raise ValueError("Escribe el motivo: queda en el expediente del pedido.")
            marcar_no_recuperado(pedido, request.user, motivo=motivo)
            messages.success(request, f"{pedido.folio}: inventario dado por no recuperado.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("mesa:reingresos")


@rol_requerido("mesa")
def reingresos(request):
    """Reingresos por decidir (Chema 2026-10-21): cajas que ya salieron y el
    carrier regresó o Mesa reportó, cancelaciones tardías, producto erróneo y
    reposiciones canceladas ya empacadas. Por caja: "Registrar reingreso"
    (modal pieza por pieza: bodega con posición y lote, cuarentena, no
    regresa), "Crear recepción" (al piso, como antes) o "Inventario no
    recuperado"."""
    if request.method == "POST":
        if request.POST.get("accion") in ("reingreso", "no_recuperado"):
            return _reingresos_decidir(request)
        _ejecutar_accion_pedido(request)
        return redirect("mesa:reingresos")
    return render(request, "mesa/reingresos.html", {
        "seccion": "reingresos",
        "pedidos": _reingresos_decorados(),
        "posiciones": _posiciones_para_modal(),
    })


def _pedidos_reingreso_caja(request):
    """"Registrar reingreso" por caja (Chema 2026-10-20, PED-00151): una caja
    despachada que regresó. Si aún no estaba por decidir, se reporta el
    regreso (la guía viva pasa a retorno con su RF); luego, con decisiones,
    se resuelve pieza por pieza, o con "Generar reingreso" se queda en
    Reingresos para decidir después."""
    from apps.pedidos.services import cajas_por_reingresar, reportar_regreso_caja, resolver_contenido_caja  # lazy por contrato

    pedido, caja = _caja_del_post(request)
    generar = request.POST.get("generar_reingreso") == "1"
    motivo = (request.POST.get("motivo") or "").strip()[:300]
    if caja.pk not in {c.pk for c in cajas_por_reingresar(pedido)}:
        reportar_regreso_caja(pedido, caja, request.user, motivo, manual=True)
        pedido.refresh_from_db()
        caja.refresh_from_db()
    if generar:
        return f"Caja {caja.numero} de {pedido.folio}: regreso registrado; queda en Reingresos por decidir."
    decisiones = _decisiones_del_post(request, pedido)
    resolver_contenido_caja(pedido, caja, decisiones, request.user, motivo=motivo)
    return f"Caja {caja.numero} de {pedido.folio}: cada pieza quedó donde decidiste."


# ─────────────────────────────────────────────────────────────────────────────
# Salud de sync
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def sync(request):
    """Salud de sync: frescura por tienda, cola de push de inventario, cola de
    escrituras de fulfillment a Shopify (reintentar / descartar las vencidas)
    y webhooks recientes."""
    from apps.integraciones.models import (
        EscrituraShopifyPendiente, PushInventarioPendiente, SyncLog, Tienda, WebhookEvento,
    )

    horas = settings.TORRE.get("SHOPIFY_REINTENTOS_HORAS", 24)
    if request.method == "POST" and request.POST.get("accion") == "reintentar_vencidas":
        # Chema 2026-10-14: tras otorgar un scope que faltaba (write_fulfillments)
        # quedaban decenas de vencidas; de un jalón se reactivan y el hilo las
        # escribe ya (el cron sigue de red).
        from apps.integraciones.services import drenar_escrituras_en_segundo_plano  # lazy por contrato

        ahora = timezone.now()
        reactivadas = EscrituraShopifyPendiente.objects.filter(vence__lte=ahora).update(vence=ahora + timedelta(hours=horas))
        if reactivadas:
            registrar_evento(
                "sync", "shopify", "escrituras_reactivadas", actor=request.user,
                delta={"reactivadas": reactivadas, "horas": horas},
                motivo="Reintentar todas las vencidas desde Mesa → Sync.",
            )
            drenar_escrituras_en_segundo_plano()
            messages.success(request, f"{reactivadas} escritura(s) vencida(s) reactivada(s): se están escribiendo en segundo plano; lo que no entre lo reintenta el cron hasta {horas} h.")
        else:
            messages.info(request, "No hay escrituras vencidas.")
        return redirect("mesa:sync")
    if request.method == "POST" and request.POST.get("accion") in ("reintentar", "descartar"):
        escritura = get_object_or_404(EscrituraShopifyPendiente, pk=request.POST.get("escritura"))
        if request.POST["accion"] == "reintentar":
            escritura.vence = timezone.now() + timedelta(hours=horas)
            escritura.save(update_fields=["vence"])
            messages.success(request, f"{escritura.get_accion_display()} de {escritura.pedido.folio}: el cron la vuelve a intentar (máximo {horas} h).")
        else:
            registrar_evento(
                "pedido", escritura.pedido_id, "escritura_shopify_descartada", actor=request.user,
                cliente=escritura.pedido.cliente,
                delta={"accion": escritura.accion, "datos": escritura.datos, "intentos": escritura.intentos},
                motivo=(escritura.ultimo_error or "Descartada desde Mesa")[:300],
            )
            escritura.delete()
            messages.warning(request, "Escritura descartada: Shopify no se actualizará; hazlo a mano en el admin si hace falta.")
        return redirect("mesa:sync")

    # El botón "correr push" se quitó (2026-08-11): la cola la drena el cron
    # cada 5 min y el drenado corre síncrono en el request — un click ansioso
    # amarraba un worker contra la API de Shopify. Manual: ssh + manage.py
    # push_inventario.
    ahora = timezone.now()
    tiendas = list(Tienda.objects.select_related("cliente").order_by("cliente__nombre", "dominio"))
    for tienda in tiendas:
        for direccion, attr in ((SyncLog.DIRECCION_INGESTA, "ingesta"), (SyncLog.DIRECCION_PUSH, "push")):
            log = tienda.sync_logs.filter(direccion=direccion).first()  # ordering: -ts
            frescura = _frescura(log.ts if log else None, ahora)
            if log is None:
                pill = "warn"
            elif log.resultado == SyncLog.RESULTADO_ERROR:
                pill = "crit"
            elif ahora - log.ts > timedelta(hours=4):
                pill = "warn"
            else:
                pill = "ok"
            setattr(tienda, f"log_{attr}", log)
            setattr(tienda, f"frescura_{attr}", frescura)
            setattr(tienda, f"pill_{attr}", pill)

    webhooks = list(
        WebhookEvento.objects.select_related("tienda", "tienda__cliente")[:15]
    )
    cola = list(
        PushInventarioPendiente.objects.select_related("sku", "sku__cliente").order_by("creado")
    )
    for pendiente in cola:
        pendiente.frescura = _frescura(pendiente.creado, ahora)
    escrituras = list(
        EscrituraShopifyPendiente.objects.select_related("pedido", "tienda").order_by("creado")
    )
    for escritura in escrituras:
        escritura.frescura = _frescura(escritura.creado, ahora)
        escritura.frescura_intento = _frescura(escritura.ultimo_intento, ahora)
    vencidas = sum(1 for e in escrituras if e.vence <= ahora)

    return render(request, "mesa/sync.html", {
        "seccion": "sync",
        "tiendas": tiendas,
        "webhooks": webhooks,
        "cola": cola,
        "escrituras": escrituras,
        "vencidas": vencidas,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Clientes
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def clientes(request):
    from apps.incidencias.models import Incidencia

    lista = list(
        Cliente.objects.annotate(
            n_tiendas=Count("tiendas", distinct=True),
            n_skus=Count("skus", distinct=True),
            n_pedidos=Count("pedidos", distinct=True),
        ).order_by("nombre")
    )
    abiertas = dict(
        Incidencia.objects.filter(estado__in=Incidencia.ESTADOS_ABIERTOS)
        .values_list("cliente_id")
        .annotate(n=Count("id"))
    )
    for cliente in lista:
        cliente.n_incidencias_abiertas = abiertas.get(cliente.pk, 0)

    return render(request, "mesa/clientes.html", {"seccion": "clientes", "clientes": lista})


@rol_requerido("mesa")
def cliente_detalle(request, pk):
    from apps.envios.models import ReglaEnvio
    from apps.incidencias.models import Incidencia
    from apps.integraciones.models import SyncLog, Tienda
    from apps.mensajeria.models import PlantillaMensaje
    from apps.pedidos.models import Pedido

    cliente = get_object_or_404(Cliente, pk=pk)

    if request.method == "POST":
        try:
            exito = _gestionar_cliente(request, cliente)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, exito)
        return redirect("mesa:cliente_detalle", pk=cliente.pk)

    ahora = timezone.now()

    # Prefill del form de tienda cuando se pide editar una (?tienda=<pk>).
    tienda_editar = None
    tienda_id = request.GET.get("tienda", "").strip()
    if tienda_id:
        tienda_editar = get_object_or_404(Tienda, pk=tienda_id, cliente=cliente)

    tiendas = list(cliente.tiendas.all())
    for tienda in tiendas:
        log_ingesta = tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_INGESTA).first()
        log_push = tienda.sync_logs.filter(direccion=SyncLog.DIRECCION_PUSH).first()
        tienda.frescura_ingesta = _frescura(log_ingesta.ts if log_ingesta else None, ahora)
        tienda.frescura_push = _frescura(log_push.ts if log_push else None, ahora)

    pedidos_recientes = list(
        Pedido.objects.filter(cliente=cliente).select_related("tienda").order_by("-creado")[:8]
    )
    for pedido in pedidos_recientes:
        pedido.pill = PILL_PEDIDO.get(pedido.estado, "")

    incidencias_abiertas = [
        _anotar_reloj_sla(inc, ahora)
        for inc in Incidencia.objects.filter(
            cliente=cliente, estado__in=Incidencia.ESTADOS_ABIERTOS
        ).select_related("pedido")
    ]

    return render(request, "mesa/cliente_detalle.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "tiendas": tiendas,
        "tienda_editar": tienda_editar,
        "usuarios": cliente.usuarios.select_related("usuario"),
        "n_skus": cliente.skus.count(),
        "pedidos_recientes": pedidos_recientes,
        "incidencias_abiertas": incidencias_abiertas,
        "reglas_envio": ReglaEnvio.objects.filter(Q(cliente=cliente) | Q(cliente__isnull=True)),
        "reparto": _resumen_reparto(cliente),
        "plantillas": PlantillaMensaje.objects.filter(Q(cliente=cliente) | Q(cliente__isnull=True)),
        "correos_incidencias": cliente.correos_incidencias.order_by("correo"),
        "correos_fijos": CorreoIncidencias.objects.filter(cliente__isnull=True, activo=True).count(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Gestión de clientes desde Mesa: alta/edición, tarifario, usuarios, catálogo
# ─────────────────────────────────────────────────────────────────────────────


def _serializar_delta(valor):
    """Valores del delta de auditoría: Decimal como str, fechas en ISO, el resto tal cual."""
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    return valor


def _avisar_reparto(request, form):
    """Flash con el tamaño de bloque y los redondeos de los pesos del reparto."""
    resumen = form.resumen_reparto()
    if resumen is None:
        return
    texto = f"Reparto por porcentajes: bloque de {resumen['tam']} cartas."
    if resumen["redondeados"]:
        texto += " Pesos redondeados al tope del bloque: " + ", ".join(resumen["redondeados"]) + "."
    messages.info(request, texto)


def _resumen_reparto(cliente):
    """Pesos, bloque y siguiente carta del cliente para la ficha; None si no reparte."""
    if cliente.integracion_envios != Cliente.INTEGRACION_REPARTO or not cliente.reparto_pesos:
        return None
    from apps.envios.reparto import posicion, resumen_pesos  # lazy por contrato

    resumen = resumen_pesos(cliente.reparto_pesos)
    resumen["bloque"], resumen["posicion"], _tam = posicion(cliente)
    return resumen


@rol_requerido("mesa")
def cliente_nuevo(request):
    form = FormCliente(request.POST or None)
    if request.method == "POST" and form.is_valid():
        datos = form.datos_cliente()
        cliente = Cliente.objects.create(
            slug=form.cleaned_data["slug"],
            branding=form.branding_actualizado(),
            **datos,
        )
        registrar_evento(
            "cliente", cliente.slug, "alta", actor=request.user, cliente=cliente,
            motivo="Alta desde Mesa de Control",
        )
        messages.success(
            request,
            "Cliente creado. Siguiente: carga sus SKUs, crea su usuario del portal "
            "y revisa su tarifario.",
        )
        _avisar_reparto(request, form)
        return redirect("mesa:cliente_detalle", pk=cliente.pk)
    return render(request, "mesa/cliente_form.html", {
        "seccion": "clientes",
        "form": form,
        "cliente": None,
        "reparto_bloque_max": settings.TORRE.get("REPARTO_BLOQUE_MAX", 100),
    })


@rol_requerido("mesa")
def cliente_editar(request, pk):
    cliente = get_object_or_404(Cliente, pk=pk)
    branding = cliente.branding or {}
    inicial = {
        "nombre": cliente.nombre,
        "slug": cliente.slug,
        "razon_social": cliente.razon_social,
        "rfc": cliente.rfc,
        "contacto_nombre": cliente.contacto_nombre,
        "contacto_whatsapp": cliente.contacto_whatsapp,
        "buffer_stock": cliente.buffer_stock,
        "carrier_preferente": cliente.carrier_preferente,
        "integracion_envios": cliente.integracion_envios,
        "carrier_respaldo": cliente.carrier_respaldo,
        "naked_packing_local": cliente.naked_packing_local,
        "avisos_comprador": cliente.avisos_comprador,
        "umbral_visto_bueno_mxn": cliente.umbral_visto_bueno_mxn,
        "incidencias_auto_pausadas_hasta": cliente.incidencias_auto_pausadas_hasta,
        "guia_de_voz": cliente.guia_de_voz,
        "activo": cliente.activo,
        **{f"peso_{carrier}": peso for carrier, peso in (cliente.reparto_pesos or {}).items()},
        **{f"brand_{clave}": branding.get(clave, "") for clave in CLAVES_BRANDING},
    }
    form = FormCliente(request.POST or None, cliente=cliente, initial=inicial)
    if request.method == "POST" and form.is_valid():
        datos = form.datos_cliente()
        datos["branding"] = form.branding_actualizado(base=cliente.branding)
        delta = {}
        for campo, nuevo in datos.items():
            viejo = getattr(cliente, campo)
            if viejo != nuevo:
                delta[campo] = [_serializar_delta(viejo), _serializar_delta(nuevo)]
                setattr(cliente, campo, nuevo)
        if "reparto_pesos" in delta:
            # Pesos nuevos = bloque nuevo desde el siguiente pedido; el bloque
            # parcial anterior se abandona (se ve en el reporte de reparto).
            delta["reparto_base"] = [cliente.reparto_base, cliente.reparto_cursor]
            cliente.reparto_base = cliente.reparto_cursor
        if delta:
            cliente.save()
            registrar_evento(
                "cliente", cliente.slug, "edicion", actor=request.user, cliente=cliente,
                delta=delta, motivo="Edición desde Mesa de Control",
            )
            messages.success(request, f"Cliente actualizado: cambiaron {len(delta)} campo(s).")
            _avisar_reparto(request, form)
        else:
            messages.success(request, "Sin cambios que guardar: la ficha ya estaba así.")
        return redirect("mesa:cliente_detalle", pk=cliente.pk)
    return render(request, "mesa/cliente_form.html", {
        "seccion": "clientes",
        "form": form,
        "cliente": cliente,
    })


@rol_requerido("mesa")
def cliente_tarifario(request, pk):
    from apps.finanzas.services import tarifario_de  # lazy por contrato

    cliente = get_object_or_404(Cliente, pk=pk)
    default = settings.TORRE["TARIFARIO_DEFAULT"]
    override = cliente.tarifario or {}

    if request.method == "POST":
        form = FormTarifario(request.POST)
        if form.is_valid():
            viejo = cliente.tarifario or {}
            nuevo = form.overrides()
            desde_antes = cliente.facturacion_desde
            cliente.tarifario = nuevo
            cliente.facturacion_desde = form.cleaned_data.get("facturacion_desde")
            cliente.save(update_fields=["tarifario", "facturacion_desde"])
            registrar_evento(
                "cliente", cliente.slug, "tarifario_actualizado", actor=request.user,
                cliente=cliente, delta={
                    "antes": viejo, "despues": nuevo,
                    "facturacion_desde": [str(desde_antes or ""), str(cliente.facturacion_desde or "")],
                },
                motivo="Editor de tarifario de Mesa de Control",
            )
            messages.success(
                request,
                "Tarifario guardado. Solo quedan como propias las tarifas que "
                "difieren del default; el resto sigue el tarifario general.",
            )
            return redirect("mesa:cliente_tarifario", pk=cliente.pk)
    else:
        efectivo = tarifario_de(cliente)
        inicial = {
            campo: efectivo.get(campo, default.get(campo, 0))
            for campo in CAMPOS_TARIFARIO_SIMPLES
        }
        for zona in ZONAS_ENVIO:
            inicial[f"envio_{zona}"] = efectivo.get("envio_bloque", {}).get(
                zona, default.get("envio_bloque", {}).get(zona, 0)
            )
        inicial["facturacion_desde"] = cliente.facturacion_desde
        form = FormTarifario(initial=inicial)

    # Filas para el template: campo + default (help) + pill propio/default.
    filas = []
    for campo in CAMPOS_TARIFARIO_SIMPLES:
        defecto = default.get(campo, 0)
        form.fields[campo].help_text = f"(default: ${defecto})"
        filas.append({"campo": form[campo], "propio": campo in override})
    envio_override = override.get("envio_bloque", {})
    for zona in ZONAS_ENVIO:
        defecto = default.get("envio_bloque", {}).get(zona, 0)
        form.fields[f"envio_{zona}"].help_text = f"(default: ${defecto})"
        filas.append({"campo": form[f"envio_{zona}"], "propio": zona in envio_override})
    filas.append({"campo": form["facturacion_desde"], "propio": cliente.facturacion_desde is not None})

    return render(request, "mesa/cliente_tarifario.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "form": form,
        "filas": filas,
        "tiene_overrides": bool(override),
    })


def _usuario_portal_del_cliente(request, cliente):
    """Usuario portal referido por usuario_id, validado contra el cliente."""
    usuario_id = request.POST.get("usuario_id", "")
    usuario = get_user_model().objects.filter(pk=usuario_id or None).first()
    perfil = getattr(usuario, "perfil", None) if usuario else None
    if (
        usuario is None
        or perfil is None
        or perfil.rol != PerfilUsuario.ROL_PORTAL
        or perfil.cliente_id != cliente.pk
    ):
        raise ValueError("Ese usuario no es del portal de este cliente. Recarga la página e intenta de nuevo.")
    return usuario


def _gestionar_cliente(request, cliente):
    """Despacha la acción POST de la ficha del cliente (usuarios y tiendas).

    Regresa mensaje de éxito o lanza ValueError con copy claro.
    """
    accion = request.POST.get("accion", "")

    if accion == "usuario_nuevo":
        username = (request.POST.get("username") or "").strip()
        if not username:
            raise ValueError("Captura el nombre de usuario para crear el acceso.")
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            raise ValueError(f"El usuario '{username}' ya existe. Elige otro nombre de usuario.")
        nombre = (request.POST.get("nombre") or "").strip()
        partes = nombre.split(" ", 1)
        email = (request.POST.get("email") or "").strip() or f"{username}@torre380e.mx"
        password_capturada = bool((request.POST.get("password") or "").strip())
        password = (request.POST.get("password") or "").strip() or secrets.token_urlsafe(8)
        usuario = User(
            username=username,
            first_name=partes[0] if nombre else "",
            last_name=partes[1] if len(partes) > 1 else "",
            email=email,
        )
        usuario.set_password(password)
        usuario.save()
        PerfilUsuario.objects.create(
            usuario=usuario, rol=PerfilUsuario.ROL_PORTAL, cliente=cliente
        )
        registrar_evento(
            "usuario_portal", username, "alta", actor=request.user, cliente=cliente,
            motivo="Alta de usuario del portal desde Mesa de Control",
        )
        # Con correo real y sin contraseña capturada, el acceso viaja por correo
        # como enlace de un solo uso: la contraseña no se muestra a nadie.
        if not password_capturada and email_real(usuario):
            try:
                enviar_acceso(usuario, cliente=cliente, actor=request.user)
            except Exception as exc:  # servidor de correo caído: el alta ya quedó
                raise ValueError(
                    f"Usuario {username} creado, pero el correo no salió ({exc}). "
                    "Usa «Enviar acceso» para reintentar."
                ) from exc
            return f"Usuario {username} creado. Le enviamos el enlace de acceso a {email}."
        return (
            f"Usuario {username} creado. Contraseña: {password} — compártela por "
            "canal seguro, no se vuelve a mostrar."
        )

    if accion == "correo_incidencias_alta":
        from apps.configuracion.services import agregar_correo_incidencias  # lazy por contrato

        fila = agregar_correo_incidencias(
            cliente, request.POST.get("correo"), request.POST.get("nombre"), actor=request.user,
        )
        return f"{fila.correo} recibirá las incidencias de {cliente.nombre}."

    if accion == "correo_incidencias_baja":
        from apps.configuracion.services import quitar_correo_incidencias  # lazy por contrato

        fila = get_object_or_404(CorreoIncidencias, pk=request.POST.get("correo_id"), cliente=cliente)
        quitar_correo_incidencias(fila, actor=request.user)
        return f"{fila.correo} ya no recibe las incidencias de {cliente.nombre}."

    if accion == "usuario_enviar_acceso":
        usuario = _usuario_portal_del_cliente(request, cliente)
        if not email_real(usuario):
            raise ValueError(
                f"{usuario.username} no tiene un correo real; captúralo en el admin "
                "o usa «Nueva contraseña»."
            )
        try:
            enviar_acceso(usuario, cliente=cliente, actor=request.user,
                          motivo="Reenvío del enlace de acceso desde Mesa de Control")
        except Exception as exc:
            raise ValueError(f"El correo no salió ({exc}). Revisa la configuración de correo.") from exc
        return f"Enlace de acceso enviado a {usuario.email} (vale 72 horas)."

    if accion == "usuario_reset":
        usuario = _usuario_portal_del_cliente(request, cliente)
        password = secrets.token_urlsafe(8)
        usuario.set_password(password)
        usuario.save(update_fields=["password"])
        registrar_evento(
            "usuario_portal", usuario.username, "reset_password", actor=request.user,
            cliente=cliente, motivo="Reset de contraseña desde Mesa de Control",
        )
        return (
            f"Contraseña de {usuario.username} regenerada. Contraseña: {password} — "
            "compártela por canal seguro, no se vuelve a mostrar."
        )

    if accion == "usuario_toggle":
        usuario = _usuario_portal_del_cliente(request, cliente)
        usuario.is_active = not usuario.is_active
        usuario.save(update_fields=["is_active"])
        estado = "activado" if usuario.is_active else "desactivado"
        registrar_evento(
            "usuario_portal", usuario.username, estado, actor=request.user,
            cliente=cliente, motivo="Cambio de acceso desde Mesa de Control",
        )
        return f"Usuario {usuario.username} {estado}."

    if accion == "tienda_guardar":
        return _guardar_tienda(request, cliente)

    raise ValueError("Acción desconocida. Recarga la página e intenta de nuevo.")


def _guardar_tienda(request, cliente):
    """Alta o edición (por tienda_id oculto) de una tienda Shopify del cliente."""
    from apps.integraciones.models import Tienda

    dominio = (request.POST.get("dominio") or "").strip().lower()
    if not dominio:
        raise ValueError("Captura el dominio de la tienda (ej. marca.myshopify.com).")
    token = (request.POST.get("token") or "").strip()
    webhook_secret = (request.POST.get("webhook_secret") or "").strip()
    location_id = (request.POST.get("location_id") or "").strip()
    activo = request.POST.get("activo") == "on"
    # Credenciales de la app custom para conectar por OAuth desde Torre (Chema 2026-10-21).
    client_id = (request.POST.get("client_id") or "").strip()
    client_secret = (request.POST.get("client_secret") or "").strip()
    read_all_orders = request.POST.get("read_all_orders") == "on"

    tienda_id = (request.POST.get("tienda_id") or "").strip()
    duplicada = Tienda.objects.filter(dominio=dominio)
    if tienda_id:
        tienda = get_object_or_404(Tienda, pk=tienda_id, cliente=cliente)
        if duplicada.exclude(pk=tienda.pk).exists():
            raise ValueError(f"Ya hay otra tienda conectada con el dominio {dominio}.")
        delta = {}
        for campo, nuevo in (
            ("dominio", dominio), ("location_id", location_id), ("activo", activo),
            ("client_id", client_id), ("read_all_orders", read_all_orders),
        ):
            viejo = getattr(tienda, campo)
            if viejo != nuevo:
                delta[campo] = [viejo, nuevo]
                setattr(tienda, campo, nuevo)
        # Credenciales vacías en edición = conservar las actuales: el form
        # nunca las prefillea (solo muestra su cola de referencia).
        if token and tienda.token != token:
            tienda.token = token
            delta["token"] = "actualizado (el valor no se registra)"
        if webhook_secret and tienda.webhook_secret != webhook_secret:
            tienda.webhook_secret = webhook_secret
            delta["webhook_secret"] = "actualizado (el valor no se registra)"
        if client_secret and tienda.client_secret != client_secret:
            tienda.client_secret = client_secret
            delta["client_secret"] = "actualizado (el valor no se registra)"
        tienda.save()
        registrar_evento(
            "tienda", tienda.dominio, "edicion", actor=request.user, cliente=cliente,
            delta=delta, motivo="Edición de tienda desde Mesa de Control",
        )
        return f"Tienda {tienda.dominio} actualizada."

    if duplicada.exists():
        raise ValueError(f"Ya hay una tienda conectada con el dominio {dominio}.")
    tienda = Tienda.objects.create(
        cliente=cliente, dominio=dominio, token=token,
        webhook_secret=webhook_secret, location_id=location_id, activo=activo,
        client_id=client_id, client_secret=client_secret, read_all_orders=read_all_orders,
    )
    registrar_evento(
        "tienda", tienda.dominio, "alta", actor=request.user, cliente=cliente,
        delta={"tienda_id": tienda.pk, "con_app": bool(client_id)}, motivo="Alta de tienda desde Mesa de Control",
    )
    if client_id and client_secret and not token:
        return f"Tienda {tienda.dominio} guardada. Ahora Conectar con Shopify desde la tabla."
    return (
        f"Tienda {tienda.dominio} conectada. Pega el webhook en Shopify: "
        f"/hooks/shopify/{tienda.pk}/."
    )


def _cajas_mover(request, cliente):
    """Ledger de cajas: entrada al rack o restock rack → packing, con evento."""
    from apps.catalogo.models import Caja, CajaStock
    from apps.core.services import registrar_evento

    caja = get_object_or_404(Caja, pk=request.POST.get("caja_id"), cliente=cliente)
    try:
        cantidad = int(request.POST.get("cantidad") or 0)
    except (TypeError, ValueError):
        cantidad = 0
    destino = redirect("mesa:cliente_cajas", pk=cliente.pk)
    if cantidad <= 0:
        messages.error(request, "Captura cuántas cajas mueves (entero mayor a cero).")
        return destino
    rack, _ = CajaStock.objects.get_or_create(caja=caja, zona=CajaStock.RACK)
    packing, _ = CajaStock.objects.get_or_create(caja=caja, zona=CajaStock.PACKING)
    if request.POST["accion"] == "entrada_rack":
        rack.cantidad += cantidad
        rack.save(update_fields=["cantidad"])
        accion = "cajas_entrada"
        motivo = f"Entraron {cantidad} caja(s) {caja.nombre} al rack."
    else:
        if rack.cantidad < cantidad:
            messages.error(
                request,
                f"Solo hay {rack.cantidad} caja(s) {caja.nombre} en rack; "
                f"no alcanza para mover {cantidad}.",
            )
            return destino
        rack.cantidad -= cantidad
        packing.cantidad += cantidad
        rack.save(update_fields=["cantidad"])
        packing.save(update_fields=["cantidad"])
        accion = "cajas_restock_packing"
        motivo = f"Restock: {cantidad} caja(s) {caja.nombre} del rack a packing."
    messages.success(request, motivo)
    registrar_evento(
        "caja", caja.pk, accion, actor=request.user, cliente=cliente,
        delta={"cantidad": cantidad, "rack": rack.cantidad, "packing": packing.cantidad},
        motivo=motivo,
    )
    return destino


@rol_requerido("mesa")
def cliente_cajas(request, pk):
    """Catálogo de cajas de empaque del cliente: lista + alta/edición."""
    from apps.catalogo.models import Caja
    from .forms import FormCaja

    cliente = get_object_or_404(Cliente, pk=pk)
    if request.method == "POST" and request.POST.get("accion") in ("entrada_rack", "mover_packing"):
        return _cajas_mover(request, cliente)
    if request.method == "POST":
        form = FormCaja(request.POST)
        if form.is_valid():
            datos = form.datos_caja()
            caja_id = form.cleaned_data.get("caja_id")
            existente = Caja.objects.filter(cliente=cliente, nombre=datos["nombre"])
            if caja_id:
                existente = existente.exclude(pk=caja_id)
            if existente.exists():
                messages.error(request, f"Ya hay una caja llamada {datos['nombre']}.")
            elif caja_id:
                Caja.objects.filter(pk=caja_id, cliente=cliente).update(**datos)
                messages.success(request, f"Caja {datos['nombre']} actualizada.")
            else:
                Caja.objects.create(cliente=cliente, **datos)
                messages.success(request, f"Caja {datos['nombre']} registrada.")
            return redirect("mesa:cliente_cajas", pk=cliente.pk)
        messages.error(request, "Revisa los campos de la caja: nombre, medidas y tara.")
    form = FormCaja()
    editar = None
    if request.GET.get("editar"):
        editar = Caja.objects.filter(pk=request.GET["editar"], cliente=cliente).first()
        if editar is not None:
            form = FormCaja(initial={
                "caja_id": editar.pk, "nombre": editar.nombre,
                "largo_cm": editar.largo_cm, "ancho_cm": editar.ancho_cm,
                "alto_cm": editar.alto_cm, "peso_gr": editar.peso_gr,
                "posicion_rack": editar.posicion_rack,
                "activo": editar.activo,
            })
    cajas = list(Caja.objects.filter(cliente=cliente).prefetch_related("stock"))
    for c in cajas:
        c.en_rack = next((x.cantidad for x in c.stock.all() if x.zona == "rack"), 0)
        c.en_packing = next((x.cantidad for x in c.stock.all() if x.zona == "packing"), 0)
    return render(request, "mesa/cliente_cajas.html", {
        "cliente": cliente,
        "cajas": cajas,
        "form": form,
        "editar": editar,
    })


@rol_requerido("mesa")
def cliente_lotes(request, pk):
    """Lotes del cliente: lista con piezas por lote, alta de lote (SKU + código +
    caducidad) y corrección de caducidad. Toda mutación pasa por catalogo.services."""
    from datetime import date

    from apps.catalogo.models import SKU, Lote
    from apps.catalogo.services import actualizar_caducidad, crear_lote, lotes_cliente

    cliente = get_object_or_404(Cliente, pk=pk)
    destino = redirect("mesa:cliente_lotes", pk=cliente.pk)
    if request.method == "POST":
        accion = request.POST.get("accion", "")
        crudo = (request.POST.get("caducidad") or "").strip()
        try:
            caducidad = date.fromisoformat(crudo) if crudo else None
        except ValueError:
            messages.error(request, "La caducidad no se entiende: usa el calendario (AAAA-MM-DD).")
            return destino
        try:
            if accion == "nuevo":
                sku = get_object_or_404(SKU, cliente=cliente, pk=request.POST.get("sku_id"), es_kit=False)
                lote = crear_lote(sku, request.POST.get("codigo"), caducidad, request.user)
                messages.success(request, f"Lote {lote.codigo} de {sku.codigo} dado de alta.")
            elif accion == "caducidad":
                lote = get_object_or_404(Lote, sku__cliente=cliente, pk=request.POST.get("lote_id"))
                actualizar_caducidad(lote, caducidad, request.user)
                messages.success(request, f"Caducidad de {lote.codigo} ({lote.sku.codigo}) actualizada.")
            else:
                messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return destino

    return render(request, "mesa/cliente_lotes.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "lotes": lotes_cliente(cliente),
        "skus": SKU.objects.filter(cliente=cliente, activo=True, es_kit=False).order_by("codigo"),
        "hoy": timezone.localdate(),
        "dias_sugerencia": settings.TORRE["LOTES_SUGERENCIA_DIAS"],
    })


@rol_requerido("mesa")
def cliente_skus(request, pk):
    from apps.catalogo.models import SKU
    from apps.inventario.services import resumen_skus  # lazy por contrato

    cliente = get_object_or_404(Cliente, pk=pk)

    form = None
    if request.method == "POST":
        accion = request.POST.get("accion", "")
        if accion == "importar_csv":
            try:
                exito = _importar_csv_skus(request, cliente)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, exito)
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "transformar_shopify":
            try:
                contexto = _transformar_shopify(request, cliente)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("mesa:cliente_skus", pk=cliente.pk)
            return render(request, "mesa/cliente_skus_transformar.html", contexto)
        if accion == "descargar_transformado":
            return _descargar_transformado(request, cliente)
        if accion == "categoria_nueva":
            try:
                exito = _skus_categoria_nueva(request, cliente)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, exito)
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "categorias_bulk":
            messages.success(request, _skus_categorias_bulk(request, cliente))
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "sku_sincronizar":
            # Botón «Sincronizar con Shopify» por SKU (Chema 2026-10-21), con modal de confirmación propio.
            from apps.integraciones.services import sincronizar_sku_desde_shopify  # lazy por contrato

            sku = get_object_or_404(SKU, pk=request.POST.get("sku_id"), cliente=cliente)
            try:
                messages.success(request, sincronizar_sku_desde_shopify(sku, request.user))
            except ValueError as exc:
                messages.error(request, str(exc))
            return redirect("mesa:cliente_skus", pk=cliente.pk)
        if accion == "sku_guardar":
            form = FormSKU(cliente, request.POST)
            if form.is_valid():
                try:
                    exito = _guardar_sku(request, cliente, form)
                except ValueError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, exito)
                return redirect("mesa:cliente_skus", pk=cliente.pk)
            # Form inválido: cae al render de abajo con sus errores.
        else:
            messages.error(request, "Acción desconocida. Recarga la página e intenta de nuevo.")
            return redirect("mesa:cliente_skus", pk=cliente.pk)

    # Link al admin de Shopify (por SKU): con la primera tienda activa basta —
    # la búsqueda por query encuentra el producto sin guardar product_id.
    tienda_principal = cliente.tiendas.filter(activo=True).first()
    shopify_conectada = cliente.tiendas.filter(activo=True).exclude(token="").exists()  # habilita «Sincronizar con Shopify»
    sku_editar = None
    if form is None:
        editar_id = request.GET.get("editar", "").strip()
        if editar_id:
            sku_editar = get_object_or_404(SKU, pk=editar_id, cliente=cliente)
            form = FormSKU(cliente, initial={
                "sku_id": sku_editar.pk,
                "categoria": sku_editar.categoria_id,
                "codigo": sku_editar.codigo,
                "descripcion": sku_editar.descripcion,
                "variante": sku_editar.variante,
                "codigo_barras": sku_editar.codigo_barras,
                "peso_gr": sku_editar.peso_gr,
                "largo_cm": sku_editar.largo_cm,
                "ancho_cm": sku_editar.ancho_cm,
                "alto_cm": sku_editar.alto_cm,
                "requiere_lote": sku_editar.requiere_lote,
                "unidad": sku_editar.unidad,
                "precio_declarado": sku_editar.precio_declarado,
                "punto_reorden": sku_editar.punto_reorden,
                "rotacion": sku_editar.rotacion,
                "empaques_divisibles": sku_editar.empaques_divisibles,
                "backorder_habilitado": sku_editar.backorder_habilitado,
                "es_kit": sku_editar.es_kit,
                "productos_por_kit": sku_editar.productos_por_kit,
                "usa_caja_propia": sku_editar.usa_caja_propia,
                "activo": sku_editar.activo,
            })
        else:
            form = FormSKU(cliente)

    from apps.catalogo.models import Categoria
    otros = Categoria.otros_de(cliente)
    skus = list(SKU.objects.filter(cliente=cliente).select_related("categoria", "cliente").order_by("codigo"))
    # SKUs que llegaron del pull de Shopify (2026-10-01): inactivos hasta que Mesa capture peso y medidas.
    por_completar = [s for s in skus if s.shopify_variant_id and not s.activo and not s.peso_gr]
    # Disponible por lote (2026-10-06): dos consultas para todo el catálogo; una por SKU tardaba
    # más que el timeout del service worker y la pantalla salía como "Sin conexión".
    resumenes = resumen_skus(skus)
    for sku in skus:
        sku.disponible = resumenes[sku.pk]["disponible"]
        sku.categoria_efectiva_id = sku.categoria_id or otros.pk

    return render(request, "mesa/cliente_skus.html", {
        "seccion": "clientes",
        "cliente": cliente,
        "skus": skus,
        "por_completar": por_completar,
        "form": form,
        "sku_editar": sku_editar,
        "tienda_principal": tienda_principal,
        "shopify_conectada": shopify_conectada,
        "categorias": list(Categoria.objects.filter(cliente=cliente)),
    })


def _skus_categoria_nueva(request, cliente):
    """Alta de una categoría del catálogo del cliente, con auditoría."""
    from apps.catalogo.models import Categoria

    nombre = (request.POST.get("nombre") or "").strip()
    if not nombre:
        raise ValueError("Captura el nombre de la categoría.")
    if Categoria.objects.filter(cliente=cliente, nombre__iexact=nombre).exists():
        raise ValueError(f"Ya existe la categoría {nombre}.")
    categoria = Categoria.objects.create(cliente=cliente, nombre=nombre)
    registrar_evento(
        "categoria", f"{cliente.slug}:{categoria.nombre}", "alta", actor=request.user,
        cliente=cliente, motivo="Alta de categoría desde Mesa de Control",
    )
    return f"Categoría {categoria.nombre} dada de alta."


def _skus_categorias_bulk(request, cliente):
    """Guarda las categorías elegidas en la tabla del catálogo (edición masiva)."""
    from apps.catalogo.models import SKU, Categoria

    validas = {str(c.pk): c for c in Categoria.objects.filter(cliente=cliente)}
    cambios = []
    for sku in SKU.objects.filter(cliente=cliente).select_related("categoria"):
        elegida = validas.get(request.POST.get(f"cat_{sku.pk}", ""))
        if elegida is None or sku.categoria_id == elegida.pk:
            continue
        antes = sku.categoria.nombre if sku.categoria else Categoria.OTROS
        sku.categoria = elegida
        sku.save(update_fields=["categoria"])
        cambios.append({"sku": sku.codigo, "antes": antes, "ahora": elegida.nombre})
    if cambios:
        registrar_evento(
            "sku", "categorias_bulk", "categorias_actualizadas", actor=request.user,
            cliente=cliente, delta={"cambios": cambios},
            motivo="Edición masiva de categorías desde el catálogo",
        )
        return f"{len(cambios)} producto(s) recategorizado(s)."
    return "Sin cambios de categoría."


def _guardar_sku(request, cliente, form):
    """Alta o edición (por sku_id oculto) de un SKU. Unicidad por (cliente, codigo)."""
    from apps.catalogo.models import SKU

    datos = form.datos_sku()
    sku_id = form.cleaned_data.get("sku_id")
    duplicado = SKU.objects.filter(cliente=cliente, codigo=datos["codigo"])
    if sku_id:
        sku = get_object_or_404(SKU, pk=sku_id, cliente=cliente)
        if duplicado.exclude(pk=sku.pk).exists():
            raise ValueError(
                f"Ya existe el SKU '{datos['codigo']}' para {cliente.nombre}; "
                "los códigos no se repiten por cliente."
            )
        for campo, valor in datos.items():
            setattr(sku, campo, valor)
        sku.save()
        registrar_evento(
            "sku", sku.codigo, "edicion", actor=request.user, cliente=cliente,
            motivo="Edición de SKU desde Mesa de Control",
        )
        return f"SKU {sku.codigo} actualizado."

    if duplicado.exists():
        raise ValueError(
            f"Ya existe el SKU '{datos['codigo']}' para {cliente.nombre}; "
            "los códigos no se repiten por cliente."
        )
    sku = SKU.objects.create(cliente=cliente, **datos)
    registrar_evento(
        "sku", sku.codigo, "alta", actor=request.user, cliente=cliente,
        motivo="Alta de SKU desde Mesa de Control",
    )
    return f"SKU {sku.codigo} dado de alta."


# Columnas enteras opcionales del CSV de catálogo.
_COLUMNAS_CSV_ENTERAS = (
    "peso_gr", "largo_cm", "ancho_cm", "alto_cm", "punto_reorden", "empaques_divisibles",
)

# Contrato del CSV de catálogo (export e import hablan el MISMO formato:
# exportar → editar en Excel → importar sin fricción).
COLUMNAS_CSV_CATALOGO = (
    "codigo", "descripcion", "variante", "codigo_barras", "categoria",
    "peso_gr", "largo_cm", "ancho_cm", "alto_cm",
    "precio_declarado", "punto_reorden", "requiere_lote", "empaques_divisibles", "es_kit",
    "rotacion",
)


def _transformar_shopify(request, cliente):
    """Convierte el products_export.csv de Shopify al CSV del import (preview)."""
    from apps.catalogo.transformador import filas_a_csv, transformar_export_shopify

    archivo = request.FILES.get("archivo")
    if archivo is None:
        raise ValueError("Adjunta el export de productos de Shopify (CSV).")
    max_mb = settings.TORRE["IMPORT_CSV_MAX_MB"]
    if archivo.size > max_mb * 1024 * 1024:
        raise ValueError(f"El CSV no debe pasar de {max_mb} MB.")
    try:
        texto = archivo.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(
            "El archivo no se pudo leer. Descárgalo de Shopify sin modificarlo y súbelo de nuevo."
        )
    resultado = transformar_export_shopify(texto)
    return {
        "seccion": "clientes",
        "cliente": cliente,
        "avisos": resultado["avisos"],
        "filas": resultado["filas"][:50],
        "total": len(resultado["filas"]),
        "csv_transformado": filas_a_csv(resultado["filas"]),
    }


def _descargar_transformado(request, cliente):
    """Descarga del CSV ya transformado (viaja en el hidden del preview: sin estado)."""
    contenido = request.POST.get("csv_transformado") or ""
    if not contenido.strip():
        messages.error(request, "No llegó el CSV transformado; vuelve a subir el export.")
        return redirect("mesa:cliente_skus", pk=cliente.pk)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = (
        f'attachment; filename="catalogo-{cliente.slug}-desde-shopify.csv"'
    )
    respuesta.write("﻿")
    respuesta.write(contenido)
    return respuesta


@rol_requerido("mesa")
def cliente_skus_exportar(request, pk):
    """Catálogo completo del cliente en el formato del import (round-trip)."""
    from apps.catalogo.models import SKU, Categoria

    cliente = get_object_or_404(Cliente, pk=pk)
    respuesta = HttpResponse(content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = f'attachment; filename="catalogo-{cliente.slug}.csv"'
    respuesta.write("﻿")  # BOM: Excel abre los acentos bien (espejo del decode)
    escritor = csv.writer(respuesta)
    escritor.writerow(COLUMNAS_CSV_CATALOGO)
    for sku in SKU.objects.filter(cliente=cliente).select_related("categoria").order_by("codigo"):
        escritor.writerow([
            sku.codigo, sku.descripcion, sku.variante, sku.codigo_barras,
            sku.categoria.nombre if sku.categoria else Categoria.OTROS,
            sku.peso_gr, sku.largo_cm, sku.ancho_cm, sku.alto_cm,
            sku.precio_declarado, sku.punto_reorden,
            "si" if sku.requiere_lote else "no", sku.empaques_divisibles,
            "si" if sku.es_kit else "no", sku.rotacion,
        ])
    return respuesta


def _parsear_fila_csv(fila):
    """Valida una fila del CSV y regresa los campos presentes. ValueError si algo falla."""
    def celda(clave):
        return (fila.get(clave) or "").strip()

    codigo = celda("codigo")
    descripcion = celda("descripcion")
    if not codigo:
        raise ValueError("falta el codigo")
    if not descripcion:
        raise ValueError("falta la descripcion")
    datos = {"codigo": codigo, "descripcion": descripcion}
    if celda("variante"):
        datos["variante"] = celda("variante")
    if celda("codigo_barras"):
        datos["codigo_barras"] = celda("codigo_barras")
    for campo in _COLUMNAS_CSV_ENTERAS:
        crudo = celda(campo)
        if not crudo:
            continue
        try:
            valor = int(crudo)
        except ValueError:
            raise ValueError(f"{campo} no es número")
        if campo == "empaques_divisibles" and valor < 1:
            raise ValueError("empaques_divisibles es mínimo 1")
        if valor < 0:
            raise ValueError(f"{campo} no puede ser negativo")
        datos[campo] = valor
    precio = celda("precio_declarado")
    if precio:
        try:
            valor = Decimal(precio)
        except InvalidOperation:
            raise ValueError("precio_declarado no es número")
        # "NaN"/"Infinity" parsean como Decimal sin excepción, pero no son
        # montos: comparar o guardar truena después. Misma fila con error.
        if not valor.is_finite():
            raise ValueError("precio_declarado no es número")
        if valor < 0:
            raise ValueError("precio_declarado no puede ser negativo")
        datos["precio_declarado"] = valor
    lote = celda("requiere_lote").lower()
    if lote:
        if lote in ("si", "sí", "1"):
            datos["requiere_lote"] = True
        elif lote in ("no", "0"):
            datos["requiere_lote"] = False
        else:
            raise ValueError("requiere_lote acepta si/no/1/0")
    kit = celda("es_kit").lower()
    if kit:
        if kit in ("si", "sí", "1"):
            datos["es_kit"] = True
        elif kit in ("no", "0"):
            datos["es_kit"] = False
        else:
            raise ValueError("es_kit acepta si/no/1/0")
    for campo in ("fragil", "este_lado_arriba"):  # manejo (2026-09-29): si/no/1/0
        crudo = celda(campo).lower()
        if crudo:
            if crudo in ("si", "sí", "1"):
                datos[campo] = True
            elif crudo in ("no", "0"):
                datos[campo] = False
            else:
                raise ValueError(f"{campo} acepta si/no/1/0")
    rotacion = celda("rotacion").strip().upper()
    if rotacion:
        if rotacion in ("A", "B", "C"):
            datos["rotacion"] = rotacion
        elif rotacion in ("AUTO", "AUTOMATICA", "AUTOMÁTICA"):
            datos["rotacion"] = "auto"
        else:
            raise ValueError("rotacion acepta A/B/C o auto")
    if celda("categoria"):
        datos["categoria_nombre"] = celda("categoria")
    return datos


def _categoria_por_nombre(cliente, nombre):
    """Categoría del cliente por nombre (insensible a mayúsculas); se crea si no existe."""
    from apps.catalogo.models import Categoria

    existente = Categoria.objects.filter(cliente=cliente, nombre__iexact=nombre).first()
    return existente or Categoria.objects.create(cliente=cliente, nombre=nombre)


def _importar_csv_skus(request, cliente):
    """Import masivo de catálogo: update_or_create por (cliente, codigo).

    Las filas con error no detienen a las demás. Un solo evento de auditoría
    resume el import completo. Todo corre dentro de una transacción: un error
    inesperado revierte el import entero — nunca mutación de catálogo sin evento.
    """
    from apps.catalogo.models import SKU, Categoria

    archivo = request.FILES.get("archivo")
    if archivo is None:
        raise ValueError("Adjunta el archivo CSV con el catálogo.")
    max_mb = settings.TORRE["IMPORT_CSV_MAX_MB"]
    if archivo.size > max_mb * 1024 * 1024:
        raise ValueError(f"El CSV no debe pasar de {max_mb} MB.")
    with transaction.atomic():
        try:
            texto = archivo.read().decode("utf-8-sig")  # BOM de Excel incluido
        except UnicodeDecodeError:
            raise ValueError("El archivo no se pudo leer. Guárdalo como CSV UTF-8 y súbelo de nuevo.")
        lector = csv.DictReader(io.StringIO(texto))
        # Encabezados con espacios ("codigo, descripcion"): normalizarlos en el
        # lector muta las claves con las que DictReader arma TODAS las filas.
        lector.fieldnames = [e.strip() for e in (lector.fieldnames or [])]
        if "codigo" not in lector.fieldnames or "descripcion" not in lector.fieldnames:
            raise ValueError(
                "El CSV debe traer al menos las columnas codigo y descripcion "
                "(revisa el renglón de encabezados)."
            )

        max_filas = settings.TORRE["IMPORT_CSV_MAX_FILAS"]
        creados = 0
        actualizados = 0
        errores = []
        # La fila 1 es el encabezado: los datos empiezan en la fila 2 (como en Excel).
        for numero, fila in enumerate(lector, start=2):
            if numero - 1 > max_filas:
                # El ValueError revienta el atomic: no se importa nada a medias.
                raise ValueError(
                    f"El CSV trae más de {max_filas} filas de datos; "
                    "divídelo en archivos más chicos y súbelos por partes."
                )
            try:
                datos = _parsear_fila_csv(fila)
            except ValueError as exc:
                errores.append(f"fila {numero}: {exc}")
                continue
            codigo = datos.pop("codigo")
            nombre_categoria = datos.pop("categoria_nombre", "")
            if nombre_categoria:
                datos["categoria"] = _categoria_por_nombre(cliente, nombre_categoria)
            sku, creado = SKU.objects.update_or_create(
                cliente=cliente, codigo=codigo, defaults=datos
            )
            if creado and sku.categoria_id is None:
                sku.categoria = Categoria.otros_de(cliente)
                sku.save(update_fields=["categoria"])
            if creado:
                creados += 1
            else:
                actualizados += 1

        registrar_evento(
            "sku", "import_csv", "import_csv", actor=request.user, cliente=cliente,
            delta={"creados": creados, "actualizados": actualizados, "errores": errores},
            motivo=f"Import de catálogo por CSV ({archivo.name})",
        )
    resumen = f"{creados} SKUs creados, {actualizados} actualizados"
    if errores:
        plural = "s" if len(errores) != 1 else ""
        resumen += f", {len(errores)} fila{plural} con error ({'; '.join(errores)})"
    return resumen + "."

# ─────────────────────────────────────────────────────────────────────────────
# Finanzas: factura por tarifario vs costos reales (visibilidad de margen)
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa")
def finanzas_corte(request):
    """Estado de resultados del CORTE (quincena, Chema 2026-10-01): cuánto se
    factura a cada cliente en sus dos estados de cuenta (fulfillment y
    guías, con IVA) y cuánto nos costó. Se llega desde `finanzas` (cliente y
    corte elegidos) y las flechas cambian de corte conservando el cliente.

    El ingreso sale del tarifario vigente (settings + override por cliente);
    el costo de envío es el REAL de las guías del corte. Los fijos (renta,
    sueldos, servicios) se restan una sola vez, a nivel global, la mitad del
    mes por corte.
    """
    from apps.finanzas import services as motor
    from apps.finanzas.cortes import corte_actual, corte_desde_clave

    corte = corte_desde_clave(request.GET.get("corte")) or corte_actual()
    inicio, fin = corte.limites()

    # Un cliente (`?cliente=slug`, Chema 2026-10-01) o todos: activos siempre;
    # inactivos solo si tuvieron guías, recepciones o reembolsos ese corte
    # (el histórico no se reescribe cuando un cliente churnea; un corte de
    # SOLO recepción o solo reembolso también cuenta).
    from apps.inventario.models import OrdenEntrada  # lazy por contrato

    todos = list(Cliente.objects.order_by("nombre"))
    cliente_sel = next((c for c in todos if c.slug == (request.GET.get("cliente") or "")), None)
    if cliente_sel is not None:
        clientes = [cliente_sel]
    else:
        clientes = (
            Cliente.objects.filter(
                Q(activo=True)
                | Q(pedidos__guias__creado__gte=inicio, pedidos__guias__creado__lt=fin)
                | Q(reembolsos_guia__fecha__gte=inicio, reembolsos_guia__fecha__lt=fin)
                | Q(
                    ordenes_entrada__ts_descarga_fin__gte=inicio,
                    ordenes_entrada__ts_descarga_fin__lt=fin,
                    ordenes_entrada__estado__in=(OrdenEntrada.RECIBIDA, OrdenEntrada.CERRADA),
                )
            )
            .distinct()
            .order_by("nombre")
        )
    filas = [motor.resumen_corte(cliente, corte) for cliente in clientes]

    torre = settings.TORRE
    ingreso_total = sum((f["ingresos"]["total"] for f in filas), Decimal("0"))
    ingreso_con_iva = sum((f["ingresos"]["total_con_iva"] for f in filas), Decimal("0"))
    costo_envio = sum((f["costos"]["carrier"] for f in filas), Decimal("0"))
    costo_insumos = sum((f["costos"]["insumos"] for f in filas), Decimal("0"))
    margen_bruto = sum((f["margen_bruto"] for f in filas), Decimal("0"))

    # Desglose por estado destino (todas las marcas), con datos reales de guías.
    acumulado = {}
    for f in filas:
        for nombre, e in f["estados"].items():
            m = acumulado.setdefault(nombre, {
                "ordenes": 0, "peso": 0.0, "guias": 0, "zona": e["zona"],
                "costo": Decimal("0"), "facturado": Decimal("0"),
            })
            m["ordenes"] += e["ordenes"]
            m["peso"] += e["peso"]
            m["guias"] += e["guias"]
            m["costo"] += e["costo"]
            m["facturado"] += e["facturado"]
    total_ordenes = sum(e["ordenes"] for e in acumulado.values())
    tabla_estados = [
        {
            "nombre": nombre,
            "zona": e["zona"],
            "ordenes": e["ordenes"],
            "pct": round(e["ordenes"] * 100 / total_ordenes, 1) if total_ordenes else 0,
            "peso_prom": round(e["peso"] / e["ordenes"], 1) if e["ordenes"] else 0,
            "guias": e["guias"],
            "costo": e["costo"],
            "facturado": e["facturado"],
            "margen_envio": e["facturado"] - e["costo"],
        }
        for nombre, e in sorted(acumulado.items(), key=lambda kv: (-kv[1]["ordenes"], kv[0]))
    ]
    peso_total = sum(e["peso"] for e in acumulado.values())
    totales_estados = {
        "ordenes": total_ordenes,
        "peso_prom": round(peso_total / total_ordenes, 1) if total_ordenes else 0,
        "guias": sum(e["guias"] for e in acumulado.values()),
        "costo": sum((e["costo"] for e in acumulado.values()), Decimal("0")),
        "facturado": sum((e["facturado"] for e in acumulado.values()), Decimal("0")),
    }
    totales_estados["margen_envio"] = totales_estados["facturado"] - totales_estados["costo"]
    fijos = Decimal(str(torre["COSTOS_FIJOS_MES_MXN"])) / 2  # la mitad del mes por corte
    profit = margen_bruto - fijos
    meta = Decimal(str(torre["META_PROFIT_MES_MXN"])) / 2
    if profit >= meta:
        profit_pill = "ok"
    elif profit >= meta / 2:
        profit_pill = "warn"
    else:
        profit_pill = "crit"

    return render(request, "mesa/finanzas.html", {
        "seccion": "finanzas",
        "corte": corte,
        "corte_prev": corte.anterior(),
        "corte_sig": corte.siguiente(),
        "cliente_sel": cliente_sel,
        "sufijo_cliente": f"&cliente={cliente_sel.slug}" if cliente_sel is not None else "",
        "iva_pct": int(motor.iva() * 100),
        "filas": filas,
        "ingreso_con_iva": ingreso_con_iva,
        "tabla_estados": tabla_estados,
        "totales_estados": totales_estados,
        "ingreso_total": ingreso_total,
        "costo_envio": costo_envio,
        "costo_insumos": costo_insumos,
        "margen_bruto": margen_bruto,
        "fijos": fijos,
        "profit": profit,
        "profit_pill": profit_pill,
        "meta_profit": meta,
    })


@rol_requerido("mesa")
def finanzas(request):
    """Inicio de Finanzas (Chema 2026-10-01): elegir cliente (default todos)
    y corte (default el actual) y pasar a los estados de cuenta
    (`finanzas_corte`). Los últimos 24 cortes y el siguiente, para revisar un
    corte pasado o adelantar el que viene."""
    from apps.finanzas.cortes import corte_actual, corte_desde_clave

    actual = corte_actual()
    elegido = corte_desde_clave(request.GET.get("corte")) or actual
    cortes, c = [], actual.siguiente()
    for _ in range(26):
        cortes.append(c)
        c = c.anterior()
    if elegido not in cortes:
        cortes.append(elegido)
        cortes.sort(reverse=True)
    return render(request, "mesa/finanzas_inicio.html", {
        "seccion": "finanzas",
        "clientes": list(Cliente.objects.order_by("nombre")),
        "cliente_slug": request.GET.get("cliente") or "",
        "cortes": cortes,
        "elegido": elegido,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Manuales (SOPs) — pre-renderizados por `manage.py render_manuales`
# ─────────────────────────────────────────────────────────────────────────────


@rol_requerido("mesa", "piso")
def manuales(request):
    """Índice de manuales. El piso también los consulta desde su tablet.

    La Mesa ve además las últimas sugerencias de clientes (llegan del portal
    como EventoAuditoria: entidad "manual", acción "sugerencia_cliente").
    """
    from apps.core.manuales import agrupar, manuales_publicados, portada

    publicados = manuales_publicados()
    sugerencias = []
    if request.rol == "mesa" or request.user.is_superuser:
        slug_por_codigo = {m["codigo"]: m["slug"] for m in publicados}
        sugerencias = list(
            EventoAuditoria.objects.filter(
                entidad="manual", accion="sugerencia_cliente"
            ).select_related("cliente")[:10]
        )
        for evento in sugerencias:
            evento.manual_slug = slug_por_codigo.get(evento.entidad_id)

    return render(request, "manuales/lista.html", {
        "seccion": "manuales",
        "grupos": agrupar(publicados),
        "portada": portada(),
        "sugerencias": sugerencias,
        "url_detalle": "mesa:manual_detalle",
    })


@rol_requerido("mesa", "piso")
def manual_detalle(request, slug):
    from apps.core.manuales import obtener_manual

    manual = obtener_manual(slug)
    if manual is None:
        raise Http404("Ese manual no existe.")
    return render(request, "manuales/detalle.html", {
        "seccion": "manuales",
        "manual": manual,
        "cuerpo_template": f"manuales/{slug}.html",
        "url_lista": "mesa:manuales",
    })


@rol_requerido("mesa")
def tienda_conectar(request, pk, tienda_pk):
    """Botones de la columna Conexión de la ficha del cliente (Chema 2026-10-21):
    `conectar` abre la pantalla de permisos de Shopify de la app de la tienda
    (al aprobar, /oauth/callback guarda el token y corre los pasos
    post-conexión); `link` genera el link de 7 días para que el cliente
    apruebe desde su admin; `sincronizar` corre catálogo y pedidos en segundo
    plano. Todo por POST; nada habla con Shopify dentro de la petición salvo
    la redirección a su pantalla."""
    from apps.integraciones import oauth  # lazy por contrato
    from apps.integraciones.models import Tienda  # lazy: modelo de otra app

    cliente = get_object_or_404(Cliente, pk=pk)
    tienda = get_object_or_404(Tienda, pk=tienda_pk, cliente=cliente)
    volver = redirect(f"{reverse('mesa:cliente_detalle', args=[cliente.pk])}?tienda={tienda.pk}")
    if request.method != "POST":
        return volver
    accion = request.POST.get("accion", "conectar")
    try:
        if accion == "link":
            messages.success(
                request,
                f"Link de conexión para el cliente (vale 7 días; lo abre con su sesión de admin de "
                f"{tienda.dominio}): {oauth.link_de_conexion(tienda)}",
            )
        elif accion == "sincronizar":
            oauth.sincronizar_en_segundo_plano(tienda, actor=request.user)
            messages.success(
                request,
                f"{tienda.dominio}: catálogo y pedidos sincronizando en segundo plano; revisa Salud de sync en unos minutos.",
            )
        else:
            return redirect(oauth.iniciar_conexion(tienda, actor=request.user))
    except ValueError as exc:
        messages.error(request, str(exc))
    return volver
