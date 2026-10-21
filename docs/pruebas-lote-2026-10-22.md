# Pruebas en local · lote 2026-10-22 (lotes correctos + cancelar reposición + parar en piso + reanudar + reingresos + fix del manifiesto)

Arranca el servidor local como siempre (`TORRE_MODO_IMPRESION=local` para que nada vaya al relay) y aplica migraciones:
`./venv/bin/python manage.py migrate` (nuevas: pedidos 0023 `PickLinea`, envios 0031 `Paquete.CANCELADO` + `regreso_reportado`).

## 1. Fix del manifiesto y lote por pedido (seed)
1. `./venv/bin/python manage.py seed_escenario_cajas` (solo con DEBUG=True). Siembra el producto SEED-C24, lote SEEDLOTE, rack SEED-A3 y dos pedidos listos en Salida: SEED-CAJAS-A (tres cajas de una pieza) y SEED-CAJAS-B (una pieza en dos medias cajas).
2. Mesa → Inventario → SEED-C24: 6 vendibles en SEED-A3 y 4 en empaque, todo con lote SEEDLOTE. Mesa → Pedidos → SEED-CAJAS-A → Líneas: la columna **Lotes** dice "3 × SEED-A3 · SEEDLOTE · en empaque".
3. Piso → Salida → corral SAL-OTRO → Registrar salida: palomea solo las cajas 1 y 2 de SEED-CAJAS-A, firma. Esperado: en empaque queda 1 (antes quedaba 0), la línea dice despachadas 2, el kardex trae una fila SALIDA −2 con lote SEEDLOTE, y en Lotes: "2 × … salió en la caja 1/2" y "1 × … en empaque".
4. Manifiesta la caja 3: en empaque 0, despachadas 3, otra fila SALIDA −1.
5. SEED-CAJAS-B: manifiesta solo la caja 1 (media caja): sale la unidad entera (en empaque 0, despachada 1). La caja 2 después no resta nada.
6. Mesa → SEED-CAJAS-A → caja 3 → Cancelar guía → **Quitar de salida**: la pieza regresa a SEED-A3 con lote SEEDLOTE (antes caía al corral sin lote) y Lotes la muestra "en empaque".

## 2. Picking con lote (QR obligatorio)
1. Crea un pedido manual de un producto con lote (SEED-C24 sirve) y dale EMPEZAR en el piso.
2. Escanea el código de barras a secas: debe rechazarlo con "lleva lote: escanea el QR de la etiqueta". Escanea el QR `P|7509999000024|SEEDLOTE`: cuenta y anota posición y lote (admin → Pedidos → Piezas pickeadas, o la columna Lotes en Mesa).
3. Un producto sin lote sigue aceptando el código de barras y anota solo la posición.

## 3. Cancelar una reposición ya empacada (caso PED-00281)
1. En un pedido entregado levanta una incidencia y aprueba una reposición de un producto con existencias; dale EMPEZAR en el piso, pickea y empaca (o deja la caja empacada).
2. Mesa → pedido → Cajas: en la fila de la caja de reposición aparece **Cancelar reposición**. El modal dice lo que el kardex sabe de cada pieza ("en empaque, no descontada: tomada de …" o "ya descontada del kardex").
3. Decide por pieza: regresa a bodega (posición y lote, prellenados), cuarentena o no regresa (con "descontar del kardex" sí/no). Prueba **Dividir** para decidir dos piezas iguales por separado. Confirma: la caja queda "Cancelada" en el expediente, la compensación CANCELADA, la guía original vuelve a mandar, contadores de la línea corregidos, inventario donde dijiste.
4. Repite con **Generar reingreso (decidir después)**: la caja aparece en Mesa → **Reingresos** y se resuelve desde ahí con el mismo modal.
5. Con dos reposiciones aprobadas sobre la misma caja original, el modal pregunta cuál se cancela (la más reciente primero).

## 4. Parar en piso y Reanudar
1. Mesa → pedido en cola / en picking / en el corral: botón **Parar en piso** (arriba a la derecha). Motivo obligatorio; sin marcar cajas solo se detiene (el operador que lo tenía lo pierde, el avance se conserva, nace la DET con origen Mesa).
2. Con una caja en el corral marca "Cancelar su guía y sacarla del corral": la guía queda cancelada, la caja pierde etiquetas/evidencia/lista en salida y el pedido desaparece de Salida.
3. Marca "Desarmarla": la lista dice qué regresa y a dónde (posición y lote anotados); al confirmar el stock vuelve a su posición, queda apartado otra vez, la caja regresa a planeada y el pedido a picking.
4. **Reanudar pedido** desde el mismo detalle: resuelve solo la DET con "pedido reanudado". Si además hay "sin paquetería", el modal avisa que seguirá detenido por eso y así queda.
5. Menú **Bloqueados**: lista los detenidos del más viejo al más nuevo con lo que los detiene; Reanudar solo cuando la DET es el único bloqueo.

## 5. Reingreso por caja (caso PED-00151)
1. Mesa → pedido con una caja despachada no entregada → **Registrar reingreso**: el modal pieza por pieza (bodega/cuarentena/no regresa). Al confirmar, la guía pasa a retorno con su RF y el inventario entra como retorno a la posición y lote elegidos.
2. **Generar reingreso (decidir después)** deja la caja en Reingresos. Ahí: Registrar reingreso (modal), Crear recepción (al piso, como antes) o Inventario no recuperado.
3. Recepciones ya no lista reingresos; el badge del menú está en Reingresos.

## 6. Estatus de envia.com
Con el poller (`poll_tracking`) o un webhook: "Shipped" pasa la guía a tránsito; "Undeliverable" cuenta como intento fallido en la línea de tiempo; "Delayed", "Information" y "N/A" quedan en el historial sin mover la guía; "Redirected" manda la caja a Reingresos por decidir con su RF sin dar la guía por regresada (y "Delivered" después la quita de la lista).

## Deploy (cuando lo decidas)
`git pull` en el VPS y `systemctl restart torre` (el servicio corre `migrate` y `collectstatic` antes de arrancar). Sin cambios en el agente de impresión ni en cron. Nota: `test_seed_demo.test_escenarios_lote_a_sembrados` ya fallaba antes de este lote (en b2dde38, del lote de etiquetas): el escenario 88003 crea dos cajas; no es de estas piezas.
