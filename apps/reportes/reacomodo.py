"""Reacomodo sugerido: qué producto está en un anaquel que no le corresponde
por su rotación, y la ocupación estimada de cada anaquel.

Tabla principal: SKUs con existencia en picking cuya clase no cuadra con la
prioridad del anaquel donde están: clase A con un anaquel vacío de mejor
prioridad disponible (mover ahí), y clase C ocupando un anaquel del tercio
mejor (liberarlo). Grupo: ocupación por anaquel (todos los clientes: es el
anaquel físico) con sus SKUs y avisos de medidas.
"""
from collections import defaultdict

from apps.catalogo.models import SKU, Ubicacion
from apps.inventario.models import Saldo

CLAVE = "reacomodo"
TITULO = "Reacomodo sugerido"
DESCRIPCION = (
    "Productos de alta rotación en anaqueles lentos (y de baja rotación en los mejores anaqueles), "
    "con el anaquel al que convendría moverlos; abajo, la ocupación estimada de cada anaquel."
)
CON_FECHAS = False
FILTROS = []
COLUMNAS = [
    ("SKU", "texto"), ("Producto", "texto"), ("Clase", "texto"), ("Está en", "texto"),
    ("Prioridad actual", "entero"), ("Sugerencia", "texto"), ("Prioridad sugerida", "entero"), ("Piezas", "entero"),
]
COLUMNAS_OCUPACION = [
    ("Anaquel", "texto"), ("Prioridad", "entero"), ("Ocupación %", "entero"), ("Estado", "texto"),
    ("SKUs", "texto"), ("Avisos", "texto"),
]


def generar(cliente, inicio, fin, filtros, es_mesa):
    from apps.catalogo.services import clases_rotacion  # lazy por contrato
    from apps.inventario.services import ocupaciones  # lazy por contrato

    anaqueles = list(
        Ubicacion.objects.filter(tipo__in=[Ubicacion.PICKING, Ubicacion.RESERVA], activo=True).order_by("codigo")
    )
    ocup = ocupaciones(anaqueles)
    por_codigo = {u.codigo: u for u in anaqueles}
    clases = clases_rotacion(cliente)
    skus = {s.pk: s for s in SKU.objects.filter(cliente=cliente, activo=True)}

    donde = defaultdict(lambda: defaultdict(int))  # sku_id → {codigo: piezas}
    for s in Saldo.objects.filter(sku__cliente=cliente, cantidad__gt=0, ubicacion__in=anaqueles).select_related("ubicacion"):
        if s.ubicacion.tipo == Ubicacion.PICKING:
            donde[s.sku_id][s.ubicacion.codigo] += s.cantidad

    con_prioridad = [u for u in anaqueles if u.tipo == Ubicacion.PICKING and u.prioridad and not u.lleno_manual]
    vacios = sorted((u for u in con_prioridad if not ocup[u.codigo]["por_sku"]), key=lambda u: u.prioridad)
    tercio = sorted(u.prioridad for u in con_prioridad)
    umbral_mejor = tercio[len(tercio) // 3] if tercio else None

    filas = []
    for sku_id, lugares in donde.items():
        sku = skus.get(sku_id)
        if sku is None:
            continue
        clase = clases.get(sku_id, "C")
        actuales = [por_codigo[c] for c in lugares if por_codigo[c].prioridad]
        if not actuales:
            continue
        mejor_actual = min(actuales, key=lambda u: u.prioridad)
        piezas = sum(lugares.values())
        if clase == "A":
            candidato = next((u for u in vacios if u.prioridad < mejor_actual.prioridad), None)
            if candidato is not None:
                filas.append([
                    sku.codigo, sku.descripcion, clase, ", ".join(sorted(lugares)), mejor_actual.prioridad,
                    f"mover a {candidato.codigo} (anaquel libre mejor)", candidato.prioridad, piezas,
                ])
        elif clase == "C" and umbral_mejor is not None and mejor_actual.prioridad <= umbral_mejor:
            peor = next((u for u in reversed(vacios) if u.prioridad > mejor_actual.prioridad), None)
            filas.append([
                sku.codigo, sku.descripcion, clase, ", ".join(sorted(lugares)), mejor_actual.prioridad,
                f"liberar {mejor_actual.codigo}" + (f": mover a {peor.codigo}" if peor else ""),
                peor.prioridad if peor else None, piezas,
            ])
    filas.sort(key=lambda f: (f[2], f[4]))

    ocupacion_filas = []
    for u in anaqueles:
        o = ocup[u.codigo]
        avisos = []
        if u.lleno_manual:
            avisos.append("marcado lleno por el piso")
        if o["sin_medidas"]:
            avisos.append("sin medidas: " + ", ".join(o["sin_medidas"]))
        if o["no_caben"]:
            avisos.append("no cabe parado: " + ", ".join(o["no_caben"]))
        ocupacion_filas.append([
            u.codigo, u.prioridad, o["pct"], o["estado"].replace("_", " "),
            ", ".join(sorted(f["sku"].codigo for f in o["por_sku"])), "; ".join(avisos),
        ])
    return {
        "filas": filas,
        "resumen": [
            ("SKUs por reacomodar", len(filas)),
            ("anaqueles llenos", sum(1 for u in anaqueles if ocup[u.codigo]["estado"] == "lleno")),
            ("anaqueles libres", len(vacios)),
        ],
        "grupos": [{"titulo": "Ocupación por anaquel", "columnas": COLUMNAS_OCUPACION, "filas": ocupacion_filas}],
    }
