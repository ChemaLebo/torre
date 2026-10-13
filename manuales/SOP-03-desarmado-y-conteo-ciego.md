# SOP-03 · Desarmado de tarimas y conteo ciego por escaneo

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-03 · v2.0 · Octubre 2026 (una caja por escaneo, etiquetas y tarimas) |
| **Dueño** | OP-1 Jefe de bodega (captura). FLEX/OP-2 desestiban |
| **Frecuencia / disparador** | Cada recepción. **El pallet se desarma el MISMO día, siempre** (cláusula de contrato) |
| **Tiempo estándar** | 30–45 min por tarima (desestiba + escaneo + etiquetas + confirmación) |
| **EPP requerido** | Botas con casquillo + guantes anticorte. Faja recomendada. Lentes si aparece vidrio roto |
| **Pantallas de Torre** | Piso → **Recepción** → **"Trabajar"** → **¿Dónde estás escaneando?** → escanear caja por caja → **¿Qué lote?** → **Imprimir etiquetas** → **Confirmar tarima** → **Cuadre y cierre** |

Una tarima sin desarmar es inventario fantasma: producto que existe pero no se puede vender, no se puede pickear y nadie sabe si llegó completo. Por eso la regla es brutal y simple: **lo que se descargó hoy, se cuenta hoy**. Y se cuenta **a ciegas y con el lector**: tú no sabes cuánto anunció el cliente, y cada caja existe porque la escaneaste, no porque la viste. Una caja idéntica a otra (misma caja, distinto producto adentro) engaña al ojo; al lector no. **Nada es vendible hasta que la orden se cierra.**

---

## A. Desestiba segura (la parte física)

1. **Coloca la tarima** en la zona de descarga/desarmado con espacio libre en al menos dos lados. Ten a la mano: carrito, cutter con guarda, y un espacio limpio para formar columnas por SKU.
2. **Corta el emplaye de arriba hacia abajo** con el cutter alejando el filo de tu cuerpo, y ve enrollándolo (el emplaye suelto en el piso es una trampa de resbalón). Junta los flejes cortados de inmediato: un fleje pisado rebota.
3. **Desestiba SIEMPRE de arriba hacia abajo, capa por capa.**
   - ⚠️ **NUNCA jales una caja de en medio o de abajo** "porque ya viste que es la que necesitas". La columna entera se te viene encima; es vidrio de 13–16 kg por caja.
   - ⚠️ **Máximo 2 cajas por viaje a mano** (y solo si la superior va estable y ves por encima de la carga). Con caja mojada o dañada: una por viaje.
4. **Carga bien** (técnica completa en SOP-11): espalda recta, caja pegada al cuerpo, giras con los pies, no con la cintura. La caja se **deposita**, no se suelta: el vidrio no perdona los últimos 5 centímetros.
5. **Forma columnas por SKU/lote** sobre el piso o carritos: máx. 5 cajas por columna, lo pesado abajo, etiquetas de frente. Cada columna es un solo SKU — no mezcles para "ahorrar espacio", porque el conteo se hace por columna.
6. **Separa físicamente lo dañado** conforme aparece: caja rota, mojada, botellas quebradas o faltantes adentro, olor a cerveza → a un lado, en zona claramente separada. Se contará como "Dañadas" y va a cuarentena (MER-01) — con foto (SOP-10).
   - ⚠️ Si una caja gotea o suena a vidrio suelto, ábrela con guantes anticorte y lentes. Nunca metas la mano a ciegas en una caja con vidrio roto.

## B. Conteo por escaneo (una caja, un escaneo)

7. **Entiende por qué es ciego**: la pantalla de escaneo muestra solo lo que TÚ has contado, nunca lo anunciado. Si vieras el número anunciado, tu cerebro contaría "hasta llegar" a ese número. Lo anunciado lo verás hasta el cuadre, cuando ya terminaste.
8. **Elige en qué tarima estás escaneando.** La primera vez (y con **Cambiar tarima** después) Torre pregunta **"¿Dónde estás escaneando?"**: toca una **tarima existente** o crea una **nueva** con el nombre que Torre propone (TAR-09 si la última es TAR-08; puedes cambiarlo, no se repite). Al contar, pasas las cajas de la tarima que llegó a esta tarima vacía: por eso quedan en orden inverso, y por eso las etiquetas salen en ese mismo orden.
9. **Escanea UNA caja con el lector** (el campo ya tiene el foco; si no, toca la pantalla fuera de los botones; **Teclear** abre el teclado para capturar a mano). Torre abre **"¿Qué lote?"** con una tarjeta por lote (código y caducidad en grande) y la tarjeta **"Otro"** para un lote que no venga en la orden (captura código y fecha). Si la caja viene dañada, marca **"Dañado"** antes de confirmar.
   - Toca la tarjeta del lote, pulsa **Confirmar** y responde **Sí** al "¿Contar 1 caja de … en TAR-…?". Torre regresa al escáner con +1 y el aviso *"Escanea la siguiente"*.
   - **Regresar (mal conteo)** vuelve al escáner SIN contar: úsalo si escaneaste una caja sin querer.
   - Una caja que ya trae **etiqueta de Torre** (QR) suma directo, sin preguntar lote: sirve para reconteos y reingresos.
   - Un producto que **no venía en la orden** pero sí es del cliente: Torre lo agrega con 0 anunciadas y te pide su lote. Un producto de otro cliente o uno que no existe en Torre: error, nada se suma; avisa a Mesa.
   - **Anunciado sin lote:** si el cliente anunció el producto sin lote, cuenta cada caja con el lote que trae impreso usando **"Otro"**; Torre suma todo al total del producto y muestra cada lote debajo.
10. **Repite caja por caja.** La tabla **"Contado"** lleva por producto y lote cuántas van, cuántas dañadas y cuántas sin etiqueta. Cambia de tarima cuando la llenes o pases a otra.
11. **Imprime las etiquetas de la tarima** (botón **Imprimir etiquetas** en "Tarimas de la orden"). Salen en tandas: primero la etiqueta de la **tarima** (QR `U|TAR-nn`) y luego una por caja en **orden inverso al escaneo**, que es el orden físico en la tarima nueva. Pega cada etiqueta en su caja conforme salen: si una caja no cuadra con su etiqueta, el conteo falló ahí y lo verás al pegar.
    - Si el rollo se acaba, cárgalo y pulsa **Continuar impresión**: sigue la siguiente tanda. Si el corte fue a media tanda, **Reimprimir desde** la etiqueta N.
    - La etiqueta de caja lleva marca, producto, lote, caducidad y el **QR con el lote** (es lo que después hace el picking FEFO sin falla). La caja dañada lleva la banda **DAÑADA**.
12. **Confirma cada tarima** (botón **Confirmar** en "Tarimas de la orden"). La pantalla muestra lo que hay en la tarima y tres salidas:
    - **Cuadró**: cada caja tiene etiqueta y no sobró ninguna. La tarima queda **recibida**: ahora sí existe el stock, en esa tarima, todavía **no vendible**. Las dañadas van solas a cuarentena.
    - **Faltaron etiquetas**: hay cajas sin etiqueta. Vuelves al escáner en esa tarima y cuentas cada una; su etiqueta sale al momento. Al terminar, confirma otra vez.
    - **Sobraron etiquetas**: escanea cada etiqueta que sobró: Torre resta esa caja de la tarima (si no se deja escanear, teclea cuántas sobraron por lote y pulsa **Restar**).

## C. Cuadre y cierre de la orden

13. **Cierra cuando TODAS las tarimas estén confirmadas.** El botón **Ir al cuadre y cerrar** abre **Cuadre y cierre**: aquí sí ves lo anunciado contra lo contado por producto y lote. Mientras una tarima tenga cajas sin confirmar, el cierre queda apagado y la pantalla te dice cuál.
14. **Lee las alarmas antes de cerrar**: *"¿Cajas cambiadas?"* aparece cuando a un producto le faltan las mismas cajas que a otro le sobran: casi siempre son cajas idénticas contadas como el otro producto; revisa el lote y el código de las cajas, no el color. *"N cajas sin etiqueta impresa"* te recuerda imprimir.
15. **Captura "Tarimas recibidas"** (las que contaste en SOP-02 paso 15) y pulsa **Confirmar cierre**. Si hay diferencias, Torre pregunta *"¿Seguro?"*: responde que sí solo cuando ya revisaste.
16. **Entiende qué pasa al cerrar** — y por qué no debes "maquillar" nada antes:
    - **Todo lo recibido se vuelve vendible** en ese momento, en su tarima, y la tienda del cliente lo ve.
    - El cliente recibe **automáticamente** la confirmación de llegada **con tus cifras reales**: lo contado, lo dañado, las diferencias contra lo anunciado.
    - Si tu conteo no cuadra con lo anunciado, **la discrepancia abre una incidencia sola**, con folio y dueño en Mesa de Control.
    - ⚠️ **La discrepancia NO se pelea en piso.** No recuentes "hasta que cuadre", no llames al cliente, no negocies con el chofer (ya se fue). Tu chamba es que cada caja escaneada sea una caja real. La aclaración es de Mesa, con tus fotos y tu conteo como evidencia. Un conteo honesto con diferencia vale oro; un conteo "cuadrado" a fuerza vale una demanda.
    - Las correcciones (una caja mal contada que ya se cerró, una tarima que se movió) las hace **Mesa desde el editor de recepción**; el piso solo escanea.

---

## Errores comunes y cómo se ven

- **Contar "confirmando" la remisión del chofer.** Es el anti-patrón exacto que el conteo ciego previene. La remisión se archiva; tú cuentas lo que hay en el piso.
- **Mezclar dos SKUs parecidos en una columna** (misma caja, distinto producto). Se ve como dos líneas descuadradas en espejo (+12 en una, −12 en otra): el cuadre lo marca como *"¿Cajas cambiadas?"*. Con el lector no pasa si escaneas la caja, no la columna: escanea cada caja, no "una y las demás iguales".
- **Escanear la misma caja dos veces** ("¿ya había pasado esta?"). Se ve como una etiqueta sobrante al confirmar la tarima: escanéala en **Sobraron etiquetas** y listo. Mueve físicamente cada caja a la tarima nueva apenas la escaneas.
- **Confirmar la tarima sin pegar etiquetas.** Pierdes la única verificación física del conteo y el picking FEFO se queda sin QR. Imprime, pega y luego confirma.
- **Dejar lo dañado revuelto con lo bueno** "para decidir después". Se ve como botella rota vendida a un comprador final. Dañado se separa en el momento y se captura en "Dañadas".
- **Cerrar la orden con prisa sin capturar las tarimas.** El campo trae precargado lo anunciado; si llegaron menos tarimas y no lo corriges, el registro miente.
- **Contar con la tablet en español y el lector en inglés.** Se ve como `U]TAR'09` en vez de `U|TAR-09`. Torre lo corrige solo, pero la tablet debe tener el lector configurado como teclado **Inglés (Estados Unidos)** en Ajustes → Administración general → Teclado físico.

## NUNCA hagas esto

- NUNCA jales cajas de en medio o de abajo de la estiba.
- NUNCA cargues más de 2 cajas por viaje a mano.
- NUNCA dejes una tarima sin desarmar para mañana (sin autorización escrita de Mesa, que es excepción de una vez).
- NUNCA preguntes ni busques "cuánto anunciaron" antes de terminar tu conteo.
- NUNCA "cuadres" tu conteo con lo anunciado ni recuentes con el número en mente.
- NUNCA cuentes "de vista" una columna de cajas iguales: una caja, un escaneo.
- NUNCA metas la mano a una caja que suena a vidrio roto sin guantes anticorte y lentes.

## Qué queda registrado

- Cada **caja escaneada**: producto, lote, tarima, dañada, usuario y hora (bitácora de escaneos). Al **confirmar la tarima** nace el stock en esa tarima (kardex de recepción) y las dañadas entran solas a **cuarentena** (visibles en Piso → Cuarentena) — de ahí solo salen con dictamen de dos firmas (SOP-10).
- Cada tanda de etiquetas impresa (qué tarima, de qué etiqueta a cuál) y cada etiqueta sobrante anulada.
- Fotos de llegada y de daños como EvidenciaFoto ligadas al folio del ASN.
- Al cerrar: tarimas recibidas, el paso a **vendible** de todo lo recibido, timestamp de "vendible" (para el reloj SLA) y la **confirmación automática al cliente con cifras**; discrepancias → incidencia automática con folio.

---

## ✂️ Checklist imprimible — DESARMADO Y CONTEO CIEGO

```
FECHA: ______  FOLIO ASN: ____________  TARIMA ___ de ___

DESESTIBA
[ ] Cutter con guarda · emplaye y flejes recogidos
[ ] De ARRIBA hacia abajo, capa por capa
[ ] Máx 2 cajas por viaje · espalda recta
[ ] Columnas por SKU (máx 5 cajas) · etiquetas de frente
[ ] Dañado SEPARADO físicamente + foto

CONTEO POR ESCANEO
[ ] Conté SIN ver lo anunciado
[ ] Tarima elegida en "¿Dónde estás escaneando?": TAR-____
[ ] Una caja, un escaneo → lote → Confirmar → Sí
[ ] Dañadas marcadas al contar: ______ (van a cuarentena)
[ ] Etiquetas impresas y PEGADAS (tarima + cajas, en orden)
[ ] Tarima confirmada: Cuadró / Faltaron / Sobraron resuelto

CIERRE (cuando todas las tarimas estén confirmadas)
[ ] Cuadre revisado: alarma "¿Cajas cambiadas?" atendida
[ ] "Tarimas recibidas" capturadas: ______
[ ] "Confirmar cierre" pulsado → todo vendible
[ ] Discrepancia (si la hay): NO se pelea en piso — la ve Mesa
FIRMA OP-1: ____________________
```
