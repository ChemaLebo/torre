# SOP-04 · Acomodo (put-away) y FEFO

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-04 · v1.1 · Octubre 2026 (la recepción ya no acomoda: ver SOP-03) |
| **Dueño** | OP-1 Jefe de bodega (responsable del SLA); OP-2 ejecuta |
| **Frecuencia / disparador** | Después de cada dictamen "revendible" o retorno (SOP-10). La recepción ya NO pasa por aquí: sus cajas quedan en su tarima y se vuelven vendibles al cerrar la orden (SOP-03) |
| **Tiempo estándar** | ≤ 4 h internas desde fin de descarga (contractual: **≤ 8 h hábiles** hasta "vendible") |
| **EPP requerido** | Botas con casquillo + guantes anticorte. Faja recomendada |
| **Pantallas de Torre** | Piso → **Cuarentena** → sección **"Put-away pendiente"** → botón **"Ubicar"** · Mesa → **Recepciones** → **Editar recepción** → **Mover** (cajas de una tarima a un rack) |

**Desde octubre de 2026 la recepción no acomoda** (Chema 2026-10-13): cada caja se escanea a una tarima, al terminar el conteo salen las etiquetas, la tarima se marca reetiquetada y **al cerrar la orden todo lo recibido queda vendible en su tarima** (SOP-03). Las tarimas son posiciones de reserva (TAR-nn) y el picking llega a ellas por FEFO como a cualquier reserva. Mover cajas de una tarima a un rack es una corrección que hace **Mesa** desde el editor de recepción (Mover). El **anaquel fijo por producto con restock desde reserva** es la siguiente fase y se documentará aquí cuando exista. Este SOP sigue vigente para el put-away suelto desde **Cuarentena** (dictámenes revendibles y retornos) y para las reglas físicas de los racks.

---

## A. Entiende el mapa de ubicaciones

| Prefijo | Tipo | Qué va ahí |
|---|---|---|
| **A-\*** y **B-\*** | **Picking** | Lo que se pickea a diario: cajas y piezas de rotación. Al alcance, sin escalera. |
| **RES-\*** | **Reserva** | Excedente por SKU: lo que no cabe en picking. De aquí se rellena el picking. |
| **MER-01 / RET-01** | Cuarentena | Dañado y retornos. AQUÍ NO SE HACE PUT-AWAY de producto vendible. |
| **SAL-\*** | Salida | Corrales de staging. Tampoco es put-away. |

Reglas físicas de los racks:

- **Lo pesado abajo.** Cajas completas de cerveza (13–16 kg) en los niveles bajos; presentaciones ligeras arriba. Respeta el límite de peso por nivel marcado en el rack ([COMPLETAR: kg por nivel según fabricante]).
- **Límite de altura**: nada sobresale del nivel ni se estiba por encima de la marca del rack. En picking, ninguna caja de vidrio por arriba de la altura de tus hombros: lo que bajas por encima del hombro, lo bajas a ciegas.
- Etiquetas de caja **de frente** (lote y caducidad visibles sin mover nada).
- Nada en pasillos, nunca, ni "mientras".

## B. FEFO: primero caduca, primero sale

**FEFO = First Expired, First Out.** La cerveza caduca; el lote que caduca antes debe estar donde se pickea primero.

- Al ubicar, **captura siempre el lote y su caducidad** si el producto los maneja (vienen impresos en la caja). Sin lote capturado, Torre no puede sugerir FEFO en picking y el control se rompe.
- Física del FEFO en el anaquel: el lote **más próximo a caducar al frente / más accesible**; lote nuevo atrás o en RES-\*. Si llega lote nuevo y todavía hay viejo en picking, el nuevo va a reserva — no lo pongas enfrente "porque ahí había hueco".
- En picking (SOP-05), Torre ordena las ubicaciones sugeridas por caducidad: tu acomodo de hoy es la sugerencia correcta de mañana.

## C. Recepción: ya no hay put-away por caja

1. Las cajas de una recepción **viven en la tarima donde se contaron** (SOP-03): etiqueta de tarima con QR `U|TAR-nn`, etiqueta en cada caja con su lote. Al cerrar la orden son vendibles ahí.
2. Si una tarima estorba o se vacía a un rack, **Mesa** lo registra en **Recepciones → Editar recepción → Mover** (elige la posición destino de la lista). El piso no captura posiciones en recepción.
3. El picking (SOP-05) manda al operador a la tarima o al rack donde Torre tiene el lote que caduca primero; la etiqueta de la caja le confirma el lote.

## D. Put-away pendiente desde Cuarentena

6. **Revisa a diario** Piso → **Cuarentena**, sección **"Put-away pendiente"**: ahí caen los dictámenes "revendibles" y los retornos aceptados que esperan anaquel. Mismo procedimiento: Cantidad + "Ubicación escaneada" + botón **"Ubicar"** (si la fila ya trae lote, Torre lo conserva y no te lo vuelve a pedir).
7. Ese producto **no es vendible hasta que lo ubiques** — no lo dejes envejecer en la repisa de cuarentena.
8. **Sin espacio en picking**: ubica en una posición de reserva (RES-\* o una tarima activa). Ahí el producto queda vendible con su lote; Mesa lo reacomoda cuando haya espacio. Cuarentena es solo para lo dañado o en revisión.

## E. El reloj SLA y qué pasa si no llegas

- El detalle de la orden muestra el reloj: *"Quedan X h XX min para quedar vendible"* (ámbar cuando quedan ≤ 2 h; *"SLA vencido hace X"* en rojo).
- **Si el SLA de 8 h hábiles se vence, el cliente cobra remedio: la recepción es gratis + $150 por tarima por día de retraso.** Además el retraso es visible en su portal — no hay dónde esconderlo, a propósito.
- Si ves que no llegas (avería, pico de pedidos), avisa a Mesa **antes** de que venza, con hora estimada. Un retraso avisado es gestión; un retraso descubierto es una falla.

---

## Errores comunes y cómo se ven

- **Capturar la ubicación "de memoria" desde la mesa.** Se ve como picking fantasma: Torre manda a OP-2 a `A-03-1` y ahí no hay nada. Escanea el anaquel parado frente al anaquel.
- **Ubicar sin capturar lote/caducidad** porque "es el mismo de siempre". Se ve semanas después: FEFO ciego, lote caducado en picking y merma que paga la casa.
- **Poner lote nuevo al frente del viejo.** Se ve como caducidad vencida al fondo del anaquel en el conteo trimestral.
- **Llenar el picking hasta el techo para no caminar a reserva.** Se ve como cajas por encima del hombro y estibas inestables. El excedente vive en RES-\*.
- **Repartir una columna en dos anaqueles y capturar una sola vez.** Descuadre inmediato en el siguiente conteo cíclico de ese SKU. Cada anaquel, su captura.

## NUNCA hagas esto

- NUNCA dejes producto dictaminado revendible sin ubicar de un día para otro.
- NUNCA pongas cajas completas de vidrio arriba de la altura del hombro ni excedas el peso por nivel.
- NUNCA "ubiques" en un pasillo, en el piso o en un hueco que no tiene etiqueta de ubicación.
- NUNCA ignores el lote/caducidad de la caja al capturar.
- NUNCA hagas put-away de producto en cuarentena sin su dictamen de dos firmas (SOP-10).

## Qué queda registrado

- Cada **"Ubicar"** desde Cuarentena: SKU, cantidad, ubicación, lote, usuario y timestamp → kardex por SKU/lote (exportable por el cliente) + EventoAuditoria.
- En recepción, el paso a **vendible** ocurre al cerrar la orden (SOP-03): es lo que empuja el disponible a la tienda del cliente.
- El timestamp "vendible" de la orden — el dato con el que se mide el SLA 8 h y sus créditos.
- La pantalla de Cuarentena muestra el put-away pendiente; el editor de recepción de Mesa, dónde quedó cada caja.

---

## ✂️ Checklist imprimible — PUT-AWAY

```
FECHA: ______  FOLIO ASN / ORIGEN: ____________

[ ] Reloj SLA visto: vence a las ______
[ ] Recepción: cajas en su tarima, etiquetadas y tarimas reetiquetadas (SOP-03)
[ ] Cuarentena → Put-away pendiente revisado hoy
[ ] Columna verificada: SKU ______ · lote ______ · caduca ______
[ ] Destino correcto: picking A-*/B-* o reserva RES-*/TAR-*
[ ] Pesado abajo · nada arriba del hombro · etiquetas de frente
[ ] FEFO: lote próximo a caducar AL FRENTE, nuevo atrás/reserva
[ ] Anaquel ESCANEADO en sitio (no de memoria)
[ ] Lote y caducidad capturados en Torre
[ ] "Ubicar" pulsado → mensaje "ya cuentan como vendibles"
FIRMA: ____________________
```
