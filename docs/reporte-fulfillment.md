# Reporte de fulfillment por guía

Mesa → Reportes → **Fulfillment por guía**. Una fila por guía de los pedidos
que INGRESARON en el periodo (de x a x día; por default del 1° del mes a hoy);
entran todas las guías de esos pedidos aunque se hayan comprado fuera del rango.
Solo Mesa por ahora; el portal se replica después quitando costos y proveedor.

## Qué trae cada fila

- Pedido, orden de Shopify, fecha de ingreso a Torre, tipo de envío (original,
  reposición, reexpedición, cancelada, sustituida, devolución).
- Guía, paquetería, proveedor (integración que la emitió) y servicio.
- Caja: número, dimensiones en cm, peso real de báscula, peso volumétrico
  (largo × ancho × alto / 5000), peso declarado al carrier, peso que pesó el
  carrier y "cobrado por" (real, volumétrico o carrier cuando hubo repesaje).
  Para 99minutos solo hay real y volumétrico: su API no reporta pesos.
- Piezas, SKUs y lotes con caducidad (por caja en pedidos pickeados desde el
  2026-10-22; antes, por línea del pedido).
- CP de origen (el origen principal de Configuración), CP destino, ciudad,
  estado y zona de cobro (Local, Metropolitana, Foránea).
- Estado de la guía y tiempos en horas: en piso (ingreso → lista en salida),
  hasta salir (→ manifiesto), hasta recolección del carrier, de entrega.
- Importes: envío facturado sin y con IVA (tarifa de la zona del tarifario del
  cliente menos reembolsos), picking y empaque (una vez por pedido). Mesa ve
  además cotizado al planear, costo inicial, costo final (lo que envia reporta
  en el objeto de envío mientras la guía viva; si no, el inicial), costo final
  sin IVA, recargo y diferencia sin y con IVA.
- Lo que no aplica va vacío, nunca en cero.

## Filtros

Periodo, cliente, paqueterías (una, varias o todas: nada elegido = todas),
tipo de envío, estado del movimiento, zona, SKU, origen, destino (CP, ciudad
o estado), dimensiones y rango de peso real. Todo filtro aplica a la tabla,
los cortes, el resumen, las gráficas y los CSV.

## Cortes y descargas

Cada tabla tiene su botón **CSV** para pegarla en tu Excel; **Exportar CSV**
baja todo en un archivo. Cortes: productos por guía, volumen diario y mensual
(pedidos, guías, paquetes, piezas; gráfica con un panel por medida), guías por
pedido, y guías por zona, ciudad, código postal, dimensiones, rango de peso
real, rango cobrado (zona · tarifa) y paquetería, con cantidad y porcentaje.

## Zonas de cobro y orígenes (Configuración de sistema)

Admin → Configuraciones de sistema → **Orígenes de envíos** y **Zonas por
código postal**. Cada zona es un rango inclusivo de CP de destino (5 dígitos:
`01251`-`01300`) con su zona y, opcional, una ciudad para los reportes; cuelga
de un origen (hoy WOP). Un CP suelto va como `01253`-`01253`; un hueco son dos
rangos; dos rangos del mismo origen no pueden traslaparse. Lo que no cae en
ningún rango es foránea.

Después del deploy, UNA vez:

```bash
sudo -u torre bash -c 'cd /srv/torre/torre && /srv/torre/venv/bin/python manage.py cargar_zonas_cp'
```

Crea el origen WOP (Local 380 E, CP 01780) y sube los rangos de
`config/zonas_cp.csv`. Mientras no se corra, Finanzas y los reportes siguen
leyendo el CSV. Para cargar otro archivo en bloque: `--archivo zonas.csv
--reemplazar` (columnas `cp_desde,cp_hasta,zona[,ciudad]`).
