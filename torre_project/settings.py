"""
Torre — núcleo operativo del 3PL boutique (Local 380 E).
Desarrollo: Postgres vía docker-compose (o SQLite sin TORRE_DB) + jobs
síncronos vía management commands. Producción: Postgres + gunicorn + cron —
sin broker ni workers: la base de datos es la única cola.
"""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# .env simple (KEY=VALUE por línea); las variables ya exportadas ganan.
_env = BASE_DIR / ".env"
if _env.exists():
    for _linea in _env.read_text().splitlines():
        _linea = _linea.strip()
        if _linea and not _linea.startswith("#") and "=" in _linea:
            _k, _, _v = _linea.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

SECRET_KEY = os.environ.get("TORRE_SECRET_KEY", "dev-only-insecure-key-cambiar-en-prod")
DEBUG = os.environ.get("TORRE_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("TORRE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# ── Producción tras HTTPS (Caddy/nginx): TORRE_HTTPS=1 en el .env del VPS ──
# Con esto las cookies solo viajan cifradas y el navegador fija HTTPS (HSTS).
# En dev (http://) queda apagado y nada cambia.
if os.environ.get("TORRE_HTTPS", "0") == "1":
    # Fail-fast: con la llave dev pública en el repo, cualquiera forja
    # sesiones/CSRF de cualquier usuario. Mejor no arrancar que arrancar así.
    if SECRET_KEY == "dev-only-insecure-key-cambiar-en-prod":
        raise RuntimeError(
            "TORRE_HTTPS=1 con la SECRET_KEY de desarrollo: define "
            "TORRE_SECRET_KEY en el .env antes de servir producción."
        )
    if DEBUG:
        raise RuntimeError(
            "TORRE_HTTPS=1 con DEBUG activo: pon TORRE_DEBUG=0 en el .env "
            "(DEBUG en producción expone settings y tracebacks completos)."
        )
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    CSRF_TRUSTED_ORIGINS = [
        f"https://{h.strip()}" for h in ALLOWED_HOSTS if h.strip() and h.strip() != "*"
    ]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Torre
    "apps.core",
    "apps.catalogo",
    "apps.inventario",
    "apps.integraciones",
    "apps.pedidos",
    "apps.envios",
    "apps.incidencias",
    "apps.mensajeria",
    "apps.portal",
    "apps.piso",
    "apps.mesa",
    "apps.rastreo",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.TenantMiddleware",
]

ROOT_URLCONF = "torre_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.torre",
            ],
        },
    },
]

WSGI_APPLICATION = "torre_project.wsgi.application"

_torre_db = os.environ.get("TORRE_DB", "").strip().lower()
if _torre_db == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "torre"),
            "USER": os.environ.get("PGUSER", "torre"),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }
elif _torre_db in ("", "sqlite"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "torre.sqlite3",
        }
    }
else:
    # Un typo ("postgresql", "Postgres"…) caía en silencio a SQLite: en un VPS
    # eso es arrancar contra un archivo vacío creyendo que hay Postgres.
    raise RuntimeError(
        f"TORRE_DB desconocido: '{_torre_db}'. Usa 'postgres', 'sqlite' o "
        "deja la variable vacía (SQLite)."
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "es-mx"
TIME_ZONE = "America/Mexico_City"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "core:login"
LOGIN_REDIRECT_URL = "core:post_login"
LOGOUT_REDIRECT_URL = "core:login"

# ── Parámetros canónicos (Constitución Operativa, BLUEPRINT.md §1) ──
# Un solo número por promesa: contrato, portal, WMS y plantillas leen ESTAS variables.
TORRE = {
    "CORTE_CONTRACTUAL": os.environ.get("TORRE_CORTE", "14:00"),  # → 15:30 por adéndum
    "HORARIO_HABIL": {"lv": ("09:00", "18:00"), "sab": ("09:00", "13:00")},
    "SLA_PRIMERA_RESPUESTA_COMPRADOR_MIN": 30,
    "SLA_PRIMERA_RESPUESTA_CLIENTE_HORAS": 2,
    "SLA_RESOLUCION_HORAS": 48,
    "SLA_RECEPCION_HORAS_INTERNO": 4,
    "SLA_RECEPCION_HORAS_CONTRACTUAL": 8,
    "UMBRAL_DISCREPANCIA_MXN": 500,
    "UMBRAL_DISCREPANCIA_UNIDADES": 12,
    "TOLERANCIA_PESO_PCT": 3.0,
    "BUFFER_COLA_BAJA": 1,
    "UMBRAL_COLA_BAJA": 5,
    "COBERTURA_ESTANDAR_MXN": 2500,
    "COBERTURA_AMPLIADA_PCT": 1.2,
    "SIN_MOVIMIENTO_LOCAL_HORAS": 24,
    "SIN_MOVIMIENTO_FORANEO_HORAS": 72,
    # Bodega real: Av. Torres de Ixtapantongo 380 Local E, Olivar de los Padres,
    # Álvaro Obregón, CDMX, CP 01780. Entrega local propia: $100 flat por paquete
    # ≤20 kg, cobertura CDMX (00-16) + metropolitano EdoMex/Toluca (50-57).
    "CP_LOCAL_PREFIJOS": [f"{i:02d}" for i in range(0, 17)]
    + [f"{i}" for i in range(50, 58)],
    "TARIFA_LOCAL_MXN": 100,
    # Flota propia de entrega local: HOY NO EXISTE (todo sale por carrier).
    # False = elegir_carrier jamás regresa "local" (los es_local viajan con su
    # carrier real y caen al corral de ese carrier) y C2 esconde SAL-LOCAL y
    # las vistas de entrega_local. Las guías "local" ya emitidas conservan su
    # corral SAL-LOCAL para no romper datos viejos.
    "FLOTA_PROPIA": False,
    # Zona metro (facturación de envío): GDL 44-45, MTY 64-67, Puebla 72, Qro 76.
    # La zona sale del CP DE DESTINO, nunca del carrier que eligió el ruteo.
    # Metro = GDL, Puebla, Qro. Monterrey se factura NACIONAL (decisión 5-ago-2026).
    "CP_METRO_PREFIJOS": ["44", "45", "72", "76"],
    # ── Import de catálogo por CSV (Mesa → catálogo del cliente) ──
    "IMPORT_CSV_MAX_MB": 2,
    "IMPORT_CSV_MAX_FILAS": 5000,
    # ── Envíos: división y meta de tarifa (regla Colima, ago 2026) ──
    "MAX_PESO_ENVIO_KG": 20,  # tope duro por paquete; arriba de esto SIEMPRE se divide
    "TARIFA_OBJETIVO_MXN": 115,  # meta nacional por envío; si la mejor opción la excede, se marca fuera de meta
    # Lista blanca Y exclusión (2026-08-19: fuera iMile/AmPm/puntopost); gana el más barato.
    # noventa9Minutos no tiene "ground": jamás usarlo de carrier_preferente.
    "CARRIERS_COTIZAR": [
        "estafeta",
        "paquetexpress",
        "fedex",
    ],
    # Carrier → proveedor que lo opera. Vacío = todo por envia.com; el flip a
    # 99minutos directo es config, no código: {"noventa9Minutos": "99minutos"}.
    "PROVEEDOR_POR_CARRIER": {},
    # Estado del ORIGEN por carrier (envia): cada conector traduce el estado
    # con su propia tabla. Probado en vivo 2026-08-29: estafeta SOLO genera
    # con el 2-letras "CX" (DF → 1129 "State code not founded"); fedex y
    # 99min-vía-envia aceptan el code_shopify "DF" (default). Candidato
    # pendiente de probe: paquetexpress (sus 424 huelen a este mismo bug).
    "ORIGEN_ESTADO_POR_CARRIER": {"estafeta": "CX"},
    # Carriers de envia que aceptan recolección PROGRAMADA (POST /ship/pickup/).
    # El botón de Salida solo aparece para estos; noventa9Minutos no va aquí
    # (su pickup es nativo: pickUpAfter en el create). Descubrimiento fino vía
    # API de envia: pendiente (mapa manual mientras).
    "CARRIERS_PICKUP": {"fedex": True, "estafeta": True, "paquetexpress": True, "dhl": True},
    # Primer sync de una tienda: solo pedidos pagados + sin fulfillear de esta
    # ventana (acuerdo con el founder). El sync recurrente no se acota.
    "BACKFILL_DIAS": 90,
    "COTIZACION_CACHE_DIAS": 7,  # vigencia del caché de cotizaciones por (CP, peso)
    # ── Finanzas: tarifario default y costos (dashboard Mesa → Finanzas) ──
    # Modelo A (Colima): calibrado sobre junio-2026 real para dar ~9% de ahorro
    # vs la factura de Melonn ($408,774 sin IVA). El envío se factura POR PEDIDO
    # por cada bloque de `bloque_kg`, NUNCA por guía: así el ruteo interno
    # (dividir/consolidar paquetes) no mueve la factura del cliente.
    "TARIFARIO_DEFAULT": {
        "almacenaje_mes": 18000,
        "alistamiento_pedido": 25,
        "empaque_pedido": 65,
        "bloque_kg": 20,
        "envio_bloque": {"local": 129, "metro": 169, "nacional": 219},
        # Modelo B (clientes generales): recepción $/tarima y piso de factura
        # mensual. Default 0 = Modelo A (Colima) no cobra recepción ni tiene
        # mínimo; los Modelo B lo activan vía el editor de tarifario de Mesa
        # (típico: 190 y 12000).
        "recepcion_tarima": 0,
        "minimo_mes": 0,
    },
    "COSTOS_FIJOS_MES_MXN": 101202,  # renta 60,413 + colaboración 6,289 + sueldos 30,000 + servicios 4,500
    "INSUMO_PAQUETE_MXN": 12,  # relleno/esquineros/cinta por bulto físico
    "BENCHMARK_PEDIDO_MXN": 357,  # Melonn jun-2026: $408,774 / ~1,146 pedidos despachados
    "META_PROFIT_MES_MXN": 100000,
    # ── Relay de impresión: fallas del agente antes de marcar ERROR ──
    "IMPRESION_MAX_INTENTOS": 5,
}

# ── Relay de impresión (Torre en VPS, térmica en bodega) ──
# "local" = lp directo en esta máquina, como siempre. "relay" = imprimir_etiqueta
# encola TrabajoImpresion y el agente de bodega (deploy/agente_impresion.py) baja
# el PDF por la API /api/impresion/ y lo imprime con su CUPS local.
TORRE_MODO_IMPRESION = os.environ.get("TORRE_MODO_IMPRESION", "local")
# Token estático del agente (Authorization: Bearer). Vacío = API cerrada (403).
TORRE_TOKEN_IMPRESION = os.environ.get("TORRE_TOKEN_IMPRESION", "")

# ── Integraciones ──
ENVIA_API_KEY = os.environ.get("ENVIA_API_KEY", "")  # sin key → modo mock
# Modo Envia: "off" = todo mock · "cotizar" = tarifas reales, guías mock (default
# seguro: cotizar no cuesta; generar etiquetas sí) · "full" = guías reales.
ENVIA_MODO = os.environ.get("ENVIA_MODO", "cotizar")
import sys  # noqa: E402

if "test" in sys.argv:
    ENVIA_MODO = "off"  # los tests jamás tocan la API real
ENVIA_API_BASE = os.environ.get("ENVIA_API_BASE", "https://api.envia.com")
ENVIA_QUERIES_BASE = os.environ.get("ENVIA_QUERIES_BASE", "https://queries.envia.com")
# 99minutos directo (proveedor "99minutos"). Sin key o modo != "full" → nada
# real; sandbox: NOVENTA9_API_BASE=https://sandbox.99minutos.com.
NOVENTA9_API_KEY = os.environ.get("NOVENTA9_API_KEY", "")  # client_id:client_secret
NOVENTA9_API_BASE = os.environ.get("NOVENTA9_API_BASE", "https://delivery.99minutos.com")
NOVENTA9_MODO = os.environ.get("NOVENTA9_MODO", "off")
if "test" in sys.argv:
    NOVENTA9_MODO = "off"
# Fallback runtime: si el directo de 99minutos falla, re-cotizar/re-generar ese
# carrier por envia (tarifa de envia, auditado con evento). Default apagado.
NOVENTA9_FALLBACK_ENVIA = os.environ.get("NOVENTA9_FALLBACK_ENVIA", "0") == "1"
# Check de báscula en empaque: "bloquear" | "avisar" | "off". Off por default:
# el peso esperado suma solo productos (la tara de la caja aún no existe en el
# sistema) y bloquear/avisar gritaría en cada empaque legítimo. La escalera:
# off hoy → avisar cuando el catálogo de cajas aporte la tara → bloquear
# cuando los pesos del catálogo sean reales. Cada salto es un flip de .env.
TORRE_PESO_MODO = os.environ.get("TORRE_PESO_MODO", "off")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")  # sin token → consola
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-01")

# ── PWA + Web Push (VAPID self-hosted; llaves en torre/.env) ──
# Sin las 3 variables (o sin el .pem) el push es no-op silencioso: la
# operación jamás depende de que las notificaciones estén configuradas.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_PEM = os.environ.get(
    "VAPID_PRIVATE_PEM", ""
)  # ruta al .pem, relativa a BASE_DIR
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "")
import mimetypes  # noqa: E402

mimetypes.add_type("application/manifest+json", ".webmanifest")

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# ── Evidencia en Spaces (S3) ─────────────────────────────────────────────────
# Con credenciales: las fotos de evidencia viven en el bucket privado (el disco
# del droplet deja de ser su única copia). Sin credenciales: disco local (dev).
# La vista autorizada (core:evidencia) no cambia — .open() streamea desde el
# bucket a través de Django; el bucket JAMÁS se expone directo (CDN off,
# listing restricted).
SPACES_KEY = os.environ.get("SPACES_KEY", "")
SPACES_SECRET = os.environ.get("SPACES_SECRET", "")
SPACES_BUCKET = os.environ.get("SPACES_BUCKET", "torre-evidencia")
SPACES_ENDPOINT = os.environ.get(
    "SPACES_ENDPOINT", "https://nyc3.digitaloceanspaces.com"
)
SPACES_REGION = os.environ.get("SPACES_REGION", "nyc3")
if SPACES_KEY and SPACES_SECRET:
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "access_key": SPACES_KEY,
                "secret_key": SPACES_SECRET,
                "bucket_name": SPACES_BUCKET,
                "endpoint_url": SPACES_ENDPOINT,
                "region_name": SPACES_REGION,
                "default_acl": "private",
                "file_overwrite": False,  # dos fotos con el mismo nombre jamás se pisan
                "querystring_auth": True,
            },
        },
        # El default de Django para estáticos se conserva (whitenoise es middleware).
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

# ── Logging ──────────────────────────────────────────────────────────────────
# Sin esto, con DEBUG=0 el default de Django manda los 500 a un handler
# filtrado por require_debug_true y a mail_admins (sin ADMINS configurado):
# un traceback de producción moría SIN DEJAR RASTRO. Aquí siempre sale a
# stderr → gunicorn → journald. Los módulos loguean con
# logging.getLogger("torre.<modulo>").
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "torre": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "consola": {"class": "logging.StreamHandler", "formatter": "torre"},
    },
    "root": {"handlers": ["consola"], "level": "WARNING"},
    "loggers": {
        "django.request": {
            "handlers": ["consola"],
            "level": "ERROR",
            "propagate": False,
        },
        "torre": {"handlers": ["consola"], "level": "INFO", "propagate": False},
    },
}
