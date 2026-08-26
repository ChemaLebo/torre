"""Transformador del export de productos de Shopify al CSV de catálogo de Torre.

Puro (stdlib csv/io): recibe el texto del products_export.csv de Shopify y
regresa filas en el contrato del import + avisos. No toca la base: las
categorías nacen en el IMPORT — aquí el Type de Shopify es solo una
sugerencia editable en Excel antes de importar.
"""
import csv
import io

COLUMNAS_TORRE = (
    "codigo", "descripcion", "variante", "codigo_barras", "categoria",
    "peso_gr", "largo_cm", "ancho_cm", "alto_cm",
    "precio_declarado", "punto_reorden", "requiere_lote", "empaques_divisibles",
)

_SIN_VARIANTE = "Default Title"


def _peso_de(gramos):
    """Variant Grams → gramos enteros; 0 en Shopify suele ser 'no capturado'."""
    crudo = (gramos or "").strip()
    if not crudo:
        return ""
    try:
        peso = int(float(crudo))
    except ValueError:
        return ""
    return str(peso) if peso > 0 else ""


def transformar_export_shopify(texto_csv):
    """{"filas": [...], "avisos": [...]} desde el products_export.csv.

    Shopify deja en blanco Title/Type en las filas 2+ de cada producto
    (forward-fill por Handle); las filas de solo-imagen se ignoran sin aviso;
    variantes sin SKU, SKUs repetidos y códigos de barras compartidos dejan
    aviso. La variante junta los Option Values distintos de 'Default Title'.
    """
    lector = csv.DictReader(io.StringIO(texto_csv))
    lector.fieldnames = [e.strip() for e in (lector.fieldnames or [])]
    if "Handle" not in lector.fieldnames or "Variant SKU" not in lector.fieldnames:
        raise ValueError(
            "Esto no parece un export de productos de Shopify "
            "(faltan las columnas Handle y Variant SKU)."
        )

    filas, avisos = [], []
    titulos, tipos = {}, {}  # forward-fill por Handle
    vistos = set()
    barcodes = {}
    for numero, cruda in enumerate(lector, start=2):
        handle = (cruda.get("Handle") or "").strip()
        if (cruda.get("Title") or "").strip():
            titulos[handle] = cruda["Title"].strip()
            tipos[handle] = (cruda.get("Type") or "").strip()
        titulo = titulos.get(handle, "")

        opciones = [(cruda.get(f"Option{i} Value") or "").strip() for i in (1, 2, 3)]
        variante = " / ".join(o for o in opciones if o and o != _SIN_VARIANTE)
        precio = (cruda.get("Variant Price") or "").strip()
        codigo = (cruda.get("Variant SKU") or "").strip()

        if not codigo:
            # Fila de imagen extra (sin precio ni opciones): silencio. Variante
            # real sin SKU: aviso — el import la rechazaría de todas formas.
            if precio or any(opciones):
                avisos.append(f"fila {numero}: variante de '{titulo or handle}' sin SKU — se omite")
            continue
        if codigo in vistos:
            avisos.append(f"fila {numero}: SKU repetido '{codigo}' — se conserva el primero")
            continue
        vistos.add(codigo)

        barcode = (cruda.get("Variant Barcode") or "").strip()
        if barcode:
            barcodes.setdefault(barcode, []).append(codigo)

        filas.append({
            "codigo": codigo,
            "descripcion": titulo or codigo,
            "variante": variante,
            "codigo_barras": barcode,
            "categoria": tipos.get(handle, ""),
            "peso_gr": _peso_de(cruda.get("Variant Grams")),
            "largo_cm": "", "ancho_cm": "", "alto_cm": "",
            "precio_declarado": precio,
            "punto_reorden": "", "requiere_lote": "", "empaques_divisibles": "",
        })

    for barcode, codigos in barcodes.items():
        if len(codigos) > 1:
            avisos.append(f"código de barras {barcode} compartido por {', '.join(codigos)}")
    if not filas:
        avisos.append("el export no trajo ninguna variante con SKU")
    return {"filas": filas, "avisos": avisos}


def filas_a_csv(filas):
    """Las filas transformadas como CSV en el contrato del import de Torre."""
    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=list(COLUMNAS_TORRE))
    escritor.writeheader()
    for fila in filas:
        escritor.writerow(fila)
    return buffer.getvalue()
