# HANDOFF — Torre (3PL WOP.partners)
*Para el tech lead que recibe el proyecto · agosto 2026*

Torre es el sistema operativo completo de un fulfillment center boutique en CDMX (cliente ancla: Cervecería de Colima, cerveza en vidrio/lata + merch, ~2,200 pedidos/mes, 2 operadores con celulares Android). Django 5 monolito, **684+ tests**, español en todo (código, UI, commits de dominio). Este documento es tu mapa; el contrato de arquitectura vive en `CONVENTIONS.md` (léelo completo antes de tocar código — las firmas de servicios SON contratos).

## 1. Correr en 5 minutos (dev)
```bash
python3.13 -m venv ../torre-env && ../torre-env/bin/pip install -r requirements.txt
../torre-env/bin/python manage.py migrate
../torre-env/bin/python manage.py seed_demo        # datos demo + usuarios
../torre-env/bin/python manage.py demo_en_vivo     # pedidos frescos de HOY (re-ejecutable)
../torre-env/bin/python manage.py runserver 0.0.0.0:8380
../torre-env/bin/python manage.py test apps        # la suite completa DEBE estar verde
```
Usuarios demo: `mesa1/mesa2026` (Mesa de Control) · `piso1/piso2026` (operador, PIN 1111) · `jefe/jefe2026` (PIN 2222) · `karina/colima2026` (portal cliente) · `admin/admin2026`.

**Ojo con dos cachés en dev**: Django ≥4.1 cachea templates aun con DEBUG (si corres `--noreload`, un cambio de template requiere reiniciar). Y los comentarios `{# #}` de Django son de UNA línea — multilinea fuga texto al HTML (ya nos pasó).

## 2. El mapa (12 apps bajo `apps/`)
- **core** — tenancy (Cliente + TenantMiddleware → `request.cliente/rol`), roles portal/piso/mesa (`@rol_requerido`, `@portal_requerido`), `EventoAuditoria` (append-only DURO, solo vía `registrar_evento`), `EvidenciaFoto` (hash SHA-256), login/tema.
- **catalogo** — SKU (código único POR cliente), Ubicacion (tipos recepcion/picking/reserva/merma/retorno/salida), Lote (FEFO).
- **inventario** — LA única puerta al stock (`services.py`: recibir/ubicar/reservar/confirmar_pick/despachar/ajustar/dictaminar_cuarentena/cerrar_recepcion). Kardex `Movimiento` append-only. RESERVADO es CAPA sobre vendible: `disponible = vendible − reservado − buffer` (romper esto rompe el push a Shopify). Ajustes y dictámenes con DOBLE PIN.
- **pedidos** — máquina de estados canónica (12 estados, `transicionar()` único camino). El carril: `iniciar_picking → confirmar_linea_pick → empacar_caja (peso por caja vs plan CON margen ×1.05) → empacar → despachar_a_corral (guía+impresión AUTO) → cerrar_caja (foto CON etiqueta) → marcar_recolectado` (manifiesto = ÚNICO disparador del "va en camino"). Ingesta Shopify idempotente.
- **envios** — cotizador multi-carrier vía Envia.com (`ENVIA_MODO`: off/cotizar/full — ¡"full" COMPRA guías reales!), división ≤20 kg, planes de carrier ÚNICO, guías, poll_tracking. `TORRE["FLOTA_PROPIA"]=False`: no hay flota propia.
- **incidencias** — folios INC, SLAs con reloj, compensaciones.
- **mensajeria** — WhatsApp Cloud API (sin token → consola), plantillas A/B/E, **Web Push VAPID** (push.py: on_commit, timeout=5, lista blanca anti-SSRF, poda en logout).
- **portal / piso / mesa** — las 3 UIs. Piso es PWA móvil (Mi turno → carril único, escáner de cámara BarcodeDetector en `static/js/escaner.js`, tab bar). Mesa: onboarding completo de clientes sin /admin/ (alta, tarifario, SKUs con CSV, usuarios, tiendas), finanzas, bodega en vivo (plano SVG), manuales.
- **integraciones** — webhooks Shopify (HMAC), push de inventario.
- **rastreo** — páginas públicas brandeadas por cliente: `/r/<token>/` (comprador) y `/r/e/<token>/` (repartidor: QR de la etiqueta, Maps exacto + botón "no encuentro" → aviso a CS del cliente).

Parámetros de negocio: TODOS en `settings.TORRE` (corte 14:00, tolerancia peso ±3%, SLAs, tarifario Modelo A/B, zonas por CP — Metro = GDL/Qro/Puebla; **MTY es nacional**). Nunca números mágicos.

## 3. Deploy a producción (para que la Mac del founder se apague)
Recomendado: **VPS chico (Hetzner/DO, 2 GB) + Postgres + Caddy** — todo scripteado en `deploy/`:
1. VPS Ubuntu: usuario `torre`, repo en `/srv/torre/torre`, venv en `/srv/torre/venv`, `pip install -r requirements.txt gunicorn`.
2. Postgres local: `TORRE_DB=postgres` + PG* en `.env` (el `.env` NO viaja en git — pedirlo por canal seguro; regenerar `SECRET_KEY` y `TORRE_TOKEN_IMPRESION` para prod).
3. `deploy/torre.service` (gunicorn + migrate + collectstatic) → systemd. `deploy/Caddyfile` (HTTPS automático con dominio real) → Caddy. `deploy/crontab.txt` → los 5 jobs.
4. `.env` prod mínimo: `TORRE_DEBUG=0`, `TORRE_ALLOWED_HOSTS=torre.dominio.mx`, `TORRE_HTTPS=1` (activa cookies secure/HSTS), `BASE_URL_PUBLICA=https://torre.dominio.mx`, `ENVIA_API_KEY`, `VAPID_*`, `TORRE_MODO_IMPRESION=relay`, `TORRE_TOKEN_IMPRESION=<random largo>`, WhatsApp cuando toque.
5. Shopify: apuntar el webhook de la tienda a `https://torre.dominio.mx/hooks/shopify/<tienda_id>/` (el token de la tienda firma el HMAC).
6. **Con HTTPS real muere el flag de Chrome** de los celulares (runbook): la PWA, la cámara y el push funcionan directo. Los operadores reinstalan la PWA desde el dominio nuevo y re-activan notificaciones.
7. Datos: exportar de la SQLite de dev solo lo que valga (clientes/SKUs se recrean con seed o con el onboarding de Mesa + CSV; los pedidos demo NO migran).

**La impresora térmica** (Aiyin BY-480BT, USB en bodega): con `TORRE_MODO_IMPRESION=relay`, las etiquetas se ENCOLAN y `deploy/agente_impresion.py` (stdlib puro, docstring con launchd/systemd) corre en cualquier máquina de la bodega con la impresora: polling autenticado → `lp` local → confirma. En la máquina de bodega: driver CUPS instalado (`lpstat -p` da la cola) + `TORRE_URL`, `TORRE_TOKEN_IMPRESION`, `IMPRESORA` en el ambiente del agente.

## 4. Estado del negocio en el sistema (contexto que no está en el código)
- Cliente ancla factura por **Modelo A** (tarifario default): almacenaje $18k, alist $25, empaque $65, envío POR PEDIDO por bloque de 20 kg según CP destino (L$129/M$169/N$219). Modelo B (clientes nuevos) se activa por cliente en Mesa → tarifario (recepción $190/tarima, mínimo $12k).
- Carriers negociados vía Envia: Estafeta local $100 c/IVA (1-20 kg), nacional $206 c/IVA, 2 recolecciones diarias. PuntoPost $86-91 ≤10 kg en zonas metro.
- Dashboard de márgenes: Mesa → Finanzas. Reglas de facturación en `apps/mesa/finanzas.py` (cancelados no facturan; reexpediciones cuestan pero no cobran).

## 5. Deuda conocida y pendientes (honestos)
1. **Manuales SOP-05/06/07 describen el flujo VIEJO de piso** (pre-carril): actualizar los .md en `manuales/` y correr `manage.py render_manuales`.
2. **Push de incidencias** se dispara dentro de la transacción del caller (acotado por timeout=5; moverlo a `on_commit` como los demás).
3. `ENVIA_MODO=cotizar` en dev: guías mock. Producción real → `full` (cada guía CUESTA dinero — probar con 1 antes).
4. La página pública del repartidor y el rastreo comparten throttle simple por IP vía cache local (con multi-worker considerar cache compartida — Redis — si crece).
5. SQLite → Postgres: los servicios usan `select_for_update` (no-op en SQLite, real en Postgres) — la suite corre en ambos, pero prod DEBE ser Postgres.
6. Piso `entrega_local`: flujo apagado por `FLOTA_PROPIA=False`; el código queda por si algún día hay flota.
7. Los tests de JS no existen (los tests de Django no ejecutan JS): cambios a `escaner.js`/`push.js`/`sw.js` se prueban a mano en un Android real. Historia: un `defer` mal puesto mató el escáner y la suite quedó verde — desconfía.

## 6. Los comandos que importan
```bash
manage.py test apps            # 684+ tests — verde SIEMPRE antes de entregar
manage.py seed_demo            # entorno demo completo (idempotente)
manage.py demo_en_vivo         # refresca el demo con pedidos de HOY
manage.py render_manuales      # re-publica los SOPs (fuente: manuales/*.md)
manage.py probar_push          # push de prueba a los celulares suscritos
manage.py poll_tracking        # tracking de guías (cron)
manage.py conteo_ciclico       # tareas de conteo del día (cron)
manage.py digest_diario        # digest WhatsApp a clientes (cron)
manage.py cerrar_entregas_presuntas  # (cron)
```

## 7. Secretos (NO están en git)
`torre/.env` (pedir por canal seguro): ENVIA_API_KEY, VAPID_PUBLIC_KEY/VAPID_PRIVATE_PEM (+ el archivo `vapid_private.pem`, chmod 600), TORRE_IMPRESORA_ETIQUETAS / TORRE_TOKEN_IMPRESION, WHATSAPP_TOKEN/WHATSAPP_PHONE_ID (cuando se conecte), y en prod: TORRE_SECRET_KEY, PG*. Regenerar llaves VAPID solo si se comprometieron (obliga a re-suscribir todos los celulares).
