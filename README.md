# Torre — núcleo operativo del 3PL (Local 380 E)

Software completo del 3PL boutique: multi-tenant, multi-tienda Shopify, envíos vía Envia.com (Paquetexpress preferente), incidencias con SLA, mensajería WhatsApp en voz de marca, portal del cliente, app de piso y torre de control — con el estilo gráfico de WOP.partners (negro, Clash Display, rojo #C41E3A).

## Arranque rápido

```bash
cd Fulfilment
./torre-env/bin/python torre/manage.py migrate
./torre-env/bin/python torre/manage.py seed_demo     # datos demo de Colima + Mezcal Nocturno
./torre-env/bin/python torre/manage.py runserver 8380
```

Abrir <http://127.0.0.1:8380>

| Usuario | Password | Qué ve |
|---|---|---|
| `karina` | `colima2026` | Portal de Cervecería Colima (`/portal/`) |
| `mario` | `colima2026` | Portal de Colima (dueño) |
| `nocturno` | `nocturno2026` | Portal de Mezcal Nocturno — prueba que cada cliente SOLO ve lo suyo |
| `piso1` | `piso2026` | App del operador (`/piso/`) · PIN de firma 1111 |
| `jefe` | `jefe2026` | App del operador · PIN 2222 |
| `mesa1` | `mesa2026` | Torre de control (`/mesa/`) · PIN 3333 |
| `admin` | `admin2026` | Django admin (`/admin/`) |

## Arquitectura

- **Django 5 + SQLite en dev** (Postgres vía `TORRE_DB=postgres`), templates + CSS propio (sistema WOP), sin build de frontend.
- **Multi-tenant duro:** `core.Cliente` es el tenant; `TenantMiddleware` cuelga `request.cliente`; toda vista de portal filtra por él (lo ajeno es 404). Un cliente puede tener **N tiendas Shopify** (`integraciones.Tienda`).
- **Parámetros canónicos** en `settings.TORRE` (corte 14:00, SLAs, umbrales, tolerancia de peso ±3%…) — un solo número por promesa, como manda el BLUEPRINT.
- **Event log append-only** (`core.EventoAuditoria`): todo movimiento tiene autor, hora, motivo. El kardex se deriva de `inventario.Movimiento` (también append-only).
- **Apps:** `catalogo` (SKU/ubicaciones/lotes) · `inventario` (saldos por estado, reserva atómica FEFO, ASN con reloj SLA, conteos cíclicos, ajustes con doble PIN) · `pedidos` (máquina de estados canónica de 12 estados; empacar exige 2 fotos + peso ±3%) · `integraciones` (webhooks Shopify idempotentes, push de **on_hand** con cola, SyncLog) · `envios` (adapter **Envia.com** real + mock sin API key; reglas por cliente; poll de tracking que abre incidencias) · `incidencias` (folio INC-AAAA-####, 7 tipos, relojes SLA, compensaciones y reclamaciones a carrier) · `mensajeria` (plantillas A/B/E en voz de marca, idempotencia de envío, WhatsApp Cloud API o consola) · `portal` / `piso` / `mesa` (las tres interfaces).

## Reglas de oro implementadas (anti-Melonn)

- "En camino" se dispara **solo al RECOLECTADO** (manifiesto firmado), jamás al etiquetar.
- El botón **Empacado no existe sin 2 fotos** y peso dentro de tolerancia.
- Naked packing en pedidos locales: el checklist lo marca y avisa "JAMÁS cinta del 3PL".
- Toda incidencia **notifica al cliente al abrirse** — el cliente siempre se entera.
- Ajustes de inventario con **doble firma** (2 PINs de personas distintas).
- Entrega local exige **verificación de mayoría de edad** en el POD (es alcohol).
- Plantilla de retraso pide disculpas en primera persona de marca — **nunca culpa a la paquetería**.

## Integraciones reales (activar con variables de entorno)

```bash
export ENVIA_API_KEY="..."        # tasas preferenciales Envia.com; sin key → MockAdapter
export WHATSAPP_TOKEN="..."       # Cloud API; sin token → consola
export WHATSAPP_PHONE_ID="..."
```

## Jobs (cron en producción; a mano en dev)

```bash
manage.py sync_shopify        # reconciliar pedidos + push de inventario
manage.py push_inventario     # drenar cola de push on_hand
manage.py poll_tracking       # tracking Envia → transiciones + incidencias RET/RF
manage.py conteo_ciclico      # 3 SKUs del día (ABC)
manage.py digest_diario       # resumen 17:30 por cliente
manage.py cerrar_entregas_presuntas
```

## Tests

```bash
./torre-env/bin/python torre/manage.py test apps   # 284 tests
```

## Envíos v2 (agosto 2026): cotización real, división ≤20 kg y rastreo brandeado

- **Cotización real vía Envia.com** (`apps/envios/cotizador.py`): tarifas vivas por lane con caché de 7 días (negativos caducan en 6 h). `ENVIA_MODO`: `cotizar` (default: tarifas reales, guías mock — generar etiquetas cuesta), `full` (guías reales), `off` (todo mock).
- **División de envíos**: tope duro 20 kg por paquete; el optimizador cotiza particiones reales (incluye **reempaque** de cajas de 24 → 2 medias si `SKU.empaques_divisibles=2`) y elige la más barata. Meta **≤$115 por envío**: la cumple PuntoPost ($86–91, ≤10 kg) en zonas metro; donde no hay cobertura el paquete se marca `fuera_de_meta` (visible en Mesa/portal, nunca bloquea).
- **Visible para pickers/packers**: picking y empaque muestran los paquetes del plan, su contenido ("1/2 de Caja 24 — REEMPACADA"), peso, carrier, precio y el ahorro vs envío único.
- **Rastreo público brandeado** (`/r/<token>/`): la página del comprador con la marca del cliente (branding por tenant), timeline humana, tarjeta por paquete, POD, reporte de problemas (abre incidencia) y WhatsApp. Embebible en Shopify: ver `docs/shopify-rastreo.md`.
- **Evidencia de tarifas**: `manage.py probar_tarifas` corre la matriz nacional real y evalúa la meta por envío.
