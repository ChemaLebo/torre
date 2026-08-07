# Contrato fase Envíos v2 — cotización real, división ≤20 kg y rastreo brandeado

Complementa CONVENTIONS.md (que sigue vigente). Tres módulos se construyen en paralelo contra ESTE contrato.

## Datos reales (medidos contra la API de Envia.com el 2026-08-03, cuenta con tasas preferenciales)

Tarifario nacional real desde Colima (CP 28017), mejor servicio por carrier, MXN con IVA:

| Peso | puntopost | estafeta | paquetexpress | fedex |
|---|---|---|---|---|
| 4 kg | **$86** | $149 | $194 | $164–207 |
| 8 kg | **$91** | $177 | $228 | $229 |
| 12 kg | no cotiza | $201 | $260 | $248 |
| 16 kg | no cotiza | $224 | $291 | $297 |
| 20 kg | no cotiza | $247 | $323 | $331 |
| 25 kg | no cotiza | $278 | $362 | $376 |

- **puntopost** (servicio "Send C2C", 5–7 días): el ÚNICO ≤$115. Cobertura parcial: CDMX/GDL/MTY/Veracruz sí; **Mérida y Tijuana NO** ("No coverage"). No cotiza >~10 kg.
- **estafeta**: tarifa plana nacional, siempre cotiza, 2–3 días. Es el respaldo universal.
- Conclusión de negocio: dividir 16 kg en 2×8 kg puntopost = $182 vs $224 directo (ahorra $42). Dividir 12 kg en 8+4 = $177 vs $201. Pero 20 kg conviene entero por estafeta ($247) si puntopost no alcanza. **La decisión SIEMPRE sale de cotizar particiones reales y comparar totales — nunca de reglas fijas.**

## Parámetros canónicos (ya en settings.TORRE — NO duplicar números)

`MAX_PESO_ENVIO_KG=20` (tope duro por paquete) · `TARIFA_OBJETIVO_MXN=115` (meta; si la mejor opción la excede se marca `fuera_de_meta=True`, jamás se bloquea el envío) · `CARRIERS_COTIZAR` · `COTIZACION_CACHE_DIAS=7`.

## Modelos nuevos (dueño: envios)

- `Paquete`: pedido FK ("pedidos.Pedido", related_name="paquetes"), numero (1..N), peso_kg (Decimal), largo_cm/ancho_cm/alto_cm (int), carrier, servicio, precio_cotizado (Decimal), fuera_de_meta (bool), estado (`PLANEADO→EN_EMPAQUE→EMPACADO→DESPACHADO`), guia FK null ("envios.Guia"), creado. `unique_together (pedido, numero)`.
- `PaqueteLinea`: paquete FK, linea_pedido FK ("pedidos.LineaPedido"), cantidad. — qué SKUs van en qué caja: esto es lo que ven pickers/packers.
- `CotizacionCache`: cp_destino, peso_kg (Decimal redondeado a 0.5), carrier, servicio, precio, estimado_entrega (str), ok (bool — False = ese carrier no cotiza ese lane/peso), ts. Vigencia `COTIZACION_CACHE_DIAS`.
- `Guia` gana FK opcional `paquete` (null=True) — una guía por paquete; las guías legacy sin paquete siguen válidas.

## Servicios nuevos (dueño: envios) — firmas contrato

- `cotizar_lane(cp_destino, peso_kg, dims) -> list[dict(carrier, servicio, precio, estimado, ok)]` — consulta CotizacionCache vigente primero; si no, pega a la API real de Envia (POST /ship/rate/ por carrier de CARRIERS_COTIZAR, en paralelo con ThreadPoolExecutor) y cachea TODO resultado, incluidos los "no cotiza" (ok=False). Sin ENVIA_API_KEY → tarifario mock basado en la tabla de arriba (para dev/tests).
- `mejor_opcion(cp_destino, peso_kg, dims) -> dict | None` — la más barata con ok=True.
- `planificar_envio(pedido) -> list[Paquete]` — EL CEREBRO:
  1. Peso total = Σ línea.cantidad × sku.peso_gr/1000 (+5% de empaque). Dims desde el catálogo.
  2. Genera particiones candidatas de las LÍNEAS (no se parte una botella): (a) todo junto si ≤ MAX_PESO_ENVIO_KG; (b) greedy en chunks ≤9 kg (aprovecha puntopost); (c) mitades balanceadas; (d) chunks ≤ MAX_PESO_ENVIO_KG mínimos. Descarta toda partición con un paquete > MAX_PESO_ENVIO_KG.
  3. Cotiza cada partición con **UN SOLO carrier para todo el plan** (el carrier más barato que cotice TODOS los paquetes de la partición; Σ por paquete) y elige el TOTAL más barato; empates → menos paquetes. Regla operativa: el manifiesto de salida se firma por corral (= por carrier), así que un plan multi-carrier partiría el pedido entre corrales y una caja saldría sin manifiesto — por eso jamás se mezclan carriers dentro de un plan.
  4. Persiste Paquetes + PaqueteLineas con carrier/servicio/precio elegidos y fuera_de_meta por paquete (precio > TARIFA_OBJETIVO_MXN). Auditar con registrar_evento (accion="plan_envio", delta={particiones evaluadas, ahorro vs envío único}).
  5. Idempotente: si el pedido ya tiene paquetes no despachados, re-planifica solo si se llama con force=True; si no, regresa los existentes.
- `generar_guia(pedido)` pasa a generar **una guía POR PAQUETE** (usa el carrier/servicio ya elegidos del paquete; conserva compatibilidad: pedidos sin paquetes → planificar primero).
- Hook: `pedidos.services.ingerir_pedido_shopify` llama `planificar_envio` lazy tras reservar (try/ImportError safe). El poller de tracking opera por guía como hoy.

## Visibilidad piso/portal (dueños: piso y portal — NO tocan envios)

- **Piso picking**: el detalle muestra la división: "📦 Paquete 1 de 2 — 7.9 kg — PuntoPost $91" con sus líneas; el picker surte por paquete.
- **Piso empaque**: se empaca POR PAQUETE: selector de paquete, checklist e ingreso de peso POR paquete (tolerancia ±3% contra paquete.peso_kg), 2 fotos POR paquete (EvidenciaFoto entidad="paquete", entidad_id=paquete.id), y una franja visible con el POR QUÉ: "Este pedido va dividido: ahorra $42 vs un solo envío" (dato del plan). El pedido pasa a EMPACADO cuando TODOS los paquetes están empacados (los servicios de pedidos ya existentes se invocan al completar el último).
- **Piso salida**: staging y manifiesto por guía/paquete (un pedido puede tener 2 guías del mismo carrier o carriers distintos).
- **Portal pedido_detalle**: sección "Paquetes de tu pedido": N tarjetas con contenido, guía, estado de tracking y link de rastreo público por paquete.

## Página pública de rastreo brandeada (dueño: rastreo, app nueva ya registrada en `/r/`)

- `AccesoRastreo`: pedido OneToOne ("pedidos.Pedido"), token (12 chars urlsafe, único, generado con secrets), creado. Servicio `obtener_o_crear_token(pedido) -> str` y `url_publica(pedido) -> str` (usa settings BASE_URL_PUBLICA env con default http://127.0.0.1:8380).
- Vistas SIN login (throttle suave por IP con cache de Django; no exponer datos sensibles: solo nombre de pila del comprador, NUNCA dirección completa ni teléfono ni precios):
  - `GET /r/<token>/` — página brandeada del cliente (branding = `pedido.cliente.branding` con defaults elegantes): logo/nombre, timeline del pedido (estados en lenguaje humano con timestamps), tarjeta POR PAQUETE (contenido resumido, guía, carrier, estado, estimado), foto POD si entregada, botón "Reportar un problema" y botón WhatsApp (branding.whatsapp_soporte).
  - `POST /r/<token>/reporte/` — form (tipo: llegó dañado / no ha llegado / llegó incompleto / otro + texto + foto opcional) → `incidencias.services.abrir_incidencia(origen="comprador")` lazy → página de confirmación "ya avisamos a {marca}, te contactamos en ≤30 min hábiles" (settings.TORRE). Anti-spam: 3 reportes máx por token.
  - `GET /r/<token>/?embed=1` — misma página sin chrome propio (para incrustar en Shopify).
- **Estilo**: NO usa base.html de Torre (es la cara del CLIENTE, no de WOP): template propio `rastreo/base_publica.html`, mobile-first, limpio, con CSS inline/propio que toma colores de branding (default Colima: crema #F5EFE0, rojo profundo #9E2B25, tipografía serif elegante para el nombre de marca — configurable por branding JSON). Sin la palabra "Torre" ni el 3PL a la vista: la marca es del cliente.
- **Shopify**: archivo `docs/shopify-rastreo.md` con: (a) página Liquid lista para pegar (formulario "número de pedido + código" NO — el link con token llega por WhatsApp/email; la página Shopify solo incrusta `<iframe>` o redirige), (b) instrucciones de App Proxy (apps/rastreo → BASE_URL_PUBLICA/r/) para servirla bajo el dominio de la tienda, (c) cómo agregar el link a la plantilla de email de confirmación de envío de Shopify.
- `mensajeria`: la plantilla B y el digest usan `rastreo.services.url_publica(pedido)` como link (lazy, con fallback al link de guía actual si rastreo no está poblado). ESTE cambio en mensajeria lo hace el agente de rastreo (único autorizado a tocar `apps/mensajeria/services.py` en esta fase, cambio quirúrgico).

## Seed y pruebas

- El agente de envios extiende `seed_demo`… NO: seed lo toca el integrador al final. Los agentes escriben tests con el adapter mock (tabla de tarifas de arriba): partición 16 kg → 2×8 (ahorro $42), 12 kg → 8+4, 20 kg → entero estafeta, 25 kg → división obligatoria aunque no ahorre, lane sin puntopost (Mérida 97000) → estafeta y fuera_de_meta=True, cache hit no repega a la API, token de rastreo no enumera pedidos ajenos, reporte abre incidencia origen comprador.
