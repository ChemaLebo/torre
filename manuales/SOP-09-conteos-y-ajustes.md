# SOP-09 · Conteos cíclicos y ajustes de inventario

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-09 · v1.0 · Agosto 2026 |
| **Dueño** | OP-1 Jefe de bodega |
| **Frecuencia / disparador** | Conteo cíclico: **diario a las 9:00, 3 SKUs** que Torre asigna (A semanal, B quincenal, C mensual + total trimestral). Ajustes: solo cuando un descuadre confirmado lo exige |
| **Tiempo estándar** | Conteo: 20–30 min los 3 SKUs · Ajuste: 10 min |
| **EPP requerido** | Botas con casquillo |
| **Pantallas de Torre** | Piso → **Conteos** (`/piso/conteos/`) → **"Registrar conteo"** · Mesa de Control → Inventario → tarjeta **"Ajuste con doble firma"** → **"Aplicar ajuste"** |

El conteo cíclico diario es la fuente del SLA de exactitud de inventario (≥ 99.5%) que le prometimos al cliente por contrato — con crédito si fallamos. Tres SKUs cada mañana, contados a ciegas, mantienen el inventario honesto sin cerrar la bodega a inventariar. La regla de acero: **contar es diario y normal; ajustar es excepcional y con dos firmas.**

---

## A. Conteo cíclico diario (9:00)

1. **Abre Piso → Conteos.** La pantalla *"Conteos de hoy"* muestra las 3 tarjetas de SKUs que Torre asignó, con descripción, cliente y código de barras. El recordatorio de la pantalla es la regla: *"Conteo CIEGO: cuenta lo que hay en el anaquel, sin ver el sistema."*
2. **Ve al anaquel y cuenta TODO el SKU**: todas sus ubicaciones de picking (A-\*/B-\*) **y** de reserva (RES-\*). Pieza por pieza, caja por caja (verifica que las cajas estén llenas: una caja de 12 con 10 adentro cuenta 10).
   - ⚠️ NO cuentes lo que está en cuarentena (MER-01/RET-01) ni lo apartado físicamente en carritos de picking en curso — si hay un pedido a medio pickear de ese SKU, cuéntalo al terminar esa ola o coordínate con OP-2 para contar en un momento quieto.
   - ⚠️ Conteo CIEGO de verdad: no abras la pantalla de inventario de Mesa, no le preguntes a nadie "cuántas debería haber". Tu número vale porque no sabías el esperado.
3. **Captura en la tarjeta del SKU**: campo **"¿Cuántas piezas contaste? (0 también cuenta)"** → botón **"Registrar conteo"**. Cero es un dato: si no hay nada, captura 0.
4. **Lee el resultado** (Torre compara contra el esperado SOLO después de tu captura):
   - **Cuadra**: *"[folio]: [SKU] cuadra perfecto (N piezas). Buen trabajo."* Listo.
   - **Diferencia chica** (dentro del umbral): mensaje ámbar — *"Quedó registrado; Mesa lo revisa."* No hagas nada más por ahora.
   - **Diferencia grande** (excede **$500 MXN o 12 piezas**): mensaje rojo — *"Se abrió el folio [DES-…] por descuadre: no ajustes nada sin doble firma."* La incidencia DES ya existe sola, con dueño en Mesa.
5. **Ante cualquier descuadre: NO ajustes en caliente.** La pantalla lo dice y va en serio. El protocolo:
   1. **Recuento por una SEGUNDA persona** (OP-2), también a ciegas: no le digas cuánto contaste tú ni cuánto espera el sistema. Solo dile el SKU.
   2. **Si el recuento cuadra con el sistema**: el primer conteo fue error humano. Se registra el resultado con Mesa y no se toca nada.
   3. **Si ambos conteos coinciden entre sí y difieren del sistema**: hay descuadre real. Busquen la causa 10 minutos (piezas en otra ubicación, put-away sin capturar, picking en curso, caja en cuarentena sin registrar) — la mayoría de los "faltantes" están mal acomodados, no robados.
   4. **Solo si la causa no aparece**, procede el ajuste (sección B), ligado al folio DES si existe.
6. **La tabla "Contados hoy"** muestra el resultado del día: folio, esperado, contado, diferencia y folio DES si se abrió. Esa tabla —y la fecha de último conteo por SKU— la ve el cliente en su portal.

## B. Ajustes de inventario (doble firma, siempre)

Un ajuste mueve el saldo vendible del cliente: es tocar SU inventario y SU dinero. Por eso **ningún ajuste existe sin dos personas, dos PINs y un motivo del catálogo**. FLEX jamás firma ajustes.

7. **Entra a Mesa de Control → Inventario**, busca el SKU y abre su detalle. Usa la tarjeta **"Ajuste con doble firma"** (la propia pantalla: *"El ajuste mueve el saldo vendible y queda en el kardex con folio AJU. Nada se ajusta sin dos personas."*).
8. **Captura**:
   - **"Piezas (con signo)"**: negativo quita, positivo agrega (ej. `-2`).
   - **"Motivo"**: del catálogo cerrado — las únicas causas válidas son:

     | Motivo del catálogo |
     |---|
     | Diferencia detectada en conteo |
     | Daño en bodega |
     | Producto caducado |
     | Merma operativa |
     | Corrección de recepción |
     | Robo o extravío |
     | Reconciliación con cliente |

   - **"Folio de incidencia (opcional)"**: si el descuadre abrió folio DES (o cualquier INC-…), cáptalo aquí. Si excedió el umbral de $500/12 piezas, la incidencia DEBE existir y ligarse.
   - **Firma 1 · usuario + PIN** (OP-1) y **Firma 2 · usuario (distinto) + PIN** (OP-2). Dos personas físicamente presentes: la segunda firma es una verificación real, no un PIN prestado.
9. **Pulsa "Aplicar ajuste".** Queda el folio AJU en el kardex. Toma foto de la evidencia física que motivó el ajuste (caja dañada, anaquel vacío) y súbela al folio de la incidencia con Mesa.
10. **Qué NO es un ajuste**: lo dañado va a cuarentena y sale por dictamen (SOP-10), no por ajuste; los faltantes de picking se resuelven con conteo (SOP-05 paso 7), no con ajuste directo; y nada se ajusta "para que cuadre el cierre del mes" — la reconciliación con cliente es un motivo formal con Mesa, no un borrador.

---

## Errores comunes y cómo se ven

- **Contar "ayudándose" del número del sistema.** El conteo deja de detectar nada: siempre cuadra. Se ve meses después como un descuadre gigante en el conteo trimestral. Ciego significa ciego.
- **Contar solo el picking y olvidar la reserva.** Se ve como descuadres negativos falsos ("faltan 24") que un recuento con RES-\* incluido habría resuelto en 5 minutos.
- **Ajustar en caliente al primer descuadre.** Se ve como DOS ajustes en el kardex la misma semana (uno para "corregir" y otro para revertir cuando las piezas aparecen en otra ubicación). Recuento de segunda persona SIEMPRE antes de ajustar.
- **Segunda firma de dedo** ("préstame tu PIN, yo capturo"). En auditoría las dos firmas valen lo mismo: si el ajuste está mal, los dos responden. La segunda persona verifica físicamente antes de firmar.
- **Capturar 0 sin ir al anaquel** porque "ese SKU siempre está vacío". El día que haya 12 piezas mal acomodadas, tu 0 dispara una incidencia DES de a gratis.

## NUNCA hagas esto

- NUNCA ajustes inventario sin recuento previo de una segunda persona.
- NUNCA firmes (ni prestes tu PIN para) un ajuste que no verificaste físicamente.
- NUNCA uses un motivo del catálogo que no es la causa real "porque es el más rápido".
- NUNCA dejes un descuadre mayor al umbral sin folio de incidencia ligado.
- NUNCA saltes el conteo de las 9:00 "porque hoy hay mucho trabajo" — es exactamente el día en que más se necesita.
- FLEX: NUNCA firma conteos con descuadre ni ajustes. Punto.

## Qué queda registrado

- Cada conteo: folio de conteo, SKU, esperado, contado, diferencia, usuario y hora → alimenta el SLA de exactitud publicado al cliente y la "fecha de último conteo" por SKU en su portal.
- Descuadre sobre umbral → incidencia **DES** automática con folio y dueño en Mesa.
- Cada ajuste: **folio AJU** en el kardex con delta, motivo del catálogo, ambas firmas, incidencia ligada y timestamp — en EventoAuditoria (append-only: nadie "arregla" la historia).
- El cliente ve el efecto del ajuste en su inventario y su kardex exportable. Nada se corrige en silencio: es el modelo de negocio.

---

## ✂️ Checklist imprimible — CONTEO CÍCLICO Y AJUSTE

```
FECHA: ______  HORA INICIO: ______ (meta: 9:00)

CONTEO (por SKU: 1__________  2__________  3__________)
[ ] Conté TODAS las ubicaciones (picking + reserva), a CIEGAS
[ ] Cajas verificadas llenas · cuarentena NO incluida
[ ] Capturado en "¿Cuántas piezas contaste?" → "Registrar conteo"
[ ] Resultado: CUADRA / DIF ±____ / FOLIO DES: __________

SI HUBO DESCUADRE
[ ] NO ajusté en caliente
[ ] Recuento por 2ª persona (a ciegas): ____________
[ ] Causa buscada 10 min (otra ubicación, put-away, picking,
    cuarentena): ______________________
[ ] ¿Ajuste necesario? → Mesa → Inventario → "Ajuste con doble
    firma": delta ____, motivo __________________, INC ________
[ ] Firma 1: ________ Firma 2: ________ → "Aplicar ajuste"
[ ] Folio AJU: __________ · foto de evidencia subida
FIRMA OP-1: ____________________
```
