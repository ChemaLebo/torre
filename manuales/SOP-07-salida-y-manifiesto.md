# SOP-07 · Salida y manifiesto

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-07 · v1.0 · Agosto 2026 |
| **Dueño** | OP-2 Operador de piso (OP-1 responde por el escalamiento si el carrier falla) |
| **Frecuencia / disparador** | 2 recolecciones diarias de paquetería ([COMPLETAR: horas pactadas]) + salida de ruta local |
| **Tiempo estándar** | 10–20 min por recolección |
| **EPP requerido** | Botas con casquillo; chaleco si la carga al camión se hace en banqueta |
| **Pantallas de Torre** | Piso → **Salida** (`/piso/salida/`): botones **"Generar guía"** y **"Manifiesto firmado · salen N"** |

La pantalla de Salida lo dice arriba, en banner, y es la regla más sagrada de la casa: *"El 'va en camino' al comprador se dispara SOLO al firmar el manifiesto. Nada de avisar antes: si sigue en bodega, no va en camino."* El proveedor anterior marcaba "enviado" paquetes que dormían en su bodega; nosotros jamás. **El botón del manifiesto se toca con la firma del chofer ya en la mano. Nunca antes.**

---

## A. Staging por corral

1. **Conoce tus corrales** (pantalla Salida, un panel por corral):

   | Corral | Carrier |
   |---|---|
   | `SAL-PQX` | Paquetexpress (preferente) |
   | `SAL-LOCAL` | Entrega local con flota propia |
   | `SAL-OTRO` | Otros carriers (Estafeta/DHL/FedEx por cobertura) |

2. **Coloca cada caja empacada en su corral**. Torre te dice a qué corral pertenece cada pedido: en la pantalla de Salida, cada pedido aparece dentro del panel de su corral. Dentro del corral: botellas paradas, máximo 2 cajas de vidrio apiladas, guías hacia arriba/al frente para el cotejo.
3. **Genera las guías que falten**: los pedidos del corral sin guía aparecen bajo *"Sin guía todavía — genérala aquí"*. Pulsa **"Generar guía"** por pedido. Torre confirma *"Guía [número] lista para [folio]. Imprime la etiqueta y pégala en la caja."* (en divididos: *"N guías listas… Imprime una etiqueta por caja: cada paquete viaja con la suya."*).
   - Si el carrier no responde, Torre te lo dice tal cual: *"El carrier no respondió al generar la guía… Reintenta en un momento o avisa a Mesa de Control."* Reintenta una vez; si sigue, Mesa.
   - **No dejes la generación de guías para cuando el chofer ya está tocando el timbre.** Guías generadas e impresas = corral listo. La meta: todo pedido del corte empacado, con guía y en staging **antes de las 14:00** — así se mide nuestro SLA de salida, con evidencia propia.

## B. Llegada del repartidor (por cada recolección)

4. **Verifica la identidad del repartidor**: uniforme/credencial del carrier y su orden o número de recolección. Si algo no cuadra (camioneta sin logo, "vengo por unos paquetes" sin datos), NO entregues nada y llama a Mesa. Los paquetes con alcohol son mercancía apetecible.
5. **Coteja físicamente contra la pantalla**: en el panel del corral, la lista *"Con guía, listos para el chofer"* muestra folio + cliente + número de guía. Escanea/verifica **caja por caja contra esa lista**: cada caja del corral está en la lista, cada renglón de la lista tiene su caja. Cuenta total de bultos = renglones.
6. **Imprime/llena el manifiesto en papel** (formato de la casa o del carrier): fecha, carrier, número de guías, folios. **El chofer firma con nombre y hora, y se lleva su copia; la nuestra se archiva.**
   - ⚠️ El chofer NO se lleva ninguna caja que no esté en la lista, y ninguna caja de otro corral "de pasada".
7. **Ya con la firma en la mano**, pulsa en Torre **"Manifiesto firmado · salen N"** en el panel de ese corral. La leyenda bajo el botón es la regla: *"Firma del chofer en mano ANTES de tocar este botón. Marca RECOLECTADO en lote y avisa al comprador."* Torre confirma: *"Manifiesto de [corral] firmado: N pedido(s) recolectado(s). Ahora sí, el comprador recibe su 'va en camino'."*
8. **Despide al camión** y verifica el corral: debe quedar vacío. Si un paquete se quedó (chofer no lo aceptó, caja dañada), NO debió estar en el manifiesto firmado — si ya lo estaba, avisa a Mesa DE INMEDIATO para corregir antes de que el comprador reciba un aviso falso.

## C. Ruta local

9. El corral `SAL-LOCAL` funciona igual: guías generadas, cotejo, y **"Manifiesto firmado · salen N"** cuando la ruta sale — quien firma el manifiesto es quien maneja la ruta (OP-2). La pantalla te recuerda: *"Ruta local: al firmar salen a reparto. Cierra cada entrega con su POD en Entregas locales."* Sigue SOP-08.

## D. Si la paquetería no llega

10. **A las 16:00, ni un minuto después**: si la recolección pactada no ha llegado, **escala a Mesa de Control**. Mesa activa el plan B formal: **entrega en sucursal del carrier** (viaje presupuestado, con responsable) para que los pedidos del corte salgan HOY. El SLA de salida es nuestro, no del chofer.
11. Si al final del día algo no salió: queda en su corral, asegurado, anotado en la bitácora de cierre (SOP-01), y Mesa gestiona la comunicación honesta al comprador. **Jamás se firma el manifiesto "para que el aviso salga hoy"** — eso es exactamente la mentira que vinimos a matar.

---

## Errores comunes y cómo se ven

- **Firmar el manifiesto en Torre "mientras llega el chofer".** Los compradores reciben "va en camino" de paquetes que siguen en el corral. Es la falla más grave de proceso que existe en esta casa; el botón se pulsa DESPUÉS de la firma en papel.
- **Dejar que el chofer cuente solo.** Se ve como guía manifestada sin caja (retorno falso instantáneo) o caja de más sin guía. El cotejo es tuyo, caja por caja contra la pantalla.
- **Generar guías a las 13:55.** Un error del carrier a esa hora te deja sin margen. Guías se generan al llegar cada caja al corral.
- **Paquete de SAL-OTRO subido al camión de PQX** "porque igual es paquetería". Se ve como extravío sin rastro: la guía de un carrier viajando con otro no existe para nadie.
- **Escalar a las 17:00 "porque seguro ya viene".** A esa hora ya no hay plan B posible y el SLA de salida se perdió. 16:00 es el límite, y avisado se resuelve.

## NUNCA hagas esto

- NUNCA pulses "Manifiesto firmado" sin la firma física del chofer en tu mano.
- NUNCA entregues paquetes a un repartidor sin verificar identidad y orden de recolección.
- NUNCA dejes salir una caja que no esté en la lista del corral en pantalla.
- NUNCA marques recolectado un paquete que se quedó en bodega, por ninguna razón.
- NUNCA apiles más de 2 cajas de vidrio en el corral ni las acuestes.

## Qué queda registrado

- Cada **"Generar guía"**: número de guía ligado al pedido, con usuario y hora.
- Cada **"Manifiesto firmado"**: evento de auditoría del corral con la lista de folios recolectados, usuario y timestamp — el paso a RECOLECTADO en lote.
- El disparo del WhatsApp **"va en camino"** al comprador (plantilla B, con guía y rastreo) — sale de esta acción y de ninguna otra.
- El manifiesto en papel firmado por el chofer, archivado 12 meses — es NUESTRA evidencia de entrega al carrier (el escaneo del hub puede tardar 12–24 h y no dependemos de él).
- El escalamiento a Mesa (si lo hubo) como incidencia con reloj.

---

## ✂️ Checklist imprimible — SALIDA / RECOLECCIÓN

```
FECHA: ______  CORRAL: SAL-______  RECOLECCIÓN: 1ª / 2ª

ANTES DEL CHOFER
[ ] Todas las cajas en su corral correcto · máx 2 apiladas, paradas
[ ] "Generar guía" hecho para todo el corral · etiquetas pegadas
    (una guía por caja)
[ ] Corral del corte listo ANTES de las 14:00

CON EL CHOFER
[ ] Identidad + orden de recolección verificadas
[ ] Cotejo caja por caja vs lista "Con guía, listos para el chofer"
[ ] Total bultos = total renglones: ______
[ ] Manifiesto en papel FIRMADO por el chofer (copia archivada)
[ ] AHORA SÍ: botón "Manifiesto firmado · salen N"
[ ] Corral vacío al despedir el camión

SI NO LLEGÓ
[ ] 16:00 → escalado a Mesa (plan B sucursal)      HORA: ______
FIRMA: ____________________
```
