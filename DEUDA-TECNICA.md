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
