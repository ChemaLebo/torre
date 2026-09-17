# Deuda técnica

Registro de decisiones pospuestas a propósito: qué se dejó, por qué y qué falta
para cerrarlo. Una entrada por tema; se borra cuando se resuelve (el commit que
la cierra la menciona).

## Manuales por tipo de usuario

**Estado (2026-09-11):** el link "Manuales" del menú del portal está comentado
en `templates/base.html`; la ruta `portal:manuales` sigue viva y sirve el mismo
índice que Mesa.

**Por qué:** los 14 SOPs del índice (`templates/manuales/_indice.json`, generado
por `manage.py render_manuales` desde `manuales/`) son procedimientos internos
de bodega con dueño piso o Mesa; ninguno está escrito para el cliente. Mostrarlos
en el portal exponía cosas que no le son pertinentes.

**Decisión pendiente:** si se usan manuales en el portal, serán manuales
escritos para ese tipo de usuario, no los SOPs internos. Mismo criterio para
piso y Mesa: cada rol ve solo los suyos.

**Para cerrarlo:** una audiencia por manual en el frontmatter (p. ej. `Audiencia:
portal | piso | mesa`) que `render_manuales` lleve al índice; `manuales_publicados`
filtrando por rol; las vistas de portal, piso y Mesa pasando el suyo; restaurar el
link del portal cuando exista al menos un manual con audiencia portal.

## Webhooks pendientes

**Estado (2026-09-14):** solo entran `orders/create|updated|cancelled` y
`fulfillment_orders/moved` por `/hooks/shopify/<tienda>/`; todo lo demás es cron
(`sync_shopify`, `push_inventario`, `poll_tracking`, `cerrar_entregas_presuntas`).

**Por agregar, en este orden:**
1. `inventory_levels/update` filtrado a nuestra location: detectar correcciones
   manuales de stock en Shopify, marcarlas en Salud de sync y re-empujar.
2. `fulfillments/create`: un fulfillment que no creó Torre sobre un pedido en
   proceso abre incidencia y detiene el picking (evita doble salida).
3. `products/create|update|delete`: catálogo. Primero un pull en `sync_shopify`
   (variantes → alta/actualización por código); Shopify manda código, nombre,
   variante, código de barras, peso y precio; lo físico (dimensiones, lote,
   reorden, kit, categoría) es de Torre y no se pisa. Los nuevos nacen inactivos
   en una cola "por completar" en Mesa; las bajas se marcan, no se borran.
   Definir qué tienda manda si Colima tiene el mismo código en dos.
4. Webhook de rastreo de envia.com: sustituye el polling; entrega al minuto en
   portal y rastreo del comprador. Confirmar eventos y firma de envia.

## Guías externas y cadencia de rastreo

**Estado (2026-09-14):** un pedido despachado fuera de Torre no se rastrea
aunque esté EN_TRANSITO: `poll_tracking` mira guías, no pedidos. Solo se puede
dar de alta una guía desde /admin/, y solo se rastrea si la emitió envia.com o
99minutos (los proveedores con adapter).

**Por hacer:** acción en Mesa → Pedidos "Registrar guía externa" (carrier,
número, proveedor) que cree la Guia, avance el pedido, despache el stock y
marque el fulfillment en Shopify. Bajar `poll_tracking` a cada 15 min (luego 10
si el log no muestra errores por límite de API) en `deploy/crontab.txt` y en el
crontab del servidor; ampliar la ventana horaria si hay entregas después de las 22.

## Checklist de empaque configurable por cliente

**Estado (2026-09-14):** los pasos del wizard de empaque en piso están escritos en
`apps/piso/views.py::_checklist_empaque`, con dos variantes fijas (local con
naked packing / resto). Cambiar un texto o agregar un paso es código y deploy.

**Por hacer:** dos campos de texto en la ficha del cliente, "Checklist de
empaque" y "Checklist de empaque local", un paso por línea; vacío = default
actual. El wizard los muestra tal cual y sigue agregando solo el paso de la nota
de regalo. Migración chica en core.Cliente.

## Campo "Visto bueno desde (MXN)" sin uso

**Estado (2026-09-14):** `Cliente.umbral_visto_bueno_mxn` se captura y se muestra
en la ficha, pero nadie lo lee. La idea era exigir visto bueno del cliente a
compensaciones/resoluciones a partir de ese monto; el flujo de "resolución
propuesta" existe pero no depende del monto.

**Decisión pendiente:** conectarlo a compensaciones e incidencias, o quitarlo del
formulario para no confundir.

## Carrier preferente ignorado bajo integración 99minutos

**Estado (2026-09-14):** con `integracion_envios = 99minutos` el carrier
preferente no se usa (envios.services.elegir_carrier); solo una ReglaEnvio
manda por encima. El default del campo al crear cliente es 99minutos.

**Por hacer:** ayuda en el formulario que lo explique y/o ocultar el preferente
cuando la integración sea 99minutos; considerar default envia.com.

## Integración directa con iMile

**Estado (2026-09-14):** iMile solo existe vía envia.com y está fuera de
`CARRIERS_COTIZAR` porque envia cotiza pero falla al generar (no cubre CDMX
como origen, error 1300, PED-00021). No hay adapter propio.

**Hallazgos (2026-09-14):** imileexpress.com NO es la API del courier en México:
es una empresa socia en Hong Kong para envíos transfronterizos. La plataforma
real es `openapi.imile.com` (peticiones firmadas: customerId, sign, signMethod,
param); la documentación requiere cuenta en su portal de desarrolladores, Chema
la comparte cuando la tenga. Reparto acordado con Diego para Colima: 75% iMile /
25% 99minutos, al azar por pedido y configurable por cliente, para medir
incidencias por carrier (y después por estado destino); revisar mensualmente.

**Por hacer:** adapter `AdapterImile` en `apps/envios/adapters.py` con el mismo
contrato que `Adapter99Minutos` (cotizar, generar, rastrear, cancelar,
recolección si la API lo da), credenciales y modo por env (`IMILE_API_KEY`,
`IMILE_MODO` off|cotizar|full, como 99minutos), proveedor "imile" en
`PROVEEDOR_POR_CARRIER`, patrón de rastreo público en `RASTREO_CARRIER_URL`,
y tests con respuestas grabadas. Antes: conseguir credenciales y documentación
de la API de iMile México y confirmar cobertura de origen.

## Canal de venta: crudo en el pedido, traducción en tabla administrable

**Estado (2026-09-14):** `Pedido.canal` se calcula en la ingesta con
`TORRE["CANAL_POR_SOURCE"]` / `["CANAL_POR_TAG"]` (prefijos de `source_name` y
tags) y `canal_fuente` guarda el `source_name` crudo. Un valor no mapeado cae en
"otro" y mapearlo exige despliegue. Valores reales vistos en Infinitea: orden
web `source_name=web`, `app_id=580111`; renovación de suscripción Appstle
`source_name=subscription_contract_checkout_one`,
`tags=appstle_subscription_recurring_order`, `app_id=4877949` (la primera compra
de una suscripción entra como web). TikTok Shop: por confirmar con la primera
orden real de Colima.

**Diseño acordado:**
- El pedido guarda solo lo crudo: `source_name`, `app_id` y `tags` tal cual
  llegan. Se quita el `canal` calculado; la ingesta no clasifica nada.
- Tabla `CanalVenta` (cliente opcional, criterio source_name | tag | app_id,
  valor exacto, nombre visible). Solo traduce al pintar: columna, filtro y
  reporte del día. Cambiar un nombre se refleja en todos los pedidos sin
  reclasificar.
- Un valor sin fila se muestra crudo capitalizado y se registra solo en la
  tabla como "por nombrar", con cliente y conteo de pedidos.
- Pantalla en Mesa → Administración (o admin de Django) para nombrar los valores
  vistos por cliente. Precargar los fijos de Shopify (web, pos,
  shopify_draft_order = B2B) y los de Appstle (suscripción).
- Migración: crea la tabla, mueve `canal_fuente` a los campos crudos, elimina
  `canal`. El filtro por canal filtra por valor crudo.

## Reparto de carriers por porcentajes — hecho 2026-09-15

Implementado como se cerró el 2026-09-14: `Cliente.integracion_envios =
"reparto"` con `reparto_pesos` (suman 100, dos decimales), `reparto_cursor` y
`reparto_base`; `envios.reparto` (bloque mínimo con tope
`TORRE["REPARTO_BLOQUE_MAX"]=100`, baraja con semilla SHA-256 de `slug-bloque`,
`sacar_carta` bajo candado y sin consultas que crezcan); la carta queda en
`Pedido.reparto_carrier` (se saca una sola vez aunque elegir_carrier corra
varias veces por pedido: plan, cada paquete, corral de Salida) y en la
auditoría `reparto_carrier`; el planificador y el replan solo cotizan la carta
(`services.carriers_del_pedido`); sin handover al fallar; pesos editables en la
ficha con aviso de bloque y redondeo (cambiar pesos → base = cursor); reporte
Mesa → Reportes → Reparto de carriers; seed: Colima 75/25 con noventa9Minutos
y estafeta, Mérida forzado a paquetexpress por regla.

**Queda:**
- Fallback automático como opción por cliente, cuando las integraciones
  lleven tiempo limpias (hoy: sin plan/guía, visible en Salida, Mesa decide).
- Al existir AdapterImile (sección iMile): agregar "imile" a
  `envios.reparto.carriers_elegibles` y a `TORRE["PROVEEDOR_POR_CARRIER"]`;
  pasar los pesos de Colima a imile 75 / noventa9Minutos 25.
- Evento "forzado" cuando gana una ReglaEnvio: NO se registra (elegir_carrier
  corre varias veces por pedido, sería ruido); el reporte lo deriva de "guía
  sin carta". Forzar a mano un pedido a otro carrier tampoco tiene pantalla en
  Mesa (hoy: admin/shell sobre `Paquete.carrier`); el reporte lo cuenta como
  "otro carrier".
- Gap preexistente, fuera de este trabajo: para clientes SIN reparto el
  planificador cotiza toda la lista blanca y no acota al carrier de la
  ReglaEnvio que aplique (la regla solo manda en el atajo local y en el
  camino legacy sin plan). En reparto sí se respeta. Generalizarlo cambiaría
  el costo de clientes con reglas catch-all: decidir con Chema.

## Despacho parcial por guía y paquete — hecho 2026-09-15

Salida palomea POR CAJA los pedidos empacados por caja (`paquete_id`); solo
sube la caja con su foto de cierre. `marcar_recolectado(pedido, actor,
paquetes)` marca las cajas DESPACHADO, despacha del kardex lo que viaja en
ellas (una unidad de venta reempacada en medias sale con la primera media)
y deja el pedido PARCIALMENTE_DESPACHADO hasta que sale la última (→
RECOLECTADO; transición nueva). Plantilla B una sola vez (primer
manifiesto); fulfillment en Shopify con el pedido completo fuera. El poller
mueve el pedido por el conjunto de sus guías: ENTREGADO solo con todas las
activas entregadas, RETORNADO solo con todas regresadas, y no toca un pedido
con cajas en bodega. Sin migración.

**Queda:** el rastreo público ya muestra el estado por caja; falta decidir
con Chema si un pedido con una caja regresada y otra entregada debe cerrar
como ENTREGADO (hoy sí, con la incidencia RF abierta) o quedarse abierto.

## Auditoría usada como estado de negocio → columnas — hecho 2026-09-15

`Paquete.ts_cierre` + `Paquete.foto_cierre` (migración envios 0009 con
backfill desde los eventos). `cajas_cerradas_completas`, el candado de
`cerrar_caja`, el wizard de piso y el reporte del día leen las columnas;
`cerrar_entregas_presuntas` usa ts_en_transito / `Pedido.actualizado` /
último movimiento de las guías. El evento `caja_cerrada_con_evidencia`
sigue registrándose como bitácora.

**Queda:** decidir si el kardex (`inventario.Movimiento`) y la auditoría se
traslapan (pendiente desde 2026-09-10). Ya no hay lecturas de negocio sobre
`EventoAuditoria` fuera del reporte del día (quién hizo cada paso) y el
reporte de reparto.

## Reportería para clientes (pedido de Colima) — hecho 2026-09-15

Ejecutado el mismo día en seis commits (bloques 1-6 de abajo): app
`apps/reportes` con índice en Mesa → Reportes y portal → Reportes, siete
reportes nuevos más el del día. **Queda:** la columna "en búsqueda" del
inventario cuando Colima defina el término; webhooks de 99minutos solo si
algún día quieren tiempo real; ventas en dinero de Colima empiezan a
llenarse con sus primeros pedidos (los de Infinitea se rellenaron con el
backfill de la migración pedidos 0010).

### Plan original (cerrado 2026-09-15)

Nueve reportes pedidos por el equipo de Colima, en el portal (su cliente) y en
Mesa (todos los clientes). Uno ya existe (reporte del día); el resto se
construye con datos que ya guardamos, más dos piezas de datos nuevas.

**Decisiones con Chema (2026-09-15):**
- Ventas en dinero con el precio de venta REAL de Shopify (`line_items[].price`,
  con descuentos), no con el precio del catálogo (`SKU.precio_declarado`),
  que no es venta real. Columna nueva `LineaPedido.precio_unitario` (nullable),
  llenada al ingerir; el importe sale solo en líneas con precio. Relleno del
  histórico: migración que recorre `WebhookEvento.payload` de cada pedido
  (el JSON ya está guardado) y empata `line_items` con las líneas por SKU;
  Colima todavía no tiene pedidos, así que aplica a Infinitea. Pedidos
  manuales quedan sin precio.
- "En búsqueda" no existe en Torre; Chema cree que es jerga de otra
  plataforma para picking o recepción. El reporte de inventario por bodega
  sale con disponible + en tránsito (ASN anunciadas no recibidas) + en
  recepción (put-away) + apartado/en empaque; se agrega la columna cuando
  Colima defina el término.
- Costos: el portal muestra lo que el cliente PAGA (tarifario: alistamiento,
  empaque, envío por bloque y zona, almacenaje); Mesa muestra además el
  costo real de la guía (lo que pagamos). Nunca el costo real en el portal.
- Bodega: columna fija "Torre" hasta que exista una segunda bodega.
- Timestamps de paquetería: los de bodega son exactos (ts_* del pedido). Los
  del carrier se guardan con la hora que reporta el carrier en una tabla
  nueva `EventoGuia` (guia, estado canónico, estado crudo, descripción,
  ts_carrier, ts_visto, raw) alimentada por el poller (cada 15 min, cron
  del VPS). envia.com regresa el historial completo con fecha por evento
  (ya se parsea en `EnviaAdapter._evento_mas_reciente`); 99minutos tiene
  `GET /api/v3/shipments/tracking?identifier=<trackingId|internalKey>` con
  `data.events[]` (statusCode, statusName, data, createdAt) y batch en
  `/shipments/tracking/batch` (8 req/s): cambiar `Adapter99Minutos.rastrear`
  de `/shipments/{id}` (solo último estado) a tracking con historial. Sin
  necesidad de webhooks; 99minutos los ofrece (`POST /api/v3/webhooks`, JWT)
  y quedan como mejora si algún día se quiere tiempo real.

**Reportes y orden de ejecución** (cada bloque = un commit; app nueva
`apps/reportes` con la lógica compartida, Mesa = todos los clientes, portal
= el suyo; rango de fechas y CSV en todos):
1. Lotes y caducidad por producto en el portal (hoy solo Mesa → Cliente →
   Lotes), con filtro de próximos a caducar; existencias SKU × lote por
   estado con la última diferencia de conteo y de recepción
   (faltantes/sobrantes).
2. Incidencias con solución: incidencia, pedido, tipo, resolución,
   compensación (reposición/reembolso/cupón, monto, estado), reclamación al
   carrier. Daños en entrega: por periodo y carrier, entregados vs con
   incidencia DAN, porcentaje (fotos "dano" enlazadas).
3. Ventas a nivel línea por fechas: pedido, SKU, producto, cantidad, precio
   unitario e importe (solo con precio), bodega, canal, estado. Requiere
   `LineaPedido.precio_unitario` + backfill.
4. Horarios logísticos: por pedido cada hora (creado, picking, empacado,
   guía, recolectado, en tránsito, entregado) y duraciones entre pasos;
   promedios por estado destino (CP → estado), zona (local/metro/nacional) y
   carrier. Requiere `EventoGuia` y el cambio de rastreo de 99minutos.
5. Costo por entrega: por pedido, transporte (tarifa por bloque y zona) y
   almacén (alistamiento + empaque) desde `finanzas.tarifario_de`; en Mesa
   además el costo real de la guía y el margen.
6. Inventario por bodega: disponible, en tránsito, en recepción, apartado,
   cuarentena; columna "en búsqueda" cuando Colima la defina.

Ya existe y no se toca: pedidos del día con estatus, guía y evidencia
(Mesa y portal → Reportes → Reporte del día, con CSV).

## Acomodo sugerido y capacidad de anaqueles (plan cerrado 2026-09-17)

**Objetivo:** que Torre diga dónde acomodar cada producto al recibir según su
rotación, y que avise cuando un anaquel se llena. El acomodo 3D exacto no
vale la pena (Chema): es un ESTIMADO volumétrico con factor de llenado, y
el conteo cíclico lo corrige.

**Decisiones con Chema:**
- Capacidad por anaquel = volumen (largo × ancho × alto en cm) × factor de
  llenado `TORRE["FACTOR_LLENADO"] = 0.70`. Medidas reales de las celdas
  (cada celda I/D × F/B): pisos 1 y 2: 180 × 58 × 52; piso 3: 180 × 58 × 44;
  piso 4 (reserva): 180 × 58, sin alto máximo → capacidad ilimitada y
  JAMÁS se sugiere: la reserva se usa solo a mano.
- Ocupación = Σ (volumen del SKU × piezas) / capacidad. SKU sin medidas no
  cuenta y el anaquel se marca "con producto sin medidas". Aviso "lleno" a
  partir del 90 %; el put-away NO se bloquea, solo avisa (estimado).
- Rotación del SKU: clase A/B/C. Campo `SKU.rotacion` con default
  "automática"; forzada a mano en Mesa o por la columna `rotacion` del CSV
  de catálogo (Colima la llena desde su reporte de ventas de Shopify, es el
  arranque). Automática = piezas vendidas en los últimos
  `TORRE["ROTACION_DIAS"] = 90` días por SKU, acumulado 80/15/5; mientras
  no haya 90 días de datos, "automática" sin ventas suficientes = C.
- Prioridad de acceso por anaquel (`Ubicacion.prioridad`, menor = mejor),
  editable en Mesa → Inventario → Ubicaciones. Orden real (Chema
  2026-09-17): rack 1 (junto a picking) → 4; dentro del rack, lado I → D;
  dentro del lado, piso 2 → 3 → 1 (el 2 es el cómodo, el 1 el de abajo);
  dentro del piso, frente (F) → atrás (B). Es decir: PIC-1-I-F-2,
  PIC-1-I-B-2, PIC-1-I-F-3, PIC-1-I-B-3, PIC-1-I-F-1, PIC-1-I-B-1,
  PIC-1-D-F-2, … `crear_racks` la calcula con esa regla. Clase A → mejores
  prioridades, C → las peores; reserva fuera (prioridad vacía).
- Sugerencia al ubicar (Recepción y Cuarentena) como PLAN, no una celda:
  1) el anaquel donde ya vive ese SKU si tiene espacio (no dispersar);
  2) si no, el mejor anaquel libre para su clase con espacio;
  el campo ubicación se prellena con el anaquel y la cantidad con lo que
  cabe; el resto se sugiere en la siguiente entrega (recepción POR RACK:
  el flujo actual ya permite varias entregas del mismo SKU). Si el
  operador teclea otro anaquel se recalcula cuánto cabe y avisa si se pasa.
  Siempre con el motivo visible ("ya tiene este SKU · 40 % libre").
- Conteo cíclico: al contar, el operador marca "anaquel lleno" o "con
  espacio" para corregir la ocupación estimada.
- Reporte "Reacomodo sugerido": SKUs clase A en anaqueles de mala
  prioridad y el anaquel al que convendría moverlos; ocupación por anaquel.

**Entregable, tres commits:**
1. Capacidad y ocupación: `Ubicacion.largo_cm/ancho_cm/alto_cm/prioridad`
   (migración catalogo; `crear_racks` los llena por piso con las medidas de
   arriba, y un comando/edición masiva en Mesa para las existentes),
   `inventario.ocupacion(ubicacion)`; plano pintado por ocupación; panel de
   Almacén con % y "lleno"; aviso en Recepción/Cuarentena.
2. Rotación: `SKU.rotacion` (auto/A/B/C) en Mesa y en el CSV de catálogo;
   `catalogo.clase_rotacion(sku)` con el cálculo a 90 días.
3. Sugerencia y reporte: `inventario.sugerir_anaquel(sku, cantidad, orden)`
   → [(anaquel, cantidad, motivo)], prellenado en Recepción y Cuarentena,
   marca de lleno/con espacio en Conteos, reporte "Reacomodo sugerido" en
   apps/reportes.
