# SOP-10 · Cuarentena, retornos y merma

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-10 · v1.0 · Agosto 2026 |
| **Dueño** | OP-1 Jefe de bodega (firma 1 de todo dictamen) |
| **Frecuencia / disparador** | Al detectar daño (recepción, bodega, empaque) · al llegar un retorno · dictámenes: diario en la tarde |
| **Tiempo estándar** | Ingreso a cuarentena: 5 min · Dictamen: 5–10 min por fila |
| **EPP requerido** | **Guantes anticorte + lentes de seguridad SIEMPRE que haya o pueda haber vidrio roto**; botas con casquillo |
| **Pantallas de Torre** | Piso → **Cuarentena** (`/piso/cuarentena/`): botón **"Dictaminar"** y sección **"Put-away pendiente"** → **"Ubicar"** |

La cuarentena es la sala de espera del producto dudoso. La pantalla lo dice en dos frases que son todo el SOP: *"Dañado y retornado en revisión. De aquí no se pickea nada."* y *"Nada sale de cuarentena sin dictamen con dos firmas."* Mientras algo está en cuarentena, NO es vendible, NO se pickea, NO se presta "porque se ve bien".

**Zonas físicas:** `MER-01` (dañado/en revisión) y `RET-01` (retornos). Señalizadas, separadas del stock vendible, y con su repisa siempre identificable a primera vista.

---

## A. Qué entra a cuarentena y cómo

**Entra:** dañado de recepción (capturado como "Dañadas" en SOP-03 — Torre lo manda solo), dañado en bodega o empaque (caja que se cayó, botella quebrada al abrir), producto derramado/mojado, producto con caducidad vencida detectado en piso, y **todo retorno** de paquetería o de entrega local fallida.

1. **Segrega físicamente en el momento**: la pieza/caja va a `MER-01` (o `RET-01` si es retorno) apenas se detecta. Nada dañado convive con stock bueno "mientras tanto".
2. **Etiqueta la caja/pieza** con marcador o etiqueta: fecha, SKU, cantidad, origen (recepción ASN-__, bodega, retorno pedido __) y tu nombre.
3. **Foto SIEMPRE**: el daño tal como se ve (botella rota adentro, caja mojada, emplaye violado). En recepción, la foto acompaña la línea "Dañadas" (SOP-03); en bodega, la foto va al folio de la incidencia con Mesa. Sin foto, la evidencia no existe.
4. **Registra el movimiento**: si viene de recepción, Torre ya lo tiene (campo "Dañadas"). Si es daño en bodega, repórtalo a OP-1 en el momento: el movimiento a cuarentena se registra con Mesa y, si implica bajar saldo vendible, lleva su ajuste con doble firma (SOP-09) y motivo "Daño en bodega".

## B. Retornos = incidencia P1

5. **Todo retorno abre (o ya tiene) una incidencia P1.** Un paquete que regresa es un comprador sin su compra: es lo más urgente que hay en el piso después de la seguridad.
6. **Reingreso SIEMPRE fotografiado**: recibe el paquete retornado, fotografíalo **cerrado** (como llegó), ábrelo con cuidado (puede traer vidrio roto — guantes y lentes), y fotografía el **contenido**. Verifica pieza por pieza contra el pedido.
7. **Todo el contenido entra a `RET-01`** en cuarentena — aunque se vea perfecto. Avisa a Mesa con las fotos: Mesa gestiona con el comprador/cliente (reenvío, reembolso). El producto NO regresa a vendible sin dictamen.

## C. Dictamen con dos firmas

El dictamen decide el destino de cada fila de cuarentena: **revendible** (regresa al inventario) o **merma** (baja definitiva). Lo hacen **dos personas juntas, frente al producto** — OP-1 y OP-2. FLEX no dictamina.

8. **Abre Piso → Cuarentena.** La tabla "En cuarentena" lista cliente, SKU, lote/caducidad, ubicación y piezas. Debajo hay una tarjeta de dictamen por cada fila.
9. **Revisen el producto físicamente, juntos**: ¿la caja está dañada pero las botellas intactas y limpias? ¿etiquetas presentables? ¿caducidad vigente? ¿sellos intactos? Criterio de la casa: **revendible solo lo que tú comprarías como nuevo**. Botella golpeada, etiqueta manchada, sello dudoso, olor a cerveza = merma. En la duda, merma — el costo de una merma es conocido; el de un comprador con producto dudoso, no.
10. **Captura el dictamen** en la tarjeta del SKU:
    - **"¿Cuántas piezas dictaminas?"**: puede ser parcial (de 12 en cuarentena, dictaminar 8 revendibles ahora y dejar 4 en revisión).
    - **"Destino"**: `Revendible — regresa a put-away` o `Merma — sale del inventario`.
    - **"Motivo del dictamen"**: texto corto y honesto (ej. *"caja mojada, producto intacto"*, *"3 botellas estrelladas, resto con etiqueta dañada"*).
    - **Firma 1 · usuario + PIN** y **Firma 2 · usuario (distinto) + PIN** — las dos personas presentes, cada quien teclea su PIN.
    - Pulsa **"Dictaminar"**.
11. **Si fue revendible**: Torre responde *"N piezas regresan a put-away — ubícalas para que vuelvan a estar vendibles."* La fila aparece en la sección **"Put-away pendiente"** de la misma pantalla: llévala a su anaquel y captura **Cantidad + "Ubicación escaneada" → "Ubicar"** (FEFO y reglas de SOP-04; si la fila ya trae lote, Torre lo conserva).
12. **Si fue merma**: Torre responde *"N piezas dadas de baja como merma. Quedó en el kardex."* Sigue la disposición física (sección D). La merma es dato del cliente: la ve en su kardex, con motivo y firmas.

## D. Manejo de vidrio roto y derrames

13. **Vidrio roto — el equipo va puesto ANTES de acercarte**: guantes anticorte + lentes.
    - ⚠️ **Escoba y recogedor, NUNCA las manos.** Ni los pedazos grandes "que se ven fáciles": el vidrio de botella corta como bisturí y los fragmentos chicos no se ven sobre concreto.
    - Todo el vidrio va a la **caja rígida rotulada "VIDRIO ROTO"** (nunca a bolsa de plástico ni al bote común: alguien más mete la mano ahí).
    - Revisa un radio amplio: una botella que revienta avienta esquirlas a varios metros.
14. **Producto derramado** (cerveza en el piso): aplica el protocolo de derrame de SOP-12 — señaliza con cono DE INMEDIATO (el piso mojado con cerveza es una pista de patinaje), absorbe con cartón/material absorbente, y si hay vidrio en el derrame, primero el vidrio (con equipo), luego el líquido. El piso queda seco, no "casi seco".
15. **Disposición de merma líquida/vidrio**: botellas de merma se vacían y disponen según indique el cliente/Mesa ([COMPLETAR: acuerdo de disposición con el cliente — algunas marcas exigen destrucción certificada o retiro por su cuenta]). Mientras no haya indicación, la merma dictaminada se resguarda intacta en `MER-01`.

---

## Errores comunes y cómo se ven

- **"Se ve perfecta, la regreso al anaquel y ya"** (sin dictamen). Se ve como pieza vendida que estuvo en un retorno sin revisar — y si el comprador reclama, no hay dictamen que nos respalde. TODO pasa por "Dictaminar".
- **Pickear de cuarentena para completar un pedido urgente.** De aquí no se pickea NADA. El faltante se maneja por SOP-05; la urgencia jamás justifica producto sin dictamen.
- **Dictaminar de memoria desde la tablet, sin el producto enfrente.** Las dos firmas certifican una inspección física, no un trámite. Se ve cuando la "revendible" llega al comprador con la etiqueta manchada.
- **Acumular cuarentena semanas** "porque no hay tiempo". Se ve como una repisa MER-01 desbordada, inventario del cliente congelado y un dictamen en lote hecho con prisa. El dictamen es rutina diaria de la tarde.
- **Recoger "solo el pedazo grande" con la mano.** Así son la mayoría de los cortes en bodegas. Escoba, recogedor, caja rígida. Siempre.
- **Retorno abierto sin foto del paquete cerrado.** Si falta producto adentro, ya no puedes probar si vino así. Foto cerrado → abrir → foto contenido, en ese orden.

## NUNCA hagas esto

- NUNCA saques nada de cuarentena sin dictamen de dos firmas en Torre.
- NUNCA pickees ni "prestes" producto de MER-01/RET-01.
- NUNCA toques vidrio roto sin guantes anticorte y lentes, ni lo recojas con las manos.
- NUNCA tires vidrio a bolsas de plástico o al bote común.
- NUNCA firmes un dictamen de producto que no inspeccionaste físicamente.
- NUNCA dejes un retorno sin incidencia P1 y sin fotos de reingreso.

## Qué queda registrado

- Entrada a cuarentena: desde recepción (línea "Dañadas") o por movimiento registrado con Mesa — visible en la pantalla de Cuarentena y en el inventario del cliente (columna cuarentena).
- Cada dictamen: cantidad, destino, motivo, **ambas firmas** y timestamp → kardex + EventoAuditoria. La merma queda como baja definitiva visible para el cliente.
- Put-away post-dictamen: ubicación, lote, usuario (el producto vuelve a vendible).
- Fotos de daño y de reingreso de retorno como EvidenciaFoto ligadas a su ASN/pedido/incidencia (retención 12 meses; expediente congelado si hay reclamación).
- El retorno como incidencia P1 con reloj y dueño en Mesa.

---

## ✂️ Checklist imprimible — CUARENTENA Y DICTAMEN

```
FECHA: ______

INGRESO A CUARENTENA
[ ] Segregado en el momento a MER-01 / RET-01
[ ] Etiqueta: fecha, SKU, cantidad, origen, nombre
[ ] FOTO del daño (retorno: foto CERRADO → abrir → CONTENIDO)
[ ] Retorno → incidencia P1 confirmada con Mesa

DICTAMEN (dos personas, producto enfrente)
[ ] Inspección física conjunta: sellos, etiquetas, caducidad
[ ] Criterio: ¿lo comprarías como nuevo? En la duda → merma
[ ] Cantidad + Destino + Motivo capturados
[ ] Firma 1 (OP-1) + Firma 2 (OP-2), cada quien su PIN
[ ] "Dictaminar" pulsado
[ ] Revendible → "Put-away pendiente" → "Ubicar" HOY
[ ] Merma → disposición según acuerdo del cliente

VIDRIO / DERRAME
[ ] Guantes anticorte + lentes ANTES de acercarse
[ ] Escoba y recogedor · caja rígida "VIDRIO ROTO"
[ ] Derrame: cono → absorber → piso SECO
FIRMAS: ____________  /  ____________
```
