# SOP-04 · Acomodo (put-away) y FEFO

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-04 · v1.0 · Agosto 2026 |
| **Dueño** | OP-1 Jefe de bodega (responsable del SLA); OP-2 ejecuta |
| **Frecuencia / disparador** | Después de cada conteo de recepción (SOP-03) y de cada dictamen "revendible" o retorno (SOP-10) |
| **Tiempo estándar** | ≤ 4 h internas desde fin de descarga (contractual: **≤ 8 h hábiles** hasta "vendible") |
| **EPP requerido** | Botas con casquillo + guantes anticorte. Faja recomendada |
| **Pantallas de Torre** | Piso → **Recepción** → detalle de orden → tarjeta **"2 · Ubicar (put-away)"** → botón **"Ubicar"** · Piso → **Cuarentena** → sección **"Put-away pendiente"** → botón **"Ubicar"** |

Lo contado **todavía no se vende**. En Torre, el producto recibido queda "en put-away": existe, pero no es vendible ni aparece disponible en la tienda del cliente. Solo cuando lo escaneas a su anaquel se vuelve **vendible**. Por eso el put-away no es "acomodar cajas": es el paso que enciende las ventas del cliente, con reloj contractual encima.

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

## C. Ejecutar el put-away (recepción)

1. **Toma una columna contada** (un SKU, un lote) y cárgala al carrito. Verifica en la caja: SKU, lote, caducidad.
2. **Elige el destino**: hueco en picking A-\*/B-\* del SKU si tiene espacio; el resto a RES-\*. En el detalle de la orden, la tarjeta "2 · Ubicar (put-away)" lista al pie los **"Anaqueles válidos"**.
3. **Traslada y acomoda** respetando pesado-abajo, FEFO al frente, etiquetas de frente.
4. **Captura en Torre** — tarjeta **"2 · Ubicar (put-away)"**:
   - **Producto**: el selector muestra cada SKU con sus piezas "por ubicar".
   - **Cantidad**: cuántas estás dejando en ESTE anaquel (si repartes entre dos anaqueles, son dos capturas).
   - **"Ubicación escaneada"**: **escanea la etiqueta del anaquel destino** (ej. `A-01-2`). Es la etiqueta del ANAQUEL, no la del producto — si te equivocas, Torre te lo dice: *"No existe la ubicación X. Escanea la etiqueta del anaquel, no la del producto."*
   - **"Lote (si el producto lo pide)"** y **"Caducidad del lote"**: cópialos de la caja, con el calendario para la fecha.
   - Pulsa **"Ubicar"**. Torre confirma: *"N × [SKU] ubicadas en [UBICACIÓN]: ya cuentan como vendibles."*
   - ⚠️ Captura ANAQUEL POR ANAQUEL en el momento, no "todo junto al final desde la oficina". El escaneo del anaquel en sitio es lo único que garantiza que el sistema y el físico dicen lo mismo.
5. **Repite** hasta que la orden marque cero "por ubicar", y cierra la orden (SOP-03 sección C: **"Todo ubicado — cerrar orden"**).

## D. Put-away pendiente desde Cuarentena

6. **Revisa a diario** Piso → **Cuarentena**, sección **"Put-away pendiente"**: ahí caen los dictámenes "revendibles" y los retornos aceptados que esperan anaquel. Mismo procedimiento: Cantidad + "Ubicación escaneada" + botón **"Ubicar"** (si la fila ya trae lote, Torre lo conserva y no te lo vuelve a pedir).
7. Ese producto **no es vendible hasta que lo ubiques** — no lo dejes envejecer en la repisa de cuarentena.

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

- NUNCA dejes producto contado sin ubicar de un día para otro.
- NUNCA pongas cajas completas de vidrio arriba de la altura del hombro ni excedas el peso por nivel.
- NUNCA "ubiques" en un pasillo, en el piso o en un hueco que no tiene etiqueta de ubicación.
- NUNCA ignores el lote/caducidad de la caja al capturar.
- NUNCA hagas put-away de producto en cuarentena sin su dictamen de dos firmas (SOP-10).

## Qué queda registrado

- Cada **"Ubicar"**: SKU, cantidad, ubicación, lote, usuario y timestamp → kardex por SKU/lote (exportable por el cliente) + EventoAuditoria.
- El paso a estado **vendible**: es lo que empuja el disponible a la tienda del cliente. Tu escaneo enciende (o no) sus ventas.
- El timestamp "vendible" de la orden — el dato con el que se mide el SLA 8 h y sus créditos.
- El detalle de la orden muestra las piezas "por ubicar" en vivo; la pantalla de Cuarentena, el put-away pendiente.

---

## ✂️ Checklist imprimible — PUT-AWAY

```
FECHA: ______  FOLIO ASN / ORIGEN: ____________

[ ] Reloj SLA visto: vence a las ______
[ ] Columna verificada: SKU ______ · lote ______ · caduca ______
[ ] Destino correcto: picking A-*/B-* o reserva RES-*
[ ] Pesado abajo · nada arriba del hombro · etiquetas de frente
[ ] FEFO: lote próximo a caducar AL FRENTE, nuevo atrás/reserva
[ ] Anaquel ESCANEADO en sitio (no de memoria)
[ ] Lote y caducidad capturados en Torre
[ ] "Ubicar" pulsado → mensaje "ya cuentan como vendibles"
[ ] Orden con CERO por ubicar → "Todo ubicado — cerrar orden"
[ ] Put-away pendiente de Cuarentena revisado hoy
FIRMA: ____________________
```
