# Pruebas en local · lote 2026-10-22 (lotes correctos + cancelar reposición + parar en piso + reanudar + reingresos + fix del manifiesto)

Arranca el servidor local como siempre (`TORRE_MODO_IMPRESION=local` para que nada vaya al relay) y aplica migraciones:
`./venv/bin/python manage.py migrate` (nuevas: pedidos 0023 `PickLinea`, envios 0031 `Paquete.CANCELADO` + `regreso_reportado`).

## 0. Arranque
```bash
cd ~/Documents/WOP/torre
git log --oneline b2dde38..HEAD          # los commits del lote
TORRE_MODO_IMPRESION=local ./venv/bin/python manage.py migrate
TORRE_MODO_IMPRESION=local ./venv/bin/python manage.py seed_escenario_cajas
TORRE_MODO_IMPRESION=local ./venv/bin/python manage.py runserver
```
El seed (solo con DEBUG=True) imprime los pk de cuatro pedidos SEED-CAJAS-A/B/C/D, el producto SEED-C24 (lote SEEDLOTE, código de barras 7509999000024) en el rack SEED-A3. Se puede volver a correr: borra y vuelve a sembrar lo suyo. No habla con ningún carrier.

## 1. Fix del manifiesto (SEED-CAJAS-A y B, ya en el corral)
1. Mesa → Inventario → SEED-C24: 13 vendibles y 4 en empaque en SEED-A3, todo con lote SEEDLOTE. Mesa → Pedidos → SEED-CAJAS-A → Líneas: columna **Lotes** = "3 × SEED-A3 · SEEDLOTE · en empaque".
2. Piso → Salida → corral SAL-OTRO → Registrar salida: palomea solo las cajas 1 y 2 de SEED-CAJAS-A y firma. Esperado: en empaque queda 1 (antes 0), despachadas 2, kardex con SALIDA −2 lote SEEDLOTE, Lotes "2 × … salió en la caja 1/2" y "1 × … en empaque".
3. Manifiesta la caja 3: en empaque 0, despachadas 3, otra SALIDA −1.
4. SEED-CAJAS-B: manifiesta solo la caja 1 (media caja): sale la unidad entera (en empaque 0, despachada 1); la caja 2 después no resta nada.
5. Mesa → SEED-CAJAS-A → caja 3 → Cancelar guía → **Quitar de salida**: la pieza regresa a SEED-A3 con lote SEEDLOTE (antes caía al corral sin lote).

## 2. Picking con lote (SEED-CAJAS-C, primero en la cola)
1. Piso → Mi turno → EMPEZAR: ofrece SEED-CAJAS-C (está en picking, libre y es el más viejo). Si te ofrece otro, abre directo `/piso/picking/<pk de C>/` con tu usuario de Mesa.
2. Escanea el código de barras `7509999000024` a secas: lo rechaza con "lleva lote: escanea el QR de la etiqueta". Escanea el QR `P|7509999000024|SEEDLOTE` dos veces: cuenta y anota posición y lote (admin → Pedidos → Piezas pickeadas; Mesa → pedido → Líneas).
3. Empaca con el wizard (báscula ~18 kg, foto de contenido). Lotes pasa a "en empaque"; el kardex trae una fila PICK con lote.

## 3. Cancelar una reposición ya empacada (SEED-CAJAS-D, segundo en la cola)
1. Mi turno → EMPEZAR ofrece SEED-CAJAS-D (reposición aprobada de 1 pieza: la caja 2 repone a la caja 1). Picking dice "0 de 1" (solo esta ola: la pieza que ya salió no cuenta) y sin banner "Va DIVIDIDO". Pickea con el QR: "1 de 1" y pasa a empaque. El asistente muestra solo la caja 2 ("Caja 2 · 1 de 1 en esta tanda"); la caja 1, que ya salió, no aparece. Empaca.
2. Mesa → SEED-CAJAS-D → Cajas: en la caja 2 aparece **Cancelar reposición**. El modal dice lo que el kardex sabe ("En empaque, no descontada: tomada de 1 de SEED-A3 lote SEEDLOTE").
3. Decide: regresa a bodega (posición y lote prellenados), cuarentena o no regresa (con "descontar del kardex"). **Dividir** parte un renglón. Confirma: caja "Cancelada" en el expediente, compensación CANCELADA, la guía original vuelve a mandar, contadores corregidos, inventario donde dijiste.
4. Vuelve a sembrar y repite con **Generar reingreso (decidir después)**: la caja aparece en Mesa → **Reingresos** y se resuelve desde ahí con el mismo modal.

## 4. Parar en piso y Reanudar
1. Mesa → SEED-CAJAS-A (o cualquier pedido en el piso): botón **Parar en piso** arriba a la derecha. Motivo obligatorio; sin marcar cajas solo se detiene (el operador lo pierde, el avance se conserva, nace la DET con origen Mesa) y desaparece de Salida.
2. Marca en una caja "Cancelar su guía y sacarla del corral": la guía queda cancelada y la caja pierde etiquetas/evidencia/lista en salida.
3. Marca "Desarmarla": la lista dice qué regresa y a dónde; al confirmar el stock vuelve a SEED-A3 con su lote, queda apartado otra vez, la caja vuelve a planeada y el pedido a picking.
4. **Reanudar pedido** en el mismo detalle: resuelve solo la DET con "pedido reanudado". Si además hay "sin paquetería", avisa que seguirá detenido.
5. Menú **Bloqueados**: los detenidos del más viejo al más nuevo con lo que los detiene; Reanudar solo cuando la DET es el único bloqueo.

## 5. Reingreso por caja (caso PED-00151)
1. Mesa → SEED-CAJAS-A después de manifestar sus cajas (o un pedido real con caja despachada no entregada) → **Registrar reingreso** en la caja: modal pieza por pieza. Al confirmar, la guía pasa a retorno con su RF y el inventario entra como retorno a la posición y lote elegidos.
2. **Generar reingreso (decidir después)** deja la caja en Reingresos: ahí Registrar reingreso (modal), Crear recepción (al piso, como antes) o Inventario no recuperado.
3. Recepciones ya no lista reingresos; el badge del menú está en Reingresos.

## 6. Estatus de envia.com
Con el poller (`poll_tracking`) o un webhook: "Shipped" pasa la guía a tránsito; "Undeliverable" cuenta como intento fallido en la línea de tiempo; "Delayed", "Information" y "N/A" quedan en el historial sin mover la guía; "Redirected" manda la caja a Reingresos por decidir con su RF sin dar la guía por regresada (y "Delivered" después la quita de la lista).

## Deploy (cuando lo decidas)
`git pull` en el VPS y `systemctl restart torre` (el servicio corre `migrate` y `collectstatic` antes de arrancar). Sin cambios en el agente de impresión ni en cron. Nota: `test_seed_demo.test_escenarios_lote_a_sembrados` ya fallaba antes de este lote (en b2dde38, del lote de etiquetas): el escenario 88003 crea dos cajas; no es de estas piezas.
