# Torre — Convenciones de construcción (contrato entre módulos)

Todo módulo se construye contra este documento. Las firmas de servicios y los nombres de modelos/URLs son **contratos**: se implementan EXACTAMENTE como están aquí. Referencias a modelos de otra app SIEMPRE como string en FKs: `models.ForeignKey("pedidos.Pedido", ...)`. Imports de servicios de otra app SIEMPRE lazy (dentro de la función) para evitar ciclos.

## Reglas generales

- Español en nombres de modelos, campos, URLs y templates. Código idiomático Django 5.
- **Tenancy:** todo modelo de datos de cliente lleva `cliente = models.ForeignKey("core.Cliente", on_delete=models.PROTECT)` (o se deriva vía su pedido/sku). Toda vista de portal filtra por `request.cliente` — JAMÁS por un id que venga del usuario.
- **Auditoría:** todo cambio de estado o movimiento llama `apps.core.services.registrar_evento(entidad, entidad_id, accion, actor=..., cliente=..., delta=..., motivo=...)`.
- **Evidencia:** fotos → `apps.core.models.EvidenciaFoto` (genérico: entidad + entidad_id + tipo + archivo).
- **Máquinas de estado:** cada modelo con estados define `ESTADOS`, dict `TRANSICIONES = {estado: {siguientes}}` y método `transicionar(nuevo, actor=None, motivo="")` que valida, guarda, y registra evento. Transición inválida → `ValueError`.
- **Dinero:** `DecimalField(max_digits=10, decimal_places=2)` MXN. **Fechas:** timezone-aware (`django.utils.timezone.now`).
- **Parámetros canónicos:** SIEMPRE desde `settings.TORRE[...]` — nunca números mágicos duplicados.
- **Jobs:** sin Celery en dev. Cada job es una función idempotente en `services.py` + management command que la invoca. Nombres exactos abajo.
- **Templates:** `{% extends "base.html" %}`, contexto `seccion` para marcar nav activo, clases del sistema WOP (`card`, `stat`, `pill ok|warn|crit|accent`, `banner`, `tabla-wrap`, `btn primario`, `timeline`, `medidor`, `grid g2|g3|g4`, `eyebrow`, `mono`). Nada de CSS inline salvo widths de medidores.
- **Admin:** registrar todos los modelos con list_display útil.
- **Tests:** cada app domain incluye `tests/test_*.py` (pytest-style con TestCase de Django) cubriendo su máquina de estados / servicio crítico.

## Esquema canónico por app (dueño → modelos)

### catalogo
- `SKU`: cliente FK, codigo (único con cliente: `unique_together`), codigo_barras, descripcion, peso_gr (int), largo_cm/ancho_cm/alto_cm (int), requiere_lote (bool, default True), unidad, precio_declarado (Decimal), punto_reorden (int), backorder_habilitado (bool default False), fecha_resurtido (date null), activo.
- `Ubicacion`: codigo único (`A-01-2`), tipo (recepcion|picking|reserva|merma|retorno|salida), activo. Corrales de salida: `SAL-PQX`, `SAL-LOCAL`, `SAL-OTRO`.
- `Lote`: sku FK, codigo, fecha_caducidad (date null).

### inventario
- `Saldo`: sku FK, ubicacion FK, lote FK null, estado (`en_putaway|ubicado_vendible|reservado|en_empaque|cuarentena`), cantidad (int). unique_together (sku, ubicacion, lote, estado).
- `Movimiento` (kardex, append-only): sku FK, lote FK null, tipo (`recepcion|putaway|reserva|pick|salida|ajuste|retorno|merma|conteo`), delta (int, signo), estado_origen, estado_destino, referencia (str: PED-x / ASN-x / AJU-x), actor, ts.
- `OrdenEntrada` (ASN): cliente FK, folio (`ASN-####`), estado (`ANUNCIADA→EN_RECEPCION→RECIBIDA→CERRADA`), fecha_compromiso, tarimas (anunciadas por el cliente), tarimas_recibidas (capturadas al cerrar), ts_descarga_fin, ts_vendible (cuándo quedó todo ubicado). SLA reloj contra `settings.TORRE["SLA_RECEPCION_HORAS_CONTRACTUAL"]`.
- `LineaASN`: orden FK, sku FK, cantidad_anunciada, cantidad_recibida, cantidad_danada.
- `Conteo`: folio (`CON-####`), sku FK, contador (str), esperado, contado, ts. Si |dif| supera umbral (settings) → abre incidencia DES vía servicio.
- `Ajuste`: sku FK, lote null, delta, motivo (choices catálogo cerrado), autorizo_1 (str), autorizo_2 (str) — **doble firma obligatoria** (valida PIN de dos PerfilUsuario distintos), conteo FK null, incidencia_ref (str blank).
- **Servicios (contrato):**
  - `disponible(sku) -> int` = suma ubicado_vendible − reservado − buffer del cliente.
  - `reservar(sku, cantidad, referencia) -> bool` — atómico (`select_for_update` sobre Saldo); False si no alcanza.
  - `liberar_reserva(sku, cantidad, referencia)`
  - `confirmar_pick(sku, cantidad, referencia)` — reservado → en_empaque + Movimiento pick.
  - `despachar(sku, cantidad, referencia)` — en_empaque → salida (delta negativo) + Movimiento salida.
  - `recibir(linea_asn, cantidad_ok, cantidad_danada, actor)` — crea saldos en_putaway + Movimiento recepcion.
  - `ubicar(sku, cantidad, ubicacion, lote, actor)` — en_putaway → ubicado_vendible + Movimiento putaway.
  - `cerrar_recepcion(orden, actor, tarimas_recibidas=None)` — valida que no quede put-away de los SKUs de la orden, guarda tarimas_recibidas, transiciona a CERRADA; con faltantes/sobrantes registra evento `cerrada_con_discrepancia` y abre UNA incidencia DES (lazy) con el desglose por SKU; siempre avisa al cliente vía lazy `mensajeria.enviar_recepcion_cerrada(orden)`.
  - `registrar_conteo(sku, contado, contador) -> Conteo` (calcula esperado, dispara DES si excede umbral).
  - `aplicar_ajuste(sku, delta, motivo, pin1_usuario, pin1, pin2_usuario, pin2, ...) -> Ajuste` (valida doble PIN).
  - `retornar(sku, cantidad, referencia, actor)` — reingreso por retorno + Movimiento retorno.
  - `dictaminar_cuarentena(sku, cantidad, destino, autorizo_1, pin_1, autorizo_2, pin_2, actor, lote=None, motivo_texto="", ubicacion=None)` — salida de CUARENTENA con **doble firma** (mismas reglas que aplicar_ajuste). destino `revendible`: cuarentena → en_putaway en la zona de recepción (vuelve al flujo de put-away; Movimiento ajuste, referencia `DICTAMEN`); destino `merma`: sale físico (Movimiento merma, delta negativo, referencia `DICTAMEN`). Con `ubicacion` el dictamen se acota a esa fila exacta (sku + ubicación + lote tal cual, incluido lote None) y la cantidad se valida contra esa fila; sin `ubicacion`, FEFO global del SKU (con lote si se pasó).
  - Tras CUALQUIER cambio de disponible: llamar lazy `apps.integraciones.services.encolar_push_inventario(sku)`.
- **Command:** `conteo_ciclico` — elige los 3 SKUs del día (ABC por antigüedad de conteo) y crea tareas.

### integraciones
- `Tienda`: cliente FK, plataforma (`shopify`), dominio (único), token (str), location_id, activo. **Multi-tienda: un cliente puede tener N tiendas.**
- `WebhookEvento`: tienda FK, webhook_id (único — idempotencia de entrada), topic, payload (JSON), procesado (bool), origen (`webhook|reconciliacion|manual`), ts.
- `SyncLog`: tienda FK, direccion (`ingesta|push`), resultado (`ok|error`), detalle, ts. — alimenta salud de sync.
- `PushInventarioPendiente`: sku FK, creado. (cola en BD; el push real la drena)
- **Vistas:** endpoint `POST hooks/shopify/<tienda_id>/` — valida HMAC si hay secreto, responde 200 <2s, guarda WebhookEvento y procesa síncrono en dev.
- **Servicios (contrato):**
  - `procesar_webhook(evento)` — orders/create|updated|cancelled → upsert Pedido vía `apps.pedidos.services.ingerir_pedido_shopify(tienda, payload, origen)`.
  - `encolar_push_inventario(sku)` — inserta en PushInventarioPendiente.
  - `push_inventario()` — drena la cola: **empuja on_hand** (vendible − cuarentena, menos buffer) por SKU a TODAS las tiendas del cliente; SyncLog por resultado. Sin API real (token vacío) → modo mock: registra en SyncLog `ok (mock)`.
  - `reconciliar_pedidos(tienda)` — polling `orders.json?updated_at_min=checkpoint` (mock si no hay token).
- **Commands:** `sync_shopify` (reconciliar + push), `push_inventario`.

### pedidos
- `Pedido`: tienda FK (`integraciones.Tienda`), cliente FK, shopify_order_id (unique_together con tienda), folio (`PED-#####`), origen, comprador_nombre, comprador_tel, comprador_email, direccion (JSON), cp, es_local (bool), valor_declarado (Decimal), nota_regalo (text blank), estado, corte_vigente_al_ingreso (time), timestamps por transición (ts_picking, ts_empacado, ts_guia, ts_recolectado, ts_en_transito, ts_entregado), incidencia_activa (bool — flag ortogonal), peso_esperado_gr (int), peso_real_gr (int null).
- Estados canónicos: `PENDIENTE→EN_PICKING→EMPACADO→GUIA_GENERADA→RECOLECTADO→EN_TRANSITO→ENTREGADO` + `ENTREGA_PRESUNTA`, `PARCIALMENTE_DESPACHADO`, `CANCELACION_PENDIENTE`, `CANCELADO`, `RETORNADO`. TRANSICIONES explícitas; `transicionar()` estampa timestamp y registra evento.
- `LineaPedido`: pedido FK, sku FK, cantidad, cantidad_pickeada (default 0), lote_asignado FK null.
- **Servicios (contrato):**
  - `ingerir_pedido_shopify(tienda, payload, origen="webhook") -> Pedido` — upsert idempotente por (tienda, shopify_order_id); reserva stock vía inventario.reservar por línea; si no alcanza → pedido queda PENDIENTE con flag y se abre incidencia FAL vía lazy incidencias.
  - `crear_pedido_manual(cliente, *, comprador_nombre, comprador_tel="", comprador_email="", direccion, cp, valor_declarado=None, nota_regalo="", lineas, actor) -> Pedido` — alta manual desde Mesa (cliente sin Shopify: mayoreo, B2B). `lineas` = lista de tuplas (sku, cantidad); `direccion` = dict con el shape del shipping_address de Shopify (zip, address1, city, ...). Misma semántica de reserva que la ingesta (sin stock → línea sin reservar + incidencia FAL + incidencia_activa), es_local por CP, corte vigente estampado, `valor_declarado` vacío = Σ precio_declarado×cantidad, plan de paquetes best-effort y plantilla A vía mensajeria (solo llega si hay teléfono). Registra evento `alta_manual` (entidad_id = folio). SKU ajeno o cantidad ≤ 0 → ValueError.
  - `iniciar_picking(pedido, actor)`, `confirmar_linea_pick(linea, cantidad, actor)`,
  - `empacar(pedido, actor, peso_real_gr, fotos:list)` — **valida líneas completas, tolerancia de peso ±settings.TORRE["TOLERANCIA_PESO_PCT"] y ≥1 foto tipo "contenido" ANTES de transicionar**; llama inventario.confirmar_pick→(las líneas ya pickeadas). La foto de caja cerrada YA NO se pide aquí (la guía aún no existe — fin de la paradoja de la evidencia): se exige en `cerrar_caja`. Si falla validación → ValueError con mensaje claro.
  - `empacar_caja(paquete, actor, peso_real_gr, foto_contenido)` — empaque POR CAJA (wizard del carril único): valida el peso de la báscula contra el plan de ESA caja (`paquete.peso_kg` ± TORRE["TOLERANCIA_PESO_PCT"], mismo criterio), guarda `Paquete.peso_real_gr`, adjunta EvidenciaFoto entidad="pedido" tipo="contenido" (el evento `caja_empacada` lleva el número de caja) y transiciona el Paquete PLANEADO/EN_EMPAQUE→EMPACADO. Cuando TODAS las cajas quedan EMPACADO encadena `empacar(pedido, actor, Σ pesos reales por caja, fotos=[], peso_ya_verificado=True)` — el plan por caja trae el margen de empaque del cotizador y `peso_esperado_gr` es NETO, así que la suma NO se revalida contra el neto (cada caja ya pasó su báscula). Atómico y con candado: paquete y pedido se re-leen con `select_for_update` — dos POSTs concurrentes de la misma caja se serializan y el segundo truena sobre el estado fresco. ValueError → no se toca nada. El pedido legacy sin paquetes sigue usando `empacar` directo (1 caja implícita).
  - `despachar_a_corral(pedido, actor)` — lo llama la vista en el MISMO POST del empaque: `generar_guia(pedido)` (idempotente) y por cada guía activa `apps.piso.etiquetas.imprimir_etiqueta(guia)` lazy **BEST-EFFORT** (error de impresora → se acumula en `mensajes`, JAMÁS revierte la guía; la reimpresión vive en Salida). Regresa `{"guias": [...], "mensajes": [...]}`.
  - `cerrar_caja(paquete, actor, foto_caja_cerrada)` — cierre con evidencia REAL: foto de la caja cerrada CON su etiqueta pegada. Solo cajas EMPACADO/DESPACHADO con guía; adjunta EvidenciaFoto entidad="pedido" tipo="caja_cerrada" (evento `caja_cerrada_con_evidencia` en entidad "paquete" con el número de caja — también sirve de candado: una caja no se cierra dos veces).
  - Property `Pedido.cajas_cerradas_completas` — True si cada caja EMPACADO/DESPACHADO tiene su cierre con evidencia, contado POR CAJA vía el evento `caja_cerrada_con_evidencia` de su paquete (una foto duplicada de la caja 1 jamás "cierra" la caja 2); sin plan de paquetes o con plan sin empacar por caja = 1 caja implícita (basta una EvidenciaFoto tipo "caja_cerrada" del pedido). **El manifiesto de salida excluye y avisa** ("PED-x se queda: falta foto de caja cerrada") los pedidos donde es False.
  - `generar_guia(pedido)` — lazy a envios.services.generar_guia.
  - `marcar_recolectado(pedido, actor)` — firma intacta: despacha inventario, transiciona, y lazy mensajeria.enviar_en_camino(pedido) (plantilla B: SOLO aquí, jamás antes).
  - `cancelar(pedido, actor, motivo)` — matriz por estado (post-picking → CANCELACION_PENDIENTE + tarea restock).
- **Command:** `cerrar_entregas_presuntas` — pedidos EN_TRANSITO sin evento > N días → ENTREGA_PRESUNTA.

### envios
- `Guia`: pedido FK, carrier (str), servicio, numero (str), costo_cotizado, costo_preferencial (Decimal — tasa envia.com), etiqueta_url (str), estado (`GUIA_CREADA→RECOLECTADO→EN_TRANSITO→EN_RUTA→ENTREGADO` + `INTENTO_FALLIDO|RETENIDO|RETORNO|EXCEPCION`), ultimo_evento (str), ts_ultimo_movimiento, raw (JSON).
- `ReglaEnvio`: cliente FK null (null = global), prioridad, condicion (JSON: {"es_local": true} / {"cp_prefijo": "28"} / {}), carrier, servicio.
- **Adapters:** clase base `CarrierAdapter` (cotizar, generar, cancelar, rastrear). `EnviaAdapter` — API real de envia.com: `POST {ENVIA_API_BASE}/ship/rate/`, `POST /ship/generate/`, tracking `GET {ENVIA_QUERIES_BASE}/guide/{numero}` con `Authorization: Bearer ENVIA_API_KEY`; carrier="paquetexpress" por regla para Colima. `MockAdapter` cuando no hay ENVIA_API_KEY (números MOCK-####, tracking simulable). Selección de adapter en `services.get_adapter()`.
- **Servicios (contrato):**
  - `elegir_carrier(pedido) -> (carrier, servicio)` — evalúa ReglaEnvio por prioridad; Colima → paquetexpress; local → entrega local SOLO con `settings.TORRE["FLOTA_PROPIA"]=True`. **Sin flota propia (default False)** las reglas con carrier "local" se saltan y los pedidos es_local viajan con su carrier real; las guías "local" ya emitidas no se tocan (conservan su corral SAL-LOCAL).
  - `generar_guia(pedido) -> Guia` (idempotente: si ya hay guía activa, la regresa).
  - `poll_tracking()` — rastrea toda guía no terminal, normaliza estados, actualiza pedido (EN_TRANSITO/ENTREGADO), y ante `INTENTO_FALLIDO|RETORNO|sin movimiento > umbral por ruta (settings)` abre incidencia vía lazy incidencias.services. Umbral: es_local → SIN_MOVIMIENTO_LOCAL_HORAS, foráneo → SIN_MOVIMIENTO_FORANEO_HORAS.
- **Command:** `poll_tracking`.

### incidencias
- `Incidencia`: cliente FK, pedido FK null, sku FK null, folio (`INC-AAAA-####` autogenerado), tipo (`DAN|RET|RF|FAL|DIR|CAN|DES`), prioridad (`P1|P2|P3`), origen (`auto|manual|comprador|cliente`), estado (`ABIERTA→EN_CURSO→RESOLUCION_PROPUESTA→RESUELTA→CERRADA`), dueno (str), ts_apertura, ts_primera_respuesta, ts_resolucion, ts_cierre, sla_respuesta_limite, sla_resolucion_limite (calculados al abrir con settings.TORRE).
- `MensajeIncidencia` (timeline visible al cliente): incidencia FK, autor (str), rol_autor (`mesa|cliente|comprador|sistema`), texto, interno (bool — notas internas: privadas pero auditables), ts.
- `Compensacion`: incidencia FK, tipo (`reposicion|reembolso|cupon`), monto, estado (`COTIZADA→APROBADA→PAGADA`), aprobo, fecha_pago, referencia_pago.
- `ReclamacionCarrier`: incidencia FK, carrier, monto_reclamado, estado (`PREPARADA→PRESENTADA→ACEPTADA→RECHAZADA→PAGADA`), monto_recuperado, fechas.
- **Servicios (contrato):**
  - `abrir_incidencia(cliente, tipo, origen, pedido=None, sku=None, texto="", prioridad=None) -> Incidencia` — calcula SLAs, congela evidencia del pedido (EvidenciaFoto.congelada=True), marca pedido.incidencia_activa, notifica lazy vía mensajeria.notificar_cliente_incidencia. **N incidencias por pedido permitidas.**
  - `responder(incidencia, autor, rol_autor, texto, interno=False)` — estampa ts_primera_respuesta si es la primera humana no-interna.
  - `resolver(incidencia, resolucion_texto, actor)`, `cerrar(incidencia, actor)`.
  - `abiertas_fuera_de_sla() -> queryset` — para dashboard Mesa.

### mensajeria
- `PlantillaMensaje`: clave (`A|B|E`), cliente FK null (null = default), nombre, cuerpo (con {variables}), aprobada_por_cliente (bool). Cuerpos default de A (confirmación), B (en camino: SOLO al RECOLECTADO), E (retraso — primera persona de marca, JAMÁS culpa a la paquetería).
- `NotificacionEnviada`: clave_idempotencia (único: f"{evento}:{canal}:{destinatario}"), canal (`whatsapp|email|consola`), destinatario, cuerpo, ts. — **idempotencia de salida: verificar antes de enviar; reenvío = no-op.**
- **Adapter:** `WhatsAppCloudAPI` (POST graph.facebook.com si WHATSAPP_TOKEN) / `ConsolaAdapter` (imprime + guarda) si no.
- **Servicios (contrato):** `enviar_plantilla(clave, pedido, contexto_extra=None)`, `enviar_en_camino(pedido)`, `enviar_confirmacion(pedido)`, `enviar_retraso(pedido, nueva_fecha)`, `notificar_cliente_incidencia(incidencia)`, `enviar_recepcion_cerrada(orden)` (confirmación honesta del ASN cerrado, cifras reales por SKU, idempotente por `REC:{folio}:{canal}:{destinatario}`), `digest_diario()` (17:30, consolidado, por cliente).
- **Command:** `digest_diario`.

### portal (solo vistas/templates; usa servicios de las demás)
URLs obligatorias (namespace `portal`): `dashboard` (Hoy), `pedidos`, `pedido_detalle` (pk), `inventario`, `incidencias`, `incidencia_detalle` (pk) con form de respuesta + botón "marcar urgente", `incidencia_nueva`, `recepciones` (lista ASN + form anunciar), `exportar` (CSV: pedidos, inventario/kardex, incidencias).
- TODO filtrado por `request.cliente` (decorator `portal_requerido`). El detalle valida pertenencia (404 si no es suyo).
- Dashboard "Hoy": banner semáforo del día, embudo de pedidos por estado, inventario crítico (bajo punto_reorden), incidencias abiertas con "quién tiene la pelota", salud de sync (último SyncLog por dirección con frescura "hace X min" — NUNCA la palabra "tiempo real"), widget corte del día (`settings.TORRE["CORTE_CONTRACTUAL"]`).
- Detalle de pedido: pipeline con timestamps, fotos de evidencia, guía + tracking, conversaciones/incidencias ligadas.
- Inventario: tabla físico/en recepción/apartado/cuarentena/disponible + fecha último conteo por SKU + kardex por SKU (movimientos) exportable.

### piso (vistas del operador, tablet)
URLs (`piso`): `home` (tareas del día), `recepciones` + `recepcion_detalle` (recibir líneas, fotos llegada, ubicar), `picking` (olas: pedidos EN_PICKING, escaneo por línea = input código de barras + cantidad), `empaque` + `empaque_pedido` (checklist: insumos del cliente, naked packing si es_local; captura peso; foto de CONTENIDO obligatoria; botón Empacado — en el MISMO POST se encadena `pedidos.services.despachar_a_corral`: guía + impresión de etiquetas best-effort; la foto de caja cerrada con la etiqueta pegada se toma después, vía `cerrar_caja`), `salida` (staging por corral SAL-*, marcar manifiesto → RECOLECTADO en lote; **excluye y avisa** pedidos sin `cajas_cerradas_completas`), `conteos` (tarea de conteo: SKU, contado), `cuarentena` (saldos en cuarentena + dictamen por fila con doble firma vía `inventario.dictaminar_cuarentena`: revendible o merma), `entrega_local` (POD: foto + nombre receptor + verificación mayoría de edad checkbox obligatorio; flujo de flota propia — se esconde con `TORRE["FLOTA_PROPIA"]=False`).
- Corrales: `_corral_de_carrier` mapea paquetexpress→SAL-PQX, "local"→SAL-LOCAL (solo guías/planes "local" viejos), resto→SAL-OTRO. Sin flota propia un pedido es_local se agrupa por su carrier real (estafeta local → SAL-OTRO, paquetexpress local → SAL-PQX).
- UI grande, botones táctiles, flujo lineal. Cada acción llama servicios de dominio, nunca toca modelos directo.

### mesa (dashboard interno + seed)
URLs (`mesa`): `dashboard` (KPIs del día: pedidos por estado, incidencias por SLA con reloj, % salida mismo día, exactitud de inventario, guías sin movimiento), `incidencias` + `incidencia_detalle` (gestión completa: responder al comprador/cliente, notas internas, compensaciones, reclamaciones), `pedidos` (todos los clientes, filtros; por fila en estado cancelable POST `accion=cancelar` con folio + motivo requerido → `pedidos.services.cancelar` + evento `cancelacion_solicitada_mesa`, con flash que explica lo que pasó según el estado), `pedido_nuevo` (alta manual en dos pasos: sin `?cliente=` selector de cliente activo, con `?cliente=<slug>` → `FormPedidoManual` → `pedidos.services.crear_pedido_manual`), `inventario` (stock por cliente vía `inventario.resumen_sku` con fila Total; `?cliente=<slug>&sku=<codigo>` = detalle con saldos por ubicación/estado/lote SOLO lectura, últimos 30 movimientos del kardex y form de AJUSTE con doble PIN → `aplicar_ajuste`; `?ver=ubicaciones` = catálogo de `catalogo.Ubicacion` con alta y activar/desactivar — apagar exige cero saldos vivos), `recepciones` (tablero de ASNs de todos los clientes con reloj SLA + captura de ASN llegada por WhatsApp en dos pasos: `?cliente=<slug>` → `FormAnuncioASNMesa`, evento `anunciada_mesa`), `sync` (SyncLog por tienda/dirección, últimos webhooks, cola de push), `clientes` (lista + ficha con tiendas), `cliente_nuevo` + `cliente_editar` (alta/edición de Cliente con branding aplanado; el slug no se edita — llave de idempotencia del seed), `cliente_tarifario` (editor de overrides: `Cliente.tarifario` guarda SOLO lo que difiere de `TORRE["TARIFARIO_DEFAULT"]`, envio_* anidado en envio_bloque, campo vacío = vuelve al default), `cliente_skus` (catálogo del cliente: alta/edición de SKU + import CSV utf-8-sig con update_or_create por (cliente, codigo)). La ficha `cliente_detalle` gestiona por POST `accion`: usuarios del portal (usuario_nuevo/usuario_reset/usuario_toggle — password generada se muestra UNA vez en el flash, jamás va a la auditoría) y tiendas Shopify (tienda_guardar, alta o edición por tienda_id). Formularios grandes de mesa en `apps/mesa/forms.py` (forms.Form planos, estilo portal).
- **Command `seed_demo`:** crea Cervecería Colima (con 2 tiendas Shopify demo: `colima-mx.myshopify.com` y `colima-usa.myshopify.com`) + 1 segundo cliente demo ("Mezcal Nocturno"), usuarios: `karina` (portal Colima, pass `colima2026`), `mario` (portal Colima), `nocturno` (portal cliente 2 — para probar aislamiento), `piso1` (piso, PIN 1111), `jefe` (piso, PIN 2222), `mesa1` (mesa), `admin` (superuser, pass `admin2026`); SKUs reales de cerveza (Colimita, Páramo, Ticús, Cayaco, Piedra Lisa — six y caja 12/24), ubicaciones del Local 380 E, corrales SAL-*, saldos iniciales, 1 ASN anunciada, ~14 pedidos en distintos estados con timestamps realistas, 3 incidencias (DAN abierta con timeline, RET resuelta, FAL cerrada) con compensación y reclamación de ejemplo, plantillas A/B/E, reglas de envío (Colima→paquetexpress, local→entrega local), conteos y un ajuste con doble firma de ejemplo. Todo con `registrar_evento` para que el kardex/auditoría se vean vivos.

## Estilo de mensajes al usuario (copy)

Español mexicano directo. El portal le habla a Karina sin tecnicismos ("Todo en orden. 14 pedidos en proceso, corte a las 14:00."). El piso da órdenes claras ("Escanea el SKU", "Falta la foto de la caja cerrada"). Errores: qué pasó + qué hacer. Nada de jerga técnica en portal/piso.
