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

## Reparto de carriers por porcentajes (plan cerrado 2026-09-14, ejecutar 2026-09-15)

**Objetivo:** Colima manda x% de pedidos por iMile y el resto por 99minutos
(Diego: 75/25) para medir incidencias por carrier y, después, ajustar pesos
por costo de incidencias. Es una opción POR CLIENTE; Infinitea y los demás no
cambian.

**Decisiones acordadas con Chema:**
- Aleatorización por bloques ("baraja"): el bloque tiene `tam` cartas con los
  carriers en la proporción exacta de los pesos; se baraja con semilla fija y
  cada pedido elegible saca la siguiente carta. El reparto es exacto al cerrar
  cada bloque; a media baraja puede ir desviado, es lo esperado.
- Tamaño de bloque = mínimo que representa los pesos exactos (75/25 → 4,
  70/30 → 10, 92/8 → 25), con tope `TORRE["REPARTO_BLOQUE_MAX"] = 100`;
  si los pesos exigen más de 100 cartas se redondean y la ficha avisa cuánto
  ("92.25 → 92"). Pesos con hasta dos decimales. Con 31 pedidos/día un bloque
  de 100 cierra en 3-4 días.
- Semilla: SHA-256 del texto `"<slug>-<bloque>"` (sin año: no cambia el 1 de
  enero). `random.Random(semilla).shuffle(cartas)`; cartas ordenadas por
  carrier antes de barajar para que el resultado sea reproducible. NUNCA
  `hash()` de Python (cambia por proceso).
- Sin consultas que crezcan con la historia: `Cliente.reparto_cursor` (entero,
  siguiente N) se toma bajo `select_for_update` y se incrementa; bloque y
  posición = `divmod(n - reparto_base, tam)`; la baraja del bloque se
  recalcula en memoria (≤100 elementos). Ni count ni query de pedidos.
- `Cliente.reparto_base` = N en que entraron en vigor los pesos actuales. Al
  cambiar pesos: base = cursor, arranca bloque nuevo; el bloque parcial
  anterior se abandona (no es exacto, se ve en el reporte). Cambios de pesos
  serán esporádicos.
- Orden de decisión por pedido (envios.services.elegir_carrier):
  1. ReglaEnvio explícita del cliente/global → gana, no consume carta,
     evento "forzado".
  2. Local con flota propia → como hoy.
  3. Cliente con integración "reparto" → sacar carta; evento de auditoría
     `reparto_carrier` con n, bloque, posición, carta.
  4. Si no, lo de hoy (99minutos directo o carrier preferente).
- SIN handover automático: si el carrier de la carta falla al cotizar o
  generar, el pedido queda sin plan/guía y visible en Mesa como fallo, igual
  que hoy con envia; Mesa decide a mano (evento con quién y por qué). La carta
  NO se devuelve ni se corrige la baraja: la diferencia entre cartas asignadas
  y guías efectivas ES la tasa de fallo de la integración, y se quiere ver.
  El fallback automático se activará como opción cuando las integraciones
  lleven tiempo limpias. Sin exclusiones por cobertura (ambos cubren todo el
  país).
- Todo forzado (antes o después de la carta) deja evento con actor y motivo.

**Entregable (un commit + seed/tests):**
- Migración core: `Cliente.reparto_pesos` (JSON `{"imile": 75, "noventa9Minutos": 25}`),
  `reparto_cursor` (int, 0), `reparto_base` (int, 0). Sin tabla nueva.
- `Cliente.INTEGRACION_REPARTO = "reparto"` como tercera opción de
  "Integración de envíos" ("Reparto por porcentajes"); en la ficha, al
  elegirla, pesos editables por carrier (choices = CARRIERS_COTIZAR +
  noventa9Minutos + imile cuando exista) con validación: suman 100, dos
  decimales, aviso del tamaño de bloque y del redondeo. Guardar pesos
  distintos → base = cursor.
- `apps/envios/reparto.py`: `tamano_bloque(pesos, tope)`, `baraja(slug, bloque,
  pesos, tam)`, `sacar_carta(cliente, pedido)`; integrado en `elegir_carrier`.
  El proveedor del carrier sale de `PROVEEDOR_POR_CARRIER` como hoy.
- Mesa → Reportes → "Reparto de carriers": por cliente y mes: cartas
  asignadas por carrier, guías efectivas, fallos (carta ≠ guía o sin guía),
  forzados, bloque en curso (n de tam). Base para revisar pesos con Diego.
- Tests: bloque mínimo y tope; baraja reproducible (misma semilla, mismo
  orden) y exacta (conteo por carrier); cursor atómico; base al cambiar
  pesos; elegir_carrier respeta reglas → local → reparto → default; sin
  fallback al fallar; eventos de auditoría.
- Seed: Colima en "reparto" 75/25 con algunos pedidos repartidos.
- Hasta que exista AdapterImile, probar con dos carriers existentes
  (p. ej. noventa9Minutos y estafeta).
