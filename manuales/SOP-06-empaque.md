# SOP-06 · Empaque

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-06 · v1.0 · Agosto 2026 |
| **Dueño** | OP-2 Operador de piso (FLEX puede empacar ASISTIDO solo con certificación de empaque) |
| **Frecuencia / disparador** | Cada pedido con picking completo. Todo lo de antes del corte se empaca HOY |
| **Tiempo estándar** | 6–12 min por caja de vidrio bien protegida |
| **EPP requerido** | Botas con casquillo; cutter con guarda; guantes anticorte al abrir/ajustar divisores |
| **Pantallas de Torre** | Piso → **Empaque** (`/piso/empaque/`) → **"Empacar"** → detalle → botón **"Empacado"** |

Aquí es donde la rotura se gana o se pierde: el 59.8% de las incidencias del proveedor anterior fueron roturas. Nuestra apuesta es que **la prevención es el seguro**: divisores, prueba de agitado, báscula ±3% y dos fotos. La pantalla no te deja marcar "Empacado" sin peso y sin las 2 fotos — y eso es a propósito: *"Sin las 2 fotos y el peso, el botón no se enciende."*

**Certificación:** nadie toca la mesa de empaque sin estar certificado en la biblia de empaque del cliente (checklist plastificado en la mesa + video v1). FLEX solo empaca con OP-1 u OP-2 supervisando.

---

## Pasos

1. **Abre Piso → Empaque.** Los pedidos con picking completo aparecen como tarjetas con el botón **"Empacar"** (los que siguen en picking aparecen abajo con **"Terminar picking"** — esos no se empacan todavía). Pulsa **"Empacar"** en el pedido cuyo carrito tienes enfrente.
2. **Lee los avisos de la pantalla ANTES de tocar material.** Torre te dice exactamente qué estándar aplica:
   - **Pedido local** → banner rojo: *"NAKED PACKING: este pedido sale en la caja limpia de [cliente]. JAMÁS cinta del 3PL, jamás relleno de terceros."* La caja comercial del cliente va limpia: cero cinta plástica nuestra, cero branding ajeno. La marca es de ellos, no nuestra.
   - **Pedido foráneo** → banner: *"Usa SOLO insumos oficiales de [cliente] (muro de insumos del cliente). Cinta genérica del 3PL = pedido mal empacado."* Burbuja oficial y cinta oficial del muro de ese cliente — el muro de insumos por cliente existe para esto.
   - **Pedido dividido** → mismo banner que en picking: cada **"Paquete N de N"** lista su contenido; **una caja por paquete, sin mezclar**.
3. **Verifica el contenido contra la pantalla**: la tabla "Contenido" muestra cada SKU con sus piezas. Coteja física y visualmente lo del carrito. Si sobra o falta algo, NO empaques: regresa a picking (el escaneo de SOP-05 se saltó algo) y avisa a OP-1.
4. **Arma la protección anti-rotura para vidrio** (checklist plastificado del cliente en la mesa):
   - Caja del tamaño correcto (el vidrio no viaja bailando).
   - **Divisores/celdas de cartón entre cada botella**: ninguna botella toca a otra, ni vidrio contra vidrio a través de una pared de cartón sencilla.
   - **Esquinas/relleno** en los huecos perimetrales; botellas paradas (el vidrio resiste más de pie).
   - **Prueba de agitado**: cierra en falso, agita con las dos manos con ganas. *"Botellas separadas entre sí; prueba de agitado sin tintineo."* Si suena, se reacomoda. Una caja que tintinea en tu mesa llega rota a Yucatán.
5. **Nota de regalo**: si el pedido la trae, la pantalla te lo marca con la etiqueta **"nota de regalo"** y el banner con el texto exacto: *"Nota de regalo — métela en la caja"*. Imprímela/escríbela como marque la casa, SIN precio visible, y métela ANTES de cerrar. El checklist en pantalla también te lo recuerda: *"Mete la nota de regalo — este pedido la trae"*.
6. **Pesa la caja en la báscula** (verificada en apertura, SOP-01):
   - La pantalla te da el rango: *"Peso esperado: N g. La báscula debe marcar entre [mín] g y [máx] g"* (**tolerancia ±3%**).
   - Captura el número en **"Peso real de la báscula (gramos)"** — en gramos, tal cual lo marca el display.
   - ⚠️ **Si NO cuadra: abre la caja y recuenta. NO fuerces el número.** La pantalla lo dice literal: *"Si no cuadra, abre la caja y revisa el contenido: NO fuerces el número."* Un peso fuera de rango casi siempre es pieza de más, pieza de menos o SKU equivocado. Si tras recontar el físico está bien y el peso sigue fuera, avisa a OP-1 (puede ser peso maestro del SKU mal capturado — eso lo corrige Mesa, no tú).
7. **Toma las 2 fotos obligatorias** con la tablet, sin sacar el paquete de la mesa:
   - **"Foto 1 · Contenido de la caja (obligatoria)"**: caja abierta, contenido acomodado con divisores visibles.
   - **"Foto 2 · Caja cerrada con guía y display de báscula visibles (obligatoria)"**: caja cerrada SOBRE la báscula, display encendido con el peso legible, y la guía visible en el mismo cuadro. Regla de la casa mientras la guía se imprime después (en Salida): si aún no tienes la etiqueta impresa, el folio del pedido debe quedar legible en la foto, y **pegas la guía en cuanto salga de la impresora — ninguna caja entra al corral sin su guía pegada**.
8. **Pulsa "Empacado".** El botón se enciende solo cuando hay peso + 2 fotos. Torre valida el rango ±3% al confirmar; si todo cuadra: *"[folio] empacado y verificado. Llévalo a su corral de salida."* En pedidos divididos: *"empacado y verificado en N paquetes. Llévalos a su corral de salida — cada caja lleva su propia guía."*
9. **Etiqueta: una guía por caja.** En cuanto la(s) guía(s) existan (SOP-07 → **"Generar guía"**), imprime y pega **una etiqueta por caja**, sobre cara plana, sin tapar sellos del cliente, y verifica que el número de guía de la etiqueta corresponde a ESE paquete (en divididos es el error clásico).
10. **Lleva la caja a su corral** (`SAL-PQX`, `SAL-LOCAL` o `SAL-OTRO` según carrier — SOP-07) y toma el siguiente pedido.

---

## Errores comunes y cómo se ven

- **Cinta del 3PL o cinta genérica en caja de cliente.** Es la falla B2.1 del proveedor anterior, la que más enfurece a la marca. Se ve en la Foto 2 — y esa foto la ve el cliente en su portal. Solo insumos del muro del cliente.
- **"Cuadrar" el peso metiendo relleno o capturando el esperado en vez del real.** Se ve cuando el comprador reporta pieza faltante y la foto muestra una caja "que pesaba bien". Captura SIEMPRE el número real del display; si no cuadra, recuenta.
- **Prueba de agitado tímida** (dos palmaditas). Se ve como incidencia DAN a los 4 días. Agita como paquetería: con ganas.
- **Foto 2 sin el display legible o sin guía/folio.** La evidencia pierde su valor de disputa. Reacomoda y repite la foto: display + guía en el MISMO cuadro.
- **Cambiar etiquetas entre cajas de un pedido dividido.** Se ve como dos compradores recibiendo la caja del otro. Guía y paquete se cotejan uno a uno antes de pegar.
- **Empacar un pedido con líneas incompletas** "porque ya casi". Torre te frena (*"Faltan piezas por pickear"*), pero el intento ya te costó tiempo: la tarjeta con botón "Empacar" es la única lista de pedidos empacables.

## NUNCA hagas esto

- NUNCA uses cinta o insumos que no sean los oficiales del cliente (local: cero cinta plástica — naked packing).
- NUNCA marques "Empacado" con un peso que no es el del display.
- NUNCA cierres una caja que tintinea.
- NUNCA dejes una caja sin guía pegada entrar al corral.
- NUNCA menciones el precio en pedidos con nota de regalo (ni en la nota ni en papeles dentro de la caja).
- NUNCA empaques sin certificación de la biblia del cliente (FLEX: solo asistido).

## Qué queda registrado

- Peso real capturado vs esperado (validación ±3%) — con usuario y hora.
- **EvidenciaFoto** "contenido" y "caja_cerrada" ligadas al pedido — visibles para el cliente en el detalle del pedido de su portal, y evidencia ante disputas (retención 12 meses).
- Transición del pedido a EMPACADO (y sus paquetes planeados a EMPACADO) en EventoAuditoria.
- La auditoría diaria de calidad (5 paquetes al azar contra el estándar) usa estas fotos como referencia.

---

## ✂️ Checklist imprimible — EMPAQUE

```
FECHA: ______  PEDIDO: ____________  ¿LOCAL/NAKED? ___  ¿DIVIDIDO? ___

[ ] Banner leído: insumos OFICIALES del cliente (local: naked,
    CERO cinta del 3PL)
[ ] Contenido cotejado contra la tabla de la pantalla
[ ] Divisores: ninguna botella toca otra · botellas paradas
[ ] Esquinas/relleno en huecos · caja del tamaño correcto
[ ] PRUEBA DE AGITADO: sin tintineo
[ ] Nota de regalo adentro (si aplica) · SIN precio
[ ] Peso real capturado: ________ g (rango: ______–______ g)
    ¿Fuera de rango? → ABRIR Y RECONTAR, no forzar
[ ] Foto 1: contenido con divisores
[ ] Foto 2: caja cerrada + display + guía/folio LEGIBLES
[ ] Botón "Empacado" → mensaje "empacado y verificado"
[ ] Una guía por caja, pegada y cotejada
[ ] Caja al corral correcto: SAL-____
FIRMA: ____________________
```
