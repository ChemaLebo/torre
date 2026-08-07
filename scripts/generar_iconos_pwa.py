"""Genera los iconos de la PWA de Torre (one-off, requiere Pillow).

Dibuja una torre de ajedrez (rook) estilizada en #C41E3A sobre fondo #050505
con esquinas redondeadas — polígonos simples, sólido, nada sci-fi. Se dibuja
a 1024 px y se baja con LANCZOS a 512 y 192 para bordes limpios.

Uso:
    cd torre && ../torre-env/bin/python scripts/generar_iconos_pwa.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent.parent
DESTINO = BASE / "static" / "pwa"

FONDO = "#050505"
ROJO = "#C41E3A"
LADO = 1024
RADIO_ESQUINAS = 200


def dibujar_icono():
    img = Image.new("RGBA", (LADO, LADO), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, LADO - 1, LADO - 1], radius=RADIO_ESQUINAS, fill=FONDO)

    # Rook centrado, dentro de la zona segura maskable (~80% central).
    # Corona almenada: tres merlones + barra.
    for x0, x1 in [(312, 402), (467, 557), (622, 712)]:
        d.rectangle([x0, 240, x1, 360], fill=ROJO)
    d.rectangle([312, 360, 712, 425], fill=ROJO)
    # Cuello: trapecio que se angosta hacia abajo.
    d.polygon([(357, 425), (667, 425), (627, 660), (397, 660)], fill=ROJO)
    # Base en dos escalones.
    d.rectangle([362, 660, 662, 715], fill=ROJO)
    d.rectangle([302, 715, 722, 800], fill=ROJO)
    return img


def main():
    DESTINO.mkdir(parents=True, exist_ok=True)
    icono = dibujar_icono()
    for lado in (512, 192):
        salida = DESTINO / f"icono-{lado}.png"
        icono.resize((lado, lado), Image.LANCZOS).save(salida)
        print(f"OK {salida.relative_to(BASE)}")


if __name__ == "__main__":
    main()
