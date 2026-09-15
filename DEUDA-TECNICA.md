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
