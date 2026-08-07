# SOP-05 · Picking

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-05 · v1.0 · Agosto 2026 |
| **Dueño** | OP-2 Operador de piso (OP-1 apoya en pico) |
| **Frecuencia / disparador** | Olas durante el día; todo pedido que entró antes del corte (14:00) se pickea HOY |
| **Tiempo estándar** | 5–10 min por pedido típico |
| **EPP requerido** | Botas con casquillo; guantes anticorte al manejar cajas |
| **Pantallas de Torre** | Piso → **Picking** (`/piso/picking/`) → **"Iniciar picking"** / **"Escanear"** → detalle del pedido → **"Confirmar pieza"** |

El picking es donde nace (o muere) el "pedido perfecto". La regla de la casa: **nada se cierra sin verificar** — cada pieza se escanea, y Torre no acepta piezas que no son del pedido. Tu escáner es el que evita mandarle al comprador la cerveza equivocada.

**Un carrito = un pedido.** Nunca dos pedidos en el mismo carrito, ni "los junto y luego los separo en la mesa". Así nacen los pedidos cruzados.

---

## Pasos

1. **Abre Piso → Picking.** Verás dos listas: **"Por iniciar"** (pedidos pendientes) y **"En picking"** (los que ya están en curso, con su barra de avance).
2. **Toma el siguiente pedido** de "Por iniciar" y pulsa **"Iniciar picking"**. Torre confirma: *"Picking de [folio] iniciado. Escanea línea por línea."* (Si un pedido de la lista trae la etiqueta roja **"faltante"**, tiene una incidencia activa: pregúntale a OP-1 antes de tomarlo.)
3. **Lee el detalle del pedido antes de caminar**:
   - Encabezado: cliente, comprador y, si aplica, la etiqueta **"entrega local"**.
   - ⚠️ **Banner de pedido DIVIDIDO**: si aparece *"Este pedido va DIVIDIDO en N paquetes"*, el pedido viaja en varias cajas, **cada caja con su propia guía**. La pantalla te muestra cada **"Paquete N de N"** con su contenido exacto. **No mezcles el contenido entre cajas**: pickea y agrupa en el carrito respetando el plan por paquete (usa separación física en el carrito desde ya — le ahorras el error a empaque).
   - Tabla "Líneas del pedido": SKU, cantidad, y la columna **"Dónde está"** con hasta 3 ubicaciones sugeridas.
4. **Sigue el FEFO de la pantalla**: las ubicaciones sugeridas vienen **ordenadas por caducidad** — la primera es el lote que caduca antes. Como dice la propia pantalla: *"Toma primero el lote que caduca antes (FEFO)."* Toma de la primera ubicación sugerida; si el lote que pide ya no está ahí, NO tomes otro lote sin avisar (ve el paso 7).
5. **Camina la ruta con el carrito** en orden de pasillo (A-\* luego B-\*), tomando las cajas con técnica (SOP-11): pesado abajo en el carrito, botellas paradas, nada volado por los bordes.
6. **Escanea pieza por pieza** en el detalle del pedido:
   - Campo **"Código de barras"**: escanea el código de la pieza/caja que tienes en la mano (el cursor ya está ahí).
   - Campo **"Cantidad"**: por default 1; si tomaste varias piezas idénticas del mismo lote, captura cuántas.
   - Pulsa **"Confirmar pieza"**. Torre avanza la línea: *"[SKU]: van X de Y."*
   - Si escaneas algo que no es del pedido, Torre te frena: *"Código equivocado: [código] no corresponde a ningún producto de este pedido. Regresa la pieza y toma la correcta."* — obedécelo literal: la pieza REGRESA a su anaquel, no se queda en el carrito.
   - Si la línea ya está completa, Torre también te frena: *"La línea de [SKU] ya está completa. No pickees de más."*
7. **¿Falta pieza física?** (la ubicación está vacía o el lote no aparece):
   - ⚠️ **NO ajustes nada. NO tomes de otro lote ni de otra ubicación "para completar" sin registro.** El descuadre es un síntoma; si lo tapas, se vuelve inventario fantasma.
   - **Reporta a OP-1 en el momento.** OP-1 dispara un conteo de ese SKU (SOP-09) y Mesa decide con el cliente: sustituir, mandar parcial o retener. Deja el pedido en pausa (queda "En picking" con su avance guardado).
   - Tú no le avisas al cliente ni al comprador: eso es de Mesa.
8. **Termina el pedido**: cuando escaneas la última pieza, Torre te lo dice: *"Pedido [folio] completo. Llévalo a la mesa de empaque."* y te lleva directo a la pantalla de empaque. También verás el banner *"Pedido completo: todas las piezas escaneadas"* con el botón **"Ir a empaque"**.
9. **Lleva el carrito a la mesa de empaque** tal cual (un carrito = un pedido) y continúa con SOP-06, o entrégaselo a quien empaca y toma el siguiente pedido.

---

## Errores comunes y cómo se ven

- **Pickear de memoria y escanear todo junto al final, en la mesa.** Se ve como pieza equivocada detectada hasta la báscula (o peor, hasta el comprador). El escaneo es EN el anaquel, pieza por pieza.
- **Tomar el lote más a la mano en lugar del primero sugerido.** Se ve como FEFO roto: lotes viejos acumulándose hasta caducar. La primera ubicación de la lista es la buena.
- **Dos pedidos en un carrito "porque van al mismo rumbo".** Se ve como incidencia FAL doble: a un comprador le llega de más y a otro de menos.
- **Dejar en el carrito la pieza que Torre rechazó** ("ahorita la regreso"). Se ve como caja de más en la mesa de empaque y descuadre en el anaquel.
- **"Completar" una línea con cantidad inflada en el campo Cantidad** sin tener las piezas en la mano. El peso no va a cuadrar en empaque y el pedido se regresa completo. Escanea lo que cargas, no lo que deberías cargar.
- **Ignorar el banner de pedido dividido.** Se ve como una caja de 25 kg imposible de flejar y dos guías para un solo bulto. El plan de paquetes se respeta desde el carrito.

## NUNCA hagas esto

- NUNCA pickees sin escanear, ni escanees un código "igualito" de otra pieza.
- NUNCA mezcles pedidos en un carrito, ni contenido entre cajas de un pedido dividido.
- NUNCA ajustes inventario ni cambies de lote por tu cuenta ante un faltante: se reporta a OP-1.
- NUNCA tomes producto de cuarentena (MER-01/RET-01) para completar un pedido, esté como esté de "nuevecito".
- NUNCA avises tú al cliente o al comprador de un faltante: eso es de Mesa.

## Qué queda registrado

- **"Iniciar picking"**: el pedido pasa a EN_PICKING, con usuario y hora.
- Cada **"Confirmar pieza"**: línea, cantidad, código escaneado, usuario, timestamp → el apartado de inventario se convierte en picking real (kardex) + EventoAuditoria.
- El avance del pedido (X de Y piezas) visible en Piso → Picking y para Mesa en vivo.
- Los rechazos de código equivocado NO mueven inventario (por eso la pieza debe regresar físicamente: el sistema jamás supo que la cargaste).

---

## ✂️ Checklist imprimible — PICKING

```
FECHA: ______  PEDIDO: ____________  OPERADOR: ______

[ ] "Iniciar picking" pulsado (no pedidos con pill "faltante" sin
    preguntar a OP-1)
[ ] Detalle leído: ¿local? ¿DIVIDIDO en N paquetes? ¿nota?
[ ] Un carrito = UN pedido (dividido: separado por paquete)
[ ] FEFO: tomé de la PRIMERA ubicación sugerida
[ ] Cada pieza escaneada EN el anaquel → "Confirmar pieza"
[ ] Pieza rechazada por Torre → regresada al anaquel
[ ] ¿Faltante físico? → OP-1 avisado, NADA ajustado: SKU ______
[ ] Mensaje "Pedido completo" → carrito a mesa de empaque
FIRMA: ____________________
```
