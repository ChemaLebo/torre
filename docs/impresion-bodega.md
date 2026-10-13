# Impresión en bodega: térmicas, colas CUPS y agente

Documento de referencia de la impresión física de Torre. Cubre la máquina de la bodega, las dos térmicas, sus colas CUPS con cada opción que importa, qué manda Torre a cada una, cómo probar y cómo diagnosticar. Escrito el 2026-10-13 después de reconstruir la cola de la térmica de guías desde cero; todo lo que dice aquí está verificado en esa máquina ese día.

Regla de oro: **antes de tocar una cola CUPS, respalda su PPD** (`sudo cp /etc/cups/ppd/<cola>.ppd ~/respaldo-ppd/`). Al borrar una cola con `lpadmin -x` se va su PPD y no hay forma de saber con qué driver y opciones estaba.

## 1. Cómo fluye una impresión

1. Torre (VPS, `TORRE_MODO_IMPRESION=relay`) no imprime: deja un `envios.TrabajoImpresion` con el PDF, la impresora destino (`impresora`: `envios` o `etiquetas`), el papel (`media`), el tipo (`guia`, `producto`, `tarima`), la referencia y el número de páginas. Lo encolan `apps/piso/etiquetas.py` (`_encolar_etiqueta` para guías, `encolar_impresion` para etiquetas de recepción).
2. El agente de bodega (`deploy/agente_impresion.py`, Python 3 puro, sin pip) hace polling cada 3 s a `GET /api/impresion/pendientes/` con el token `TORRE_TOKEN_IMPRESION`, baja el PDF de `url_pdf` y ejecuta:
   ```
   lp -d <cola CUPS> -o media=<media del trabajo> archivo.pdf
   ```
   La cola sale de `IMPRESORA` (trabajos `envios`) o `IMPRESORA_ETIQUETAS` (trabajos `etiquetas`). Después reporta a `POST /api/impresion/<id>/resultado/`; Torre marca IMPRESO o suma un intento (máximo `TORRE["IMPRESION_MAX_INTENTOS"]` = 5, luego ERROR).
3. CUPS en la laptop rasteriza el PDF con el driver de la cola y lo manda por USB a la térmica.

Un trabajo de etiquetas sin `IMPRESORA_ETIQUETAS` configurada se queda PENDIENTE en Torre (el agente lo avisa en su log, sin consumir intentos) y sale en cuanto se configure. Un agente anterior al 2026-10-13 ignora `impresora` y `media`: imprime TODO en la cola de guías con papel 10×15, lo que saca las etiquetas de 51 mm gigantes en la térmica grande. Nunca corras dos agentes a la vez (uno de systemd y otro a mano): los dos toman los mismos trabajos.

## 2. La máquina de bodega

| | |
|---|---|
| Equipo | Laptop Lenovo IdeaPad 3 15ADA05, Ubuntu (locale es), hostname `almacen-IdeaPad-3-15ADA05` |
| Usuario | `almacen` (con sudo) |
| Agente | `/opt/torre/agente_impresion.py`, copia de `deploy/agente_impresion.py` |
| Servicio | `torre-impresion.service` (systemd, enabled, `Restart=always`), archivo `/etc/systemd/system/torre-impresion.service` |
| Log del agente | `journalctl -u torre-impresion -n 30 --no-pager` (o `-f` para verlo en vivo) |
| Log de CUPS | `/var/log/cups/error_log` (con sudo) |
| Torre | `TORRE_URL=https://fulfillment.wop.partners` |

Variables del servicio (sección `[Service]`):

```
Environment=TORRE_URL=https://fulfillment.wop.partners
Environment=TORRE_TOKEN_IMPRESION=<el mismo valor que TORRE_TOKEN_IMPRESION en el .env del VPS>
Environment=IMPRESORA=AiYin_BY-480BT
Environment=IMPRESORA_ETIQUETAS=Etiquetas51
```

Al arrancar, el agente escribe una línea que confirma la configuración; si no dice las dos colas, algo falta:

```
Agente arriba: https://fulfillment.wop.partners → guías en AiYin_BY-480BT, etiquetas en Etiquetas51 (poll cada 3 s)
```

Actualizar el agente tras un cambio en el repo:

```bash
sudo cp ~/Descargas/agente_impresion.py /opt/torre/agente_impresion.py
sudo systemctl restart torre-impresion
journalctl -u torre-impresion -n 3 --no-pager
```

Cambiar una variable: editar el archivo del servicio, luego `sudo systemctl daemon-reload && sudo systemctl restart torre-impresion`.

## 3. Térmica de guías: AiYin BY-480BT

| | |
|---|---|
| Qué imprime | Guías de paquetería (PDF del carrier o etiqueta interna de Torre), 100×150 mm, una página por trabajo |
| Hardware | AiYin BY-480BT (familia Beeprt/Rollo/Munbyn de térmicas de 4"), 203 dpi, USB. `lsusb`: `09c6:0426 Beeprt Printer` |
| URI | `usb://AiYin/BY-480BT?serial=0.0` (lo da `sudo lpinfo -v`) |
| Cola CUPS | `AiYin_BY-480BT` |
| Driver | `tspl-cups-driver` 1.3.6 (RunTheWall, libre), PPD `lsb/usr/tspl/tspl-label.ppd`, nombre "TSPL Label Printer - HZD950 & rebadges" |
| Papel por default | `na_index-4x6_4x6in` (4×6", `w288h432`); cada trabajo manda `media=Custom.100x150mm` |
| Resolución | **203dpi** (ver abajo: el default del PPD es 300 y rompe la impresión) |
| Tracking | Gap, longitud 30 (3 mm) |
| Ajuste al papel | `print-scaling=fit` y `fit-to-page=true` como defaults de la cola: los PDF de los carriers no siempre miden 100×150 |

Recrear la cola desde cero (exactamente lo que se hizo el 2026-10-13):

```bash
sudo lpadmin -p AiYin_BY-480BT -E -v "usb://AiYin/BY-480BT?serial=0.0" -m lsb/usr/tspl/tspl-label.ppd -o media=na_index-4x6_4x6in
sudo lpadmin -p AiYin_BY-480BT -o Resolution=203dpi
sudo lpadmin -p AiYin_BY-480BT -o print-scaling-default=fit -o fit-to-page-default=true
sudo cupsaccept AiYin_BY-480BT
sudo cupsenable AiYin_BY-480BT
```

Verificación: `lpoptions -p AiYin_BY-480BT -l` debe empezar con `Resolution/Resolution: *203dpi 300dpi` (el asterisco marca el valor activo). Opciones del PPD tal como las lista esa cola:

```
Resolution/Resolution: *203dpi 300dpi
PageSize/Media Size: *w288h432 w283h425 w288h288 w162h108 w144h72 Custom.WIDTHxHEIGHT
PrintSpeed/Print Speed: 0 10 20 30 *40 50 60
MediaTracking/Media tracking: *Gap BlackMark Continuous FixedPitch PrinterDefault
GapLength/Gap or black-mark length: 15 20 25 *30 40 50
Darkness/Darkness: 0 … *8 … 15
PrintMode/Print Mode: 5 0 2 3 *4
Horizontal/Horizontal offset (dots): *0 8 16 24
Vertical/Vertical offset (dots): *0 8 16 24
```

Si la impresión sale desplazada se corrige con `Horizontal`/`Vertical` (en puntos de 203 dpi, 8 puntos = 1 mm); si sale tenue, con `Darkness`. Siempre con `sudo lpadmin -p AiYin_BY-480BT -o Opcion=valor`.

Prueba directa, sin Torre, con cualquier PDF de 100×150:

```bash
lp -d AiYin_BY-480BT -o media=Custom.100x150mm archivo.pdf
```

## 4. Térmica de etiquetas: Munbyn ITPP130B (51×51 mm)

| | |
|---|---|
| Qué imprime | Etiquetas de recepción: cajas (marca, SKU, producto, lote, caducidad, QR `P|barras|lote`) y tarimas (nombre, QR `U|TAR-nn`); trabajos de varias páginas (tandas, `configuracion.TandaEtiquetas`, default 50) |
| Hardware | Munbyn ITPP130B, térmica de 4" con rollo de 51×51 mm, 203 dpi, lenguaje TSPL, USB. `lsusb`: `5958:0130 YXWL ITPP130B` |
| URI | `tspl://5958-0130` (backend del driver TSPL; `usb://Printer/ITPP130?serial=0.0` también la identifica) |
| Cola CUPS | `Etiquetas51` |
| Driver | el mismo `tspl-cups-driver` 1.3.6, PPD `lsb/usr/tspl/tspl-label.ppd` |
| Resolución | 203dpi |
| Tracking | Gap, 2 mm (`GapLength=20`) |
| Papel | cada trabajo manda `media=Custom.51x51mm` |

Instalación del driver: el repo apt de RunTheWall falló en esa Ubuntu (llave vacía; no trae `curl`), así que se instaló el `.deb` directo de `tspl-cups-driver` 1.3.6. Recrear la cola:

```bash
sudo lpadmin -p Etiquetas51 -E -v tspl://5958-0130 -m lsb/usr/tspl/tspl-label.ppd -o Resolution=203dpi -o MediaTracking=Gap -o GapLength=20
sudo cupsaccept Etiquetas51
sudo cupsenable Etiquetas51
```

Prueba directa (el PDF de prueba vive en `~/Descargas/etiqueta-prueba-51mm.pdf` en esa laptop):

```bash
lp -d Etiquetas51 -o media=Custom.51x51mm ~/Descargas/etiqueta-prueba-51mm.pdf
```

Al cambiar de rollo conviene dejar que la impresora calibre el gap (feed sostenido) antes de la primera tanda.

## 5. Colas que NO se usan

`lpstat -p` en esa laptop también lista colas que no sirven para Torre y que confunden:

- `BY-480BT`: creada el 2026-08-20 por el autodescubrimiento de Ubuntu con un PPD de **HP Designjet T920 PostScript**. Acepta trabajos, los "completa" y no imprime nada (la térmica ignora PostScript). Es la razón por la que "el log dice impresa y no sale papel" si el agente apunta ahí.
- `Etiquetas_Bodega_BY_480BT_MacBook_Air_de_Diego`: cola compartida por red desde la Mac de Diego (`implicitclass://`). Ruido.

Se pueden borrar (`sudo lpadmin -x BY-480BT`), pero solo después de respaldar su PPD y de confirmar que el servicio del agente no las nombra. Ubuntu puede volver a crear `BY-480BT` sola al reconectar la impresora; no pasa nada mientras `IMPRESORA` siga diciendo `AiYin_BY-480BT`.

## 6. Qué manda Torre, por si cambia el código

Constantes en el repo (no cambiarlas sin cambiar las colas):

| Dónde | Valor | Para qué |
|---|---|---|
| `envios.TrabajoImpresion.IMPRESORA_ENVIOS` / `IMPRESORA_ETIQUETAS` | `envios` / `etiquetas` | campo `impresora` del trabajo; el agente lo traduce a `IMPRESORA` / `IMPRESORA_ETIQUETAS` |
| `TrabajoImpresion.MEDIA_GUIAS` | `Custom.100x150mm` | papel de las guías |
| `TrabajoImpresion.MEDIA_ETIQUETAS` y `apps/piso/etiquetas.py: MEDIA_ETIQUETAS` | `Custom.51x51mm` | papel de las etiquetas |
| `apps/piso/etiquetas.py: ANCHO, ALTO` | 100 mm × 150 mm | tamaño del PDF de la etiqueta interna de guía |
| `apps/piso/etiquetas.py: ETIQUETA_LADO` | 51 mm | tamaño del PDF de etiquetas de recepción |
| `deploy/agente_impresion.py: MEDIA_GUIAS` | `Custom.100x150mm` | papel si un trabajo viniera sin `media` |
| `torre_project/settings.py` | `TORRE_MODO_IMPRESION=relay`, `TORRE_TOKEN_IMPRESION`, `TORRE["IMPRESION_MAX_INTENTOS"]=5` | modo, token de la API, reintentos |

Modo `local` (sin agente): Torre corre `lp` en el propio servidor con las colas de `TORRE_IMPRESORA_ETIQUETAS` (guías) y `TORRE_IMPRESORA_ETIQUETAS_51` (etiquetas) del `.env`. En producción no se usa.

## 7. Pruebas

Desde el VPS, sin tocar la laptop (encola dos etiquetas de prueba a la chica; es un trabajo real de la cola, no toca inventario ni pedidos):

```bash
sudo -u torre bash -c 'cd /srv/torre/torre && /srv/torre/venv/bin/python manage.py shell' <<'EOF'
from datetime import date
from apps.core.models import Cliente
from apps.envios.models import TrabajoImpresion
from apps.piso.etiquetas import MEDIA_ETIQUETAS, encolar_impresion, generar_pdf_etiquetas
items = [
    {"tipo": "tarima", "nombre": "PRUEBA", "qr": "U|PRUEBA"},
    {"tipo": "caja", "marca": "TORRE", "sku": "PRUEBA-01", "producto": "Etiqueta de prueba 51x51",
     "presentacion": "Prueba de impresora", "lote": "LOTE-PRUEBA", "caducidad": date.today(),
     "barras": "7500000000000", "qr": "P|7500000000000|LOTE-PRUEBA", "danada": False},
]
t = encolar_impresion(generar_pdf_etiquetas(items), tipo=TrabajoImpresion.TIPO_TARIMA,
    impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS, media=MEDIA_ETIQUETAS, referencia="PRUEBA impresora 51x51",
    cliente=Cliente.objects.order_by("pk").first(), nombre="prueba-51x51.pdf")
print("trabajo", t.pk, t.estado, "páginas", t.paginas)
EOF
```

Reencolar trabajos ya impresos (los dos últimos de etiquetas con 3 páginas o menos):

```bash
sudo -u torre bash -c 'cd /srv/torre/torre && /srv/torre/venv/bin/python manage.py shell' <<'EOF'
from apps.envios.models import TrabajoImpresion
for t in TrabajoImpresion.objects.filter(impresora=TrabajoImpresion.IMPRESORA_ETIQUETAS, paginas__lte=3).order_by("-pk")[:2]:
    t.estado, t.intentos, t.error = TrabajoImpresion.PENDIENTE, 0, ""
    t.save(update_fields=["estado", "intentos", "error"])
    print("reencolado", t.folio, t.referencia, t.paginas, "página(s)")
EOF
```

Guías: reimprimir desde Torre (Mesa → pedido → caja → imprimir guía, o desde el piso). Etiquetas: pantalla de Reetiquetado de la recepción → "Reimprimir etiquetas de esta tarima".

Ver la cola de Torre:

```bash
sudo -u torre bash -c 'cd /srv/torre/torre && /srv/torre/venv/bin/python manage.py shell' <<'EOF'
from apps.envios.models import TrabajoImpresion
for t in TrabajoImpresion.objects.order_by("-pk")[:10]:
    print(t.pk, t.estado, t.tipo, t.impresora, t.media, t.paginas, t.intentos, t.referencia, (t.error or "")[:80])
EOF
```

## 8. Diagnóstico

Comandos en la laptop, en este orden:

```bash
ps aux | grep -v grep | grep agente_impresion     # debe haber UN proceso
journalctl -u torre-impresion -n 30 --no-pager    # "Agente arriba" + una línea por trabajo
lpstat -t                                         # colas, URIs, estado, trabajos atorados
lpoptions -p AiYin_BY-480BT -l                    # opciones activas (asterisco)
lpoptions -p Etiquetas51 -l
sudo lpinfo -v | grep -iE 'usb|tspl'              # qué ve la laptop por USB
lsusb                                             # 09c6:0426 Beeprt (Aiyin), 5958:0130 YXWL (Munbyn)
sudo tail -40 /var/log/cups/error_log
sudo grep -E 'NickName|cupsFilter' /etc/cups/ppd/<cola>.ppd   # qué driver tiene una cola
```

Síntoma → causa → arreglo:

| Síntoma | Causa | Arreglo |
|---|---|---|
| Guía 1.5 veces más grande y cortada | la cola quedó en `Resolution=300dpi` (default del PPD TSPL) y la Aiyin es de 203 | `sudo lpadmin -p AiYin_BY-480BT -o Resolution=203dpi` |
| El log dice "impresa" y no sale papel | el agente apunta a una cola con driver equivocado (`BY-480BT`, HP Designjet) o a un URI viejo | `grep IMPRESORA /etc/systemd/system/torre-impresion.service` debe decir `AiYin_BY-480BT`; `lpstat -v` con el URI actual de `lpinfo -v` |
| Etiqueta de 51 mm sale gigante en la grande | agente viejo (anterior al 2026-10-13) o dos agentes corriendo | un solo agente, el del repo, con `IMPRESORA_ETIQUETAS` |
| Etiquetas en blanco en la chica | cola con PPD de HP u otro no térmico | recrear `Etiquetas51` como en §4 |
| Trabajos de etiquetas quedan PENDIENTE y el log dice "queda pendiente" | falta `IMPRESORA_ETIQUETAS` en el servicio | agregarla y reiniciar |
| Trabajo en ERROR en Torre tras 5 intentos | `lp` falló 5 veces (cola pausada, impresora apagada) | arreglar la cola y reencolar (§7) |
| Contenido desplazado | offset | `Horizontal`/`Vertical` en la cola (8 puntos = 1 mm) |
| La impresora cambió de puerto USB | reconexión | `sudo lpadmin -p <cola> -v "<URI de lpinfo -v>"`, no crear otra cola |

## 9. Historial

- 2026-08-20: Ubuntu crea sola `BY-480BT` con PPD de HP Designjet (nunca imprimió).
- Hasta el 2026-10-13: las guías salían por una cola `AiYin_BY-480BT` cuyo driver no quedó registrado.
- 2026-10-13: se instala la Munbyn ITPP130B. Primer intento con PPD de HP (etiquetas en blanco); resuelto con `tspl-cups-driver` 1.3.6 y la cola `Etiquetas51`. Se despliega el agente de dos impresoras. En el proceso la cola `AiYin_BY-480BT` quedó mal (guías gigantes), el agente se apuntó por error a `BY-480BT` (nada salía) y la cola de la Aiyin se borró sin respaldo. Se reconstruyó con el driver TSPL; el detalle que lo arregló fue `Resolution=203dpi`.

Pendiente recomendado: guardar en esa laptop un respaldo de `/etc/cups/ppd/` (`sudo tar czf ~/respaldo-ppd-$(date +%F).tgz /etc/cups/ppd`) y traer una copia a `deploy/cups/` del repo para que recrear una cola sea copiar un archivo.
