"""seed_demo — datos demo completos de Torre (contrato CONVENTIONS.md §mesa).

Crea Cervecería Colima (2 tiendas Shopify demo) + Mezcal Nocturno (segundo
tenant para probar aislamiento), usuarios de todos los roles, catálogo real de
cerveza, saldos con kardex vivo (ASN recibida y ubicada vía servicios), una ASN
anunciada, ~16 pedidos en todos los estados con timestamps escalonados,
3 incidencias con timeline/compensación/reclamación, plantillas A/B/E, reglas
de envío, conteos y un ajuste con doble firma.

Idempotente: correrlo dos veces no duplica nada (get_or_create + guardas por
entidad). Usa los servicios de dominio donde es natural para que el kardex y
la auditoría queden poblados de verdad.

Usuarios/passwords documentados (también se imprimen al final):
    admin/admin2026 · karina/colima2026 · mario/colima2026 ·
    nocturno/nocturno2026 · piso1/piso2026 (PIN 1111) ·
    jefe/jefe2026 (PIN 2222) · mesa1/mesa2026 (PIN 3333)
"""
import base64
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# PNG 1×1 válido: evidencia demo sin depender de archivos externos.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# (username, password, rol, cliente_slug, pin, nombre, superuser)
USUARIOS = [
    ("admin", "admin2026", None, None, "", "Administración Torre", True),
    ("karina", "colima2026", "portal", "colima", "", "Karina Fuentes", False),
    ("mario", "colima2026", "portal", "colima", "", "Mario Sandoval", False),
    ("nocturno", "nocturno2026", "portal", "mezcal-nocturno", "", "Aurelio Cruz", False),
    ("piso1", "piso2026", "piso", None, "1111", "Poncho Ledezma", False),
    ("jefe", "jefe2026", "piso", None, "2222", "Chuy Alcaraz", False),
    ("mesa1", "mesa2026", "mesa", None, "3333", "Diana Villalpando", False),
]

# (codigo, descripcion, peso_gr, largo, ancho, alto, precio, punto_reorden, codigo_barras)
SKUS_COLIMA = [
    ("COLIMITA-SIX", "Colimita Lager · six pack 355 ml", 2850, 28, 19, 13, "189.00", 20, "7500465010011"),
    ("COLIMITA-C24", "Colimita Lager · caja 24 botellas", 11400, 40, 27, 25, "640.00", 8, "7500465010028"),
    ("PARAMO-SIX", "Páramo Pale Ale · six pack 355 ml", 2850, 28, 19, 13, "205.00", 15, "7500465010035"),
    ("PARAMO-C12", "Páramo Pale Ale · caja 12 botellas", 5700, 40, 27, 13, "390.00", 8, "7500465010042"),
    ("TICUS-SIX", "Ticús Porter · six pack 355 ml", 2850, 28, 19, 13, "215.00", 12, "7500465010059"),
    ("TICUS-C12", "Ticús Porter · caja 12 botellas", 5700, 40, 27, 13, "410.00", 6, "7500465010066"),
    ("CAYACO-SIX", "Cayaco Cerveza de Colima · six pack 355 ml", 2850, 28, 19, 13, "175.00", 10, "7500465010073"),
    ("PIEDRA-LISA-SIX", "Piedra Lisa Session IPA · six pack 355 ml", 2850, 28, 19, 13, "199.00", 12, "7500465010080"),
]
SKUS_NOCTURNO = [
    ("MN-JOVEN-750", "Mezcal Nocturno Joven · botella 750 ml", 1350, 33, 9, 9, "520.00", 6, "7501031310017"),
    ("MN-REP-750", "Mezcal Nocturno Reposado · botella 750 ml", 1380, 33, 9, 9, "690.00", 4, "7501031310024"),
]

# (codigo, tipo, carriers) — Local 380 E; carriers solo aplica a corrales
UBICACIONES = [
    ("REC-01", "recepcion", ""),
    ("A-01-1", "picking", ""), ("A-01-2", "picking", ""), ("A-02-1", "picking", ""),
    ("A-02-2", "picking", ""), ("A-03-1", "picking", ""),
    ("B-01-1", "picking", ""), ("B-01-2", "picking", ""), ("B-02-1", "picking", ""),
    ("B-02-2", "picking", ""),
    ("RES-01", "reserva", ""), ("RES-02", "reserva", ""),
    ("MER-01", "merma", ""), ("RET-01", "retorno", ""),
    ("SAL-PQX", "salida", "paquetexpress"), ("SAL-99MIN", "salida", "noventa9Minutos"),
    ("SAL-LOCAL", "salida", "local"), ("SAL-OTRO", "salida", ""),
]

# Stock inicial: sku → (cantidad_ok, cantidad_danada, lote, [(ubicacion, cantidad), ...])
STOCK_COLIMA = {
    "COLIMITA-SIX": (80, 0, "LC-2601", [("A-01-1", 60), ("RES-01", 20)]),
    "COLIMITA-C24": (30, 0, "LC-2602", [("B-01-1", 30)]),
    "PARAMO-SIX": (60, 0, "LP-2603", [("A-01-2", 60)]),
    "PARAMO-C12": (24, 0, "LP-2604", [("B-01-2", 24)]),
    "TICUS-SIX": (46, 2, "LT-2605", [("A-02-1", 46)]),
    "TICUS-C12": (18, 0, "LT-2606", [("B-02-1", 18)]),
    "CAYACO-SIX": (40, 0, "LCA-2607", [("A-02-2", 40)]),
    "PIEDRA-LISA-SIX": (36, 0, "LPL-2608", [("A-03-1", 36)]),
}
STOCK_NOCTURNO = {
    "MN-JOVEN-750": (24, 0, "MNJ-2601", [("B-02-2", 24)]),
    "MN-REP-750": (12, 0, "MNR-2601", [("RES-02", 12)]),
}

PLANTILLAS_COLIMA = {
    "A": (
        "¡Salud, {nombre}! Somos el equipo de entregas de {marca}. "
        "Tu pedido {folio} ya está en nuestras manos y lo estamos preparando con el mismo "
        "cuidado con el que se embotella en Colima. Nuestro corte del día es a las {corte}, "
        "así que tu pedido sale de bodega el {fecha}. Te avisamos por aquí en cuanto vaya en camino."
    ),
    "B": (
        "¡Ya va en camino, {nombre}! Tu pedido {folio} de {marca} acaba de salir de nuestra "
        "bodega. Tu guía es {guia} y puedes seguirlo paso a paso aquí: {link_rastreo} "
        "Si necesitas algo, respóndenos por este chat: del otro lado hay una persona real."
    ),
    "E": (
        "Hola {nombre}, te escribimos de {marca}. Tu pedido {folio} está tardando más de lo "
        "que nos gustaría y te pedimos una disculpa. Ya lo estamos siguiendo de cerca; la "
        "nueva fecha estimada de entrega es el {fecha}. Gracias por la paciencia: la primera "
        "ronda va por nosotros."
    ),
}


class Command(BaseCommand):
    help = "Puebla la base con el demo completo de Torre (Cervecería Colima + Mezcal Nocturno). Idempotente."

    # ────────────────────────────────────────────────────────────────────
    # Helpers de tiempo y backdating
    # ────────────────────────────────────────────────────────────────────

    def _dt(self, dias_atras, hora, minuto=0):
        """Datetime aware: hace `dias_atras` días a las hora:minuto locales."""
        fecha = timezone.localdate() - timedelta(days=dias_atras)
        return timezone.make_aware(datetime.combine(fecha, time(hora, minuto)))

    def _fechar_pedido(self, pedido, **campos):
        from apps.pedidos.models import Pedido
        Pedido.objects.filter(pk=pedido.pk).update(**campos)
        pedido.refresh_from_db()

    def _fechar_guia(self, guia, **campos):
        from apps.envios.models import Guia
        Guia.objects.filter(pk=guia.pk).update(**campos)
        guia.refresh_from_db()

    def _fechar_incidencia(self, inc, apertura, primera=None, resolucion=None, cierre=None):
        """Reescribe los relojes con los valores canónicos de settings.TORRE."""
        from apps.incidencias.models import Incidencia
        torre = settings.TORRE
        if inc.origen == Incidencia.ORIGEN_COMPRADOR:
            limite_respuesta = apertura + timedelta(minutes=torre["SLA_PRIMERA_RESPUESTA_COMPRADOR_MIN"])
        else:
            limite_respuesta = apertura + timedelta(hours=torre["SLA_PRIMERA_RESPUESTA_CLIENTE_HORAS"])
        campos = {
            "ts_apertura": apertura,
            "sla_respuesta_limite": limite_respuesta,
            "sla_resolucion_limite": apertura + timedelta(hours=torre["SLA_RESOLUCION_HORAS"]),
        }
        if primera is not None:
            campos["ts_primera_respuesta"] = primera
        if resolucion is not None:
            campos["ts_resolucion"] = resolucion
        if cierre is not None:
            campos["ts_cierre"] = cierre
        Incidencia.objects.filter(pk=inc.pk).update(**campos)
        inc.refresh_from_db()

    def _fechar_mensajes(self, inc, tiempos):
        """Escalona el timeline: N-ésimo mensaje → N-ésimo datetime."""
        from apps.incidencias.models import MensajeIncidencia
        for mensaje, ts in zip(inc.mensajes.order_by("pk"), tiempos):
            MensajeIncidencia.objects.filter(pk=mensaje.pk).update(ts=ts)

    # ────────────────────────────────────────────────────────────────────
    # Clientes, usuarios, catálogo
    # ────────────────────────────────────────────────────────────────────

    def _clientes(self):
        from apps.core.models import Cliente
        from apps.core.services import registrar_evento

        colima, creado = Cliente.objects.get_or_create(
            slug="colima",
            defaults={
                "nombre": "Cervecería Colima",
                "razon_social": "Cervecería de Colima S.A. de C.V.",
                "rfc": "CCO050525AB1",
                "contacto_nombre": "Karina Fuentes",
                "contacto_whatsapp": "+523121112233",
                "buffer_stock": 2,
                "carrier_preferente": "paquetexpress",
                "integracion_envios": "envia",
                "naked_packing_local": True,
                "guia_de_voz": (
                    "Voz cálida y directa, orgullosamente colimense. Hablamos de "
                    "'cerveza artesanal de Colima', jamás culpamos a la paquetería y "
                    "firmamos como 'el equipo de entregas de Cervecería Colima'."
                ),
            },
        )
        if creado:
            registrar_evento("cliente", colima.slug, "alta", cliente=colima,
                             motivo="Alta de cliente ancla en el seed demo")
        if not colima.branding:
            colima.branding = {
                "nombre_publico": "Cervecería de Colima",
                "color_primario": "#9E2B25",
                "color_fondo": "#F5EFE0",
                "color_texto": "#2B2118",
                "whatsapp_soporte": "523121112233",
            }
            colima.save(update_fields=["branding"])
        nocturno, creado = Cliente.objects.get_or_create(
            slug="mezcal-nocturno",
            defaults={
                "nombre": "Mezcal Nocturno",
                "razon_social": "Destilados Nocturnos S. de R.L.",
                "contacto_nombre": "Aurelio Cruz",
                "contacto_whatsapp": "+525522334455",
                "buffer_stock": 0,
                "carrier_preferente": "paquetexpress",
                "integracion_envios": "envia",
                "naked_packing_local": False,
                "guia_de_voz": "Voz sobria y artesanal. Nada de emojis; el mezcal se respeta.",
            },
        )
        if creado:
            registrar_evento("cliente", nocturno.slug, "alta", cliente=nocturno,
                             motivo="Alta de segundo tenant (prueba de aislamiento)")
        return colima, nocturno

    def _tiendas(self, colima, nocturno):
        from apps.integraciones.models import Tienda

        tiendas = {}
        especificacion = [
            ("colima-mx.myshopify.com", colima, "loc-colima-mx"),
            ("colima-usa.myshopify.com", colima, "loc-colima-usa"),
            ("mezcal-nocturno.myshopify.com", nocturno, "loc-nocturno"),
        ]
        for dominio, cliente, location in especificacion:
            tienda, _ = Tienda.objects.get_or_create(
                dominio=dominio,
                defaults={"cliente": cliente, "plataforma": Tienda.PLATAFORMA_SHOPIFY,
                          "token": "", "location_id": location, "activo": True},
            )
            tiendas[dominio] = tienda
        return tiendas

    def _usuarios(self, colima, nocturno):
        from apps.core.models import PerfilUsuario

        clientes = {"colima": colima, "mezcal-nocturno": nocturno}
        User = get_user_model()
        usuarios = {}
        for username, password, rol, cliente_slug, pin, nombre, es_super in USUARIOS:
            partes = nombre.split(" ", 1)
            user, _ = User.objects.get_or_create(username=username)
            user.first_name = partes[0]
            user.last_name = partes[1] if len(partes) > 1 else ""
            user.email = f"{username}@torre380e.mx"
            user.is_staff = es_super
            user.is_superuser = es_super
            user.set_password(password)
            user.save()
            if rol is not None:
                PerfilUsuario.objects.update_or_create(
                    usuario=user,
                    defaults={"rol": rol, "cliente": clientes.get(cliente_slug), "pin": pin},
                )
            usuarios[username] = user
        return usuarios

    def _catalogo(self, colima, nocturno):
        from apps.catalogo.models import SKU, Categoria, Lote, Ubicacion

        for codigo, tipo, carriers in UBICACIONES:
            Ubicacion.objects.get_or_create(
                codigo=codigo, defaults={"tipo": tipo, "carriers": carriers, "activo": True},
            )

        def _categoria(cliente, codigo):
            if codigo.endswith("-SIX"):
                nombre = "Six packs"
            elif "-C" in codigo:
                nombre = "Cajas"
            else:
                nombre = "Botellas"
            cat, _ = Categoria.objects.get_or_create(cliente=cliente, nombre=nombre)
            return cat

        skus = {}
        for cliente, especificacion in ((colima, SKUS_COLIMA), (nocturno, SKUS_NOCTURNO)):
            for codigo, desc, peso, largo, ancho, alto, precio, reorden, barras in especificacion:
                sku, _ = SKU.objects.get_or_create(
                    cliente=cliente, codigo=codigo,
                    defaults={
                        "descripcion": desc, "peso_gr": peso,
                        "largo_cm": largo, "ancho_cm": ancho, "alto_cm": alto,
                        "requiere_lote": True, "unidad": "pieza",
                        "precio_declarado": Decimal(precio), "punto_reorden": reorden,
                        "codigo_barras": barras, "activo": True,
                        # Cajas de 24: reempacables en 2 medias de 12 para
                        # abaratar el envío (motor de división).
                        "empaques_divisibles": 2 if "C24" in codigo else 1,
                    },
                )
                if "C24" in codigo and sku.empaques_divisibles != 2:
                    sku.empaques_divisibles = 2
                    sku.save(update_fields=["empaques_divisibles"])
                if sku.categoria_id is None:
                    sku.categoria = _categoria(cliente, codigo)
                    sku.save(update_fields=["categoria"])
                skus[codigo] = sku

        caducidad_cerveza = timezone.localdate() + timedelta(days=240)
        lotes = {}
        for stock in (STOCK_COLIMA, STOCK_NOCTURNO):
            for codigo_sku, (_ok, _dan, codigo_lote, _destinos) in stock.items():
                caducidad = caducidad_cerveza if codigo_sku in STOCK_COLIMA else None
                lote, _ = Lote.objects.get_or_create(
                    sku=skus[codigo_sku], codigo=codigo_lote,
                    defaults={"fecha_caducidad": caducidad},
                )
                lotes[codigo_sku] = lote
        return skus, lotes

    def _plantillas(self, colima):
        from apps.mensajeria.models import CUERPOS_DEFAULT, NOMBRES_DEFAULT, PlantillaMensaje

        for clave in ("A", "B", "E"):
            PlantillaMensaje.objects.get_or_create(
                clave=clave, cliente=None,
                defaults={"nombre": NOMBRES_DEFAULT[clave], "cuerpo": CUERPOS_DEFAULT[clave],
                          "aprobada_por_cliente": False},
            )
            PlantillaMensaje.objects.get_or_create(
                clave=clave, cliente=colima,
                defaults={"nombre": f"{NOMBRES_DEFAULT[clave]} · voz Colima",
                          "cuerpo": PLANTILLAS_COLIMA[clave], "aprobada_por_cliente": True},
            )

    def _reglas_envio(self, colima):
        from apps.envios.models import ReglaEnvio

        ReglaEnvio.objects.get_or_create(
            cliente=colima, prioridad=10,
            defaults={"condicion": {"es_local": True}, "carrier": "local", "servicio": "entrega_local"},
        )
        ReglaEnvio.objects.get_or_create(
            cliente=colima, prioridad=20,
            defaults={"condicion": {}, "carrier": "paquetexpress", "servicio": "ground"},
        )

    # ────────────────────────────────────────────────────────────────────
    # Stock inicial (ASN recibida + ubicada vía servicios → kardex vivo)
    # ────────────────────────────────────────────────────────────────────

    def _stock_inicial(self, cliente, skus, lotes, stock, dias_atras):
        from apps.catalogo.models import Ubicacion
        from apps.inventario.models import LineaASN, OrdenEntrada
        from apps.inventario.services import recibir, ubicar

        if OrdenEntrada.objects.filter(cliente=cliente, estado=OrdenEntrada.CERRADA).exists():
            return  # stock inicial ya sembrado

        orden = OrdenEntrada.objects.create(
            cliente=cliente, fecha_compromiso=timezone.localdate() - timedelta(days=dias_atras),
        )
        lineas = []
        for codigo_sku, (ok, danada, _lote, _destinos) in stock.items():
            lineas.append(LineaASN.objects.create(
                orden=orden, sku=skus[codigo_sku], cantidad_anunciada=ok + danada,
            ))
        for linea in lineas:
            ok, danada, _lote, _destinos = stock[linea.sku.codigo]
            recibir(linea, ok, danada, actor="jefe")
        ubicaciones = {u.codigo: u for u in Ubicacion.objects.all()}
        for codigo_sku, (_ok, _danada, _lote, destinos) in stock.items():
            for codigo_ubicacion, cantidad in destinos:
                ubicar(skus[codigo_sku], cantidad, ubicaciones[codigo_ubicacion],
                       lotes[codigo_sku], actor="jefe")
        orden.transicionar(OrdenEntrada.CERRADA, actor="jefe",
                           motivo="Inventario inicial contado, ubicado y vendible")
        OrdenEntrada.objects.filter(pk=orden.pk).update(
            creado=self._dt(dias_atras, 9, 30),
            ts_descarga_fin=self._dt(dias_atras, 11, 0),
            ts_vendible=self._dt(dias_atras, 13, 30),
        )

    def _asn_anunciada(self, colima, skus):
        from apps.core.services import registrar_evento
        from apps.inventario.models import LineaASN, OrdenEntrada

        if OrdenEntrada.objects.filter(cliente=colima, estado=OrdenEntrada.ANUNCIADA).exists():
            return
        # Próxima ventana de recepción: martes o jueves (BLUEPRINT §1.8).
        fecha = timezone.localdate() + timedelta(days=1)
        while fecha.weekday() not in (1, 3):
            fecha += timedelta(days=1)
        orden = OrdenEntrada.objects.create(cliente=colima, fecha_compromiso=fecha)
        for codigo, cantidad in (("COLIMITA-SIX", 120), ("PARAMO-SIX", 60), ("CAYACO-SIX", 48)):
            LineaASN.objects.create(orden=orden, sku=skus[codigo], cantidad_anunciada=cantidad)
        registrar_evento(
            "asn", orden.folio, "anunciada", actor="karina", cliente=colima,
            delta={"lineas": 3, "fecha_compromiso": str(fecha)},
            motivo="Resurtido anunciado desde el portal (cita en ventana mar/jue)",
        )

    # ────────────────────────────────────────────────────────────────────
    # Pedidos
    # ────────────────────────────────────────────────────────────────────

    def _payload(self, order_id, nombre, tel, cp, ciudad, provincia, lineas, note=""):
        """Payload estilo Shopify con los campos que la ingesta consume."""
        partes = nombre.split(" ", 1)
        total = sum(sku.precio_declarado * cantidad for sku, cantidad in lineas)
        return {
            "id": order_id,
            "name": f"#{order_id}",
            "email": f"comprador{order_id}@example.com",
            "financial_status": "paid",  # los filtros de ingesta solo dejan pasar lo pagado
            "total_price": str(total),
            "note": note,
            "customer": {"first_name": partes[0], "last_name": partes[1] if len(partes) > 1 else ""},
            "shipping_address": {
                "name": nombre, "phone": tel, "zip": cp,
                "address1": "Av. de los Insurgentes 123", "city": ciudad,
                "province": provincia, "country": "México",
            },
            "line_items": [
                {"sku": sku.codigo, "quantity": cantidad, "title": sku.descripcion}
                for sku, cantidad in lineas
            ],
        }

    def _crear_pedido(self, tienda, payload):
        """Ingesta por el MISMO camino que producción: webhook → procesar.

        Regresa (pedido, creado). Si la orden ya existía (re-corrida del seed)
        no se re-procesa ni se re-avanza.
        """
        from apps.integraciones.services import procesar_webhook, registrar_webhook
        from apps.pedidos.models import Pedido

        order_id = str(payload["id"])
        existente = Pedido.objects.filter(tienda=tienda, shopify_order_id=order_id).first()
        if existente is not None:
            return existente, False
        evento, _ = registrar_webhook(tienda, f"seed:{tienda.pk}:{order_id}", "orders/create", payload)
        pedido = procesar_webhook(evento)
        if pedido is None:
            raise CommandError(
                f"La ingesta del pedido demo {order_id} falló; revisa el SyncLog de {tienda.dominio}."
            )
        return pedido, True

    def _fotos_empaque(self, pedido, actor="piso1"):
        from apps.core.models import EvidenciaFoto
        for tipo in ("contenido", "caja_cerrada"):
            EvidenciaFoto.objects.create(
                entidad="pedido", entidad_id=str(pedido.pk), tipo=tipo,
                archivo=ContentFile(PNG_1PX, name=f"{pedido.folio}-{tipo}.png"),
                tomada_por=actor,
            )

    def _avanzar(self, pedido, hasta):
        """Avanza por el pipeline canónico usando los servicios de dominio.

        hasta ∈ {EN_PICKING, EMPACADO, GUIA_GENERADA, RECOLECTADO}.
        Regresa la Guia si se generó, o None.
        """
        from apps.pedidos.services import (
            confirmar_linea_pick, empacar, generar_guia, iniciar_picking, marcar_recolectado,
        )

        niveles = ["EN_PICKING", "EMPACADO", "GUIA_GENERADA", "RECOLECTADO"]
        objetivo = niveles.index(hasta)
        guia = None
        iniciar_picking(pedido, "piso1")
        if objetivo >= 1:
            for linea in pedido.lineas.all():
                confirmar_linea_pick(linea, linea.cantidad, "piso1")
            self._fotos_empaque(pedido)
            pedido.refresh_from_db()
            empacar(pedido, "piso1", pedido.peso_esperado_gr, fotos=[])
        if objetivo >= 2:
            guia = generar_guia(pedido)
        if objetivo >= 3:
            marcar_recolectado(pedido, "jefe")
        return guia

    def _en_transito(self, pedido, guia, motivo="En tránsito hacia destino (mock)"):
        guia.transicionar("EN_TRANSITO", motivo=motivo)
        pedido.transicionar("EN_TRANSITO", motivo=motivo)

    def _entregado(self, pedido, guia, motivo="Entregado (mock)"):
        guia.transicionar("ENTREGADO", motivo=motivo)
        pedido.transicionar("ENTREGADO", motivo=motivo)

    def _pedidos(self, colima, nocturno, tiendas, skus, usuarios):
        """~16 pedidos en todos los estados, con timestamps realistas escalonados.

        Regresa {clave: (pedido, creado)} para que las incidencias se cuelguen
        solo cuando el pedido acaba de sembrarse (idempotencia).
        """
        from apps.core.models import EvidenciaFoto
        from apps.inventario.services import despachar, retornar
        from apps.pedidos.services import cancelar, confirmar_linea_pick, iniciar_picking

        t_mx = tiendas["colima-mx.myshopify.com"]
        t_usa = tiendas["colima-usa.myshopify.com"]
        t_noc = tiendas["mezcal-nocturno.myshopify.com"]
        ahora = timezone.now()
        resultado = {}

        # ── C1 · ENTREGADO local (naked packing + POD) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5001, "Lupita Ramírez", "+523121234501", "28017", "Colima", "Colima",
            [(skus["COLIMITA-SIX"], 2)], note="Es para un cumpleaños, gracias.",
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            guia.transicionar("ENTREGADO", motivo="POD de entrega local: firma y foto en la puerta")
            pedido.transicionar(
                "ENTREGADO", actor="jefe",
                motivo="Entrega local: recibió Lupita Ramírez, mayoría de edad verificada.",
            )
            EvidenciaFoto.objects.create(
                entidad="pedido", entidad_id=str(pedido.pk), tipo="pod",
                archivo=ContentFile(PNG_1PX, name=f"{pedido.folio}-pod.png"), tomada_por="jefe",
            )
            self._fechar_pedido(
                pedido, creado=self._dt(6, 10, 15), ts_picking=self._dt(6, 10, 40),
                ts_empacado=self._dt(6, 11, 20), ts_guia=self._dt(6, 11, 25),
                ts_recolectado=self._dt(6, 12, 30), ts_entregado=self._dt(6, 16, 10),
            )
            self._fechar_guia(guia, creado=self._dt(6, 11, 25), ts_ultimo_movimiento=self._dt(6, 16, 10))
        resultado["entregado_local"] = (pedido, creado)

        # ── C2 · ENTREGADO foráneo CDMX (después: incidencia DAN) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5002, "Andrés Mejía", "+525512345602", "06700", "Ciudad de México", "CDMX",
            [(skus["TICUS-SIX"], 1), (skus["PARAMO-SIX"], 1)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._en_transito(pedido, guia)
            self._entregado(pedido, guia)
            self._fechar_pedido(
                pedido, creado=self._dt(5, 9, 50), ts_picking=self._dt(5, 10, 10),
                ts_empacado=self._dt(5, 10, 45), ts_guia=self._dt(5, 10, 50),
                ts_recolectado=self._dt(5, 13, 20), ts_en_transito=self._dt(4, 9, 5),
                ts_entregado=self._dt(3, 12, 40),
            )
            self._fechar_guia(guia, creado=self._dt(5, 10, 50), ts_ultimo_movimiento=self._dt(3, 12, 40))
        resultado["entregado_dan"] = (pedido, creado)

        # ── C3 · ENTREGADO foráneo GDL (después: incidencia FAL cerrada) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5003, "Renata Solís", "+523312345603", "44100", "Guadalajara", "Jalisco",
            [(skus["COLIMITA-C24"], 1)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._en_transito(pedido, guia)
            self._entregado(pedido, guia)
            self._fechar_pedido(
                pedido, creado=self._dt(3, 10, 20), ts_picking=self._dt(3, 10, 45),
                ts_empacado=self._dt(3, 11, 30), ts_guia=self._dt(3, 11, 35),
                ts_recolectado=self._dt(3, 13, 45), ts_en_transito=self._dt(2, 8, 30),
                ts_entregado=self._dt(1, 11, 0),
            )
            self._fechar_guia(guia, creado=self._dt(3, 11, 35), ts_ultimo_movimiento=self._dt(1, 11, 0))
        resultado["entregado_fal"] = (pedido, creado)

        # ── C4 · EN_TRANSITO Mérida, guía dormida (después: incidencia RET) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5004, "Jorge Canul", "+529991234604", "97000", "Mérida", "Yucatán",
            [(skus["PIEDRA-LISA-SIX"], 2)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._en_transito(pedido, guia)
            self._fechar_pedido(
                pedido, creado=self._dt(4, 11, 5), ts_picking=self._dt(4, 11, 30),
                ts_empacado=self._dt(4, 12, 5), ts_guia=self._dt(4, 12, 10),
                ts_recolectado=self._dt(4, 13, 50), ts_en_transito=self._dt(4, 18, 20),
            )
            self._fechar_guia(guia, creado=self._dt(4, 12, 10), ts_ultimo_movimiento=self._dt(4, 18, 20))
        resultado["transito_ret"] = (pedido, creado)

        # ── C5 · ENTREGA_PRESUNTA Monterrey ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5005, "Mariana Garza", "+528112345605", "64000", "Monterrey", "Nuevo León",
            [(skus["CAYACO-SIX"], 1), (skus["COLIMITA-SIX"], 1)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._en_transito(pedido, guia)
            pedido.transicionar(
                "ENTREGA_PRESUNTA",
                motivo="Sin evento del carrier tras la promesa; confirmando entrega con el comprador (POD alternativo).",
            )
            self._fechar_pedido(
                pedido, creado=self._dt(6, 9, 30), ts_picking=self._dt(6, 9, 55),
                ts_empacado=self._dt(6, 10, 35), ts_guia=self._dt(6, 10, 40),
                ts_recolectado=self._dt(6, 13, 10), ts_en_transito=self._dt(6, 19, 0),
            )
            self._fechar_guia(guia, creado=self._dt(6, 10, 40), ts_ultimo_movimiento=self._dt(6, 19, 0))
        resultado["presunta"] = (pedido, creado)

        # ── C6 · RETORNADO Tijuana (reingreso a cuarentena) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5006, "Óscar Valdez", "+526641234606", "22000", "Tijuana", "Baja California",
            [(skus["PARAMO-C12"], 1)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._en_transito(pedido, guia)
            guia.transicionar("RETORNO", motivo="Retornado al remitente: rechazado en destino (mock)")
            pedido.transicionar(
                "RETORNADO", actor="jefe",
                motivo="Carrier marcó retorno; reingreso fotografiado a cuarentena para inspección.",
            )
            for linea in pedido.lineas.all():
                if linea.cantidad_pickeada:
                    retornar(linea.sku, linea.cantidad_pickeada, pedido.folio, "jefe")
            self._fechar_pedido(
                pedido, creado=self._dt(5, 10, 40), ts_picking=self._dt(5, 11, 10),
                ts_empacado=self._dt(5, 12, 0), ts_guia=self._dt(5, 12, 5),
                ts_recolectado=self._dt(4, 13, 0), ts_en_transito=self._dt(4, 15, 0),
            )
            self._fechar_guia(guia, creado=self._dt(5, 12, 5), ts_ultimo_movimiento=self._dt(1, 9, 30))
        resultado["retornado"] = (pedido, creado)

        # ── C7 · RECOLECTADO hoy (tienda USA: multi-tienda) ──
        pedido, creado = self._crear_pedido(t_usa, self._payload(
            5007, "Diego Fernández", "+525512345607", "06100", "Ciudad de México", "CDMX",
            [(skus["COLIMITA-SIX"], 3)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._fechar_pedido(
                pedido, creado=ahora - timedelta(hours=5),
                ts_picking=ahora - timedelta(hours=4, minutes=30),
                ts_empacado=ahora - timedelta(hours=4),
                ts_guia=ahora - timedelta(hours=3, minutes=55),
                ts_recolectado=ahora - timedelta(hours=3, minutes=30),
            )
            self._fechar_guia(guia, ts_ultimo_movimiento=ahora - timedelta(hours=3, minutes=30))
        resultado["recolectado_hoy"] = (pedido, creado)

        # ── C8 · GUIA_GENERADA hoy (local, staging SAL-LOCAL) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5008, "Sofía Barragán", "+523121234608", "28045", "Villa de Álvarez", "Colima",
            [(skus["TICUS-C12"], 1)],
        ))
        if creado:
            self._avanzar(pedido, "GUIA_GENERADA")
            self._fechar_pedido(
                pedido, creado=ahora - timedelta(hours=3),
                ts_picking=ahora - timedelta(hours=2, minutes=40),
                ts_empacado=ahora - timedelta(hours=2, minutes=10),
                ts_guia=ahora - timedelta(hours=2),
            )
        resultado["guia_hoy"] = (pedido, creado)

        # ── C9 · EMPACADO hoy (foráneo) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5009, "Emilio Cortés", "+524421234609", "76000", "Querétaro", "Querétaro",
            [(skus["CAYACO-SIX"], 2)],
        ))
        if creado:
            self._avanzar(pedido, "EMPACADO")
            self._fechar_pedido(
                pedido, creado=ahora - timedelta(hours=2, minutes=30),
                ts_picking=ahora - timedelta(hours=2),
                ts_empacado=ahora - timedelta(hours=1, minutes=30),
            )
        resultado["empacado_hoy"] = (pedido, creado)

        # ── C10 · EN_PICKING hoy (pick parcial) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5010, "Valeria Chávez", "+523121234610", "28017", "Colima", "Colima",
            [(skus["PIEDRA-LISA-SIX"], 1), (skus["COLIMITA-SIX"], 1)],
        ))
        if creado:
            iniciar_picking(pedido, "piso1")
            primera = pedido.lineas.first()
            confirmar_linea_pick(primera, primera.cantidad, "piso1")
            self._fechar_pedido(
                pedido, creado=ahora - timedelta(hours=2),
                ts_picking=ahora - timedelta(hours=1),
            )
        resultado["picking_hoy"] = (pedido, creado)

        # ── C11 · PENDIENTE hoy ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5011, "Hugo Miranda", "+529991234611", "97300", "Mérida", "Yucatán",
            [(skus["PARAMO-SIX"], 2)],
        ))
        if creado:
            self._fechar_pedido(pedido, creado=ahora - timedelta(minutes=40))
        resultado["pendiente_hoy"] = (pedido, creado)

        # ── C12 · CANCELACION_PENDIENTE hoy (tarea de restock) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5012, "Brenda Ríos", "+523121234612", "28060", "Colima", "Colima",
            [(skus["COLIMITA-C24"], 1)],
        ))
        if creado:
            iniciar_picking(pedido, "piso1")
            cancelar(pedido, usuarios["mesa1"],
                     "La compradora se equivocó de dirección y canceló en Shopify.")
            self._fechar_pedido(
                pedido, creado=ahora - timedelta(hours=4),
                ts_picking=ahora - timedelta(hours=3, minutes=30),
            )
        resultado["cancelacion_pendiente"] = (pedido, creado)

        # ── C13 · CANCELADO (pre-picking: libera reserva) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5013, "Raúl Ochoa", "+525512345613", "06700", "Ciudad de México", "CDMX",
            [(skus["TICUS-SIX"], 2)],
        ))
        if creado:
            cancelar(pedido, usuarios["mesa1"],
                     "Cancelada por el comprador minutos después de pagar.")
            self._fechar_pedido(pedido, creado=self._dt(2, 12, 10))
        resultado["cancelado"] = (pedido, creado)

        # ── C14 · PARCIALMENTE_DESPACHADO (salió 1 de 2 bultos) ──
        pedido, creado = self._crear_pedido(t_mx, self._payload(
            5014, "Fernanda Lugo", "+526621234614", "83000", "Hermosillo", "Sonora",
            [(skus["COLIMITA-C24"], 1), (skus["PARAMO-C12"], 1)],
        ))
        if creado:
            guia = self._avanzar(pedido, "GUIA_GENERADA")
            primera = pedido.lineas.first()
            despachar(primera.sku, primera.cantidad_pickeada, pedido.folio)
            pedido.transicionar(
                "PARCIALMENTE_DESPACHADO", actor="jefe",
                motivo="Salió 1 de 2 bultos con Paquetexpress; el segundo sale en la siguiente recolección.",
            )
            self._fechar_pedido(
                pedido, creado=self._dt(1, 10, 0), ts_picking=self._dt(1, 10, 25),
                ts_empacado=self._dt(1, 11, 10), ts_guia=self._dt(1, 11, 15),
            )
            self._fechar_guia(guia, creado=self._dt(1, 11, 15), ts_ultimo_movimiento=self._dt(1, 14, 30))
        resultado["parcial"] = (pedido, creado)

        # ── N1 · Mezcal Nocturno ENTREGADO (aislamiento multi-tenant) ──
        pedido, creado = self._crear_pedido(t_noc, self._payload(
            5015, "Camila Ortega", "+525512345615", "06140", "Ciudad de México", "CDMX",
            [(skus["MN-JOVEN-750"], 2)],
        ))
        if creado:
            guia = self._avanzar(pedido, "RECOLECTADO")
            self._en_transito(pedido, guia)
            self._entregado(pedido, guia)
            self._fechar_pedido(
                pedido, creado=self._dt(2, 10, 30), ts_picking=self._dt(2, 11, 0),
                ts_empacado=self._dt(2, 11, 40), ts_guia=self._dt(2, 11, 45),
                ts_recolectado=self._dt(2, 13, 15), ts_en_transito=self._dt(1, 9, 10),
                ts_entregado=self._dt(1, 13, 0),
            )
            self._fechar_guia(guia, creado=self._dt(2, 11, 45), ts_ultimo_movimiento=self._dt(1, 13, 0))
        resultado["nocturno_entregado"] = (pedido, creado)

        # ── N2 · Mezcal Nocturno PENDIENTE hoy ──
        pedido, creado = self._crear_pedido(t_noc, self._payload(
            5016, "Tomás Bravo", "+523312345616", "45040", "Zapopan", "Jalisco",
            [(skus["MN-REP-750"], 1)],
        ))
        if creado:
            self._fechar_pedido(pedido, creado=ahora - timedelta(hours=1))
        resultado["nocturno_pendiente"] = (pedido, creado)

        return resultado

    # ────────────────────────────────────────────────────────────────────
    # Incidencias (DAN abierta con timeline · RET resuelta · FAL cerrada)
    # ────────────────────────────────────────────────────────────────────

    def _incidencias(self, colima, pedidos, usuarios):
        from apps.core.models import EvidenciaFoto
        from apps.core.services import registrar_evento
        from apps.incidencias.models import Compensacion, ReclamacionCarrier
        from apps.incidencias.services import abrir_incidencia, cerrar, resolver, responder
        from apps.mensajeria.services import enviar_retraso

        # ── DAN abierta (EN_CURSO) con timeline, compensación y reclamación ──
        pedido, creado = pedidos["entregado_dan"]
        if creado:
            inc = abrir_incidencia(
                colima, "DAN", "comprador", pedido=pedido,
                texto=("Mi pedido llegó con dos botellas de Ticús estrelladas; era un regalo. "
                       "Les adjunto la foto de cómo llegó la caja."),
            )
            EvidenciaFoto.objects.create(
                entidad="incidencia", entidad_id=inc.folio, tipo="dano",
                archivo=ContentFile(PNG_1PX, name=f"{inc.folio}-dano.png"),
                tomada_por="comprador", congelada=True,
            )
            responder(inc, "mesa1", "mesa",
                      ("Lamentamos mucho la rotura, Andrés: sabemos que era un regalo. "
                       "Hoy mismo sale la reposición express con el gesto de disculpa de la casa. "
                       "Te comparto la guía nueva en cuanto la tenga."))
            responder(inc, "mesa1", "mesa",
                      ("Caja con golpe de esquina; evidencia de empaque congelada. Procede "
                       "reposición a nuestro costo y reclamación a Paquetexpress con el expediente."),
                      interno=True)
            responder(inc, "Karina Fuentes", "cliente",
                      "De acuerdo con la reposición. Usen la nota de disculpa firmada que dejamos en bodega.")
            inc.transicionar("EN_CURSO", actor=usuarios["mesa1"],
                             motivo="La Mesa toma el caso: reposición express en curso")
            inc.dueno = "mesa1"
            inc.save(update_fields=["dueno"])

            comp = Compensacion.objects.create(
                incidencia=inc, tipo=Compensacion.TIPO_REPOSICION, monto=Decimal("215.00"),
            )
            registrar_evento(
                "compensacion", comp.pk, "creada", actor=usuarios["mesa1"], cliente=colima,
                delta={"incidencia": inc.folio, "tipo": comp.tipo, "monto": str(comp.monto)},
            )
            comp.transicionar(Compensacion.APROBADA, actor=usuarios["karina"],
                              motivo="Visto bueno de la marca a la reposición")
            rec = ReclamacionCarrier.objects.create(
                incidencia=inc, carrier="paquetexpress", monto_reclamado=Decimal("430.00"),
            )
            registrar_evento(
                "reclamacion_carrier", rec.pk, "creada", actor=usuarios["mesa1"], cliente=colima,
                delta={"incidencia": inc.folio, "carrier": rec.carrier,
                       "monto_reclamado": str(rec.monto_reclamado)},
            )
            rec.transicionar(ReclamacionCarrier.PRESENTADA, actor=usuarios["mesa1"],
                             motivo="Expediente presentado con fotos, báscula y valor declarado")

            apertura = self._dt(1, 10, 0)
            self._fechar_incidencia(inc, apertura, primera=self._dt(1, 10, 25))
            self._fechar_mensajes(inc, [apertura, self._dt(1, 10, 25),
                                        self._dt(1, 10, 40), self._dt(1, 12, 0)])

        # ── RET resuelta (guía dormida detectada por el poller) ──
        pedido, creado = pedidos["transito_ret"]
        if creado:
            guia = pedido.guias.order_by("-id").first()
            horas = settings.TORRE["SIN_MOVIMIENTO_FORANEO_HORAS"]
            inc = abrir_incidencia(
                colima, "RET", "auto", pedido=pedido,
                texto=(f"Guía {guia.numero} (paquetexpress) sin movimiento por más de {horas} h "
                       "en ruta foránea. Último evento: En tránsito hacia destino."),
            )
            responder(inc, "mesa1", "mesa",
                      ("Levantamos reporte con Paquetexpress: el embarque está detenido en el hub "
                       "de Villahermosa. Seguimiento cada 12 h hasta que retome ruta."))
            resolver(inc,
                     ("Paquetexpress confirmó que la unidad retomó ruta; nueva entrega estimada "
                      "mañana. Avisamos al comprador con la disculpa en voz de la marca."),
                     usuarios["mesa1"])
            enviar_retraso(pedido, timezone.localdate() + timedelta(days=1))

            apertura = self._dt(1, 9, 0)
            self._fechar_incidencia(inc, apertura, primera=self._dt(1, 9, 45),
                                    resolucion=self._dt(1, 14, 0))
            self._fechar_mensajes(inc, [apertura, self._dt(1, 9, 45), self._dt(1, 14, 0)])

        # ── FAL cerrada (faltante reportado tras la entrega) ──
        pedido, creado = pedidos["entregado_fal"]
        if creado:
            inc = abrir_incidencia(
                colima, "FAL", "comprador", pedido=pedido,
                texto="La caja llegó con 23 botellas; faltó una Colimita.",
            )
            responder(inc, "mesa1", "mesa",
                      ("Revisamos las fotos y el peso de báscula del empaque; procede la "
                       "reposición sin vueltas: hoy mismo sale tu botella faltante."))
            resolver(inc,
                     ("Reposición de la pieza faltante enviada y recibida. Auditamos la mesa "
                      "de empaque del turno; el conteo por línea queda reforzado."),
                     usuarios["mesa1"])
            cerrar(inc, usuarios["mesa1"])

            apertura = self._dt(1, 12, 30)
            self._fechar_incidencia(inc, apertura, primera=self._dt(1, 12, 50),
                                    resolucion=self._dt(1, 16, 30), cierre=self._dt(1, 18, 0))
            self._fechar_mensajes(inc, [apertura, self._dt(1, 12, 50), self._dt(1, 16, 30)])

    # ────────────────────────────────────────────────────────────────────
    # Conteos, sync y ajuste con doble firma
    # ────────────────────────────────────────────────────────────────────

    def _vendible(self, sku):
        from django.db.models import Sum

        from apps.inventario.models import Saldo
        total = Saldo.objects.filter(sku=sku, estado=Saldo.UBICADO_VENDIBLE).aggregate(
            t=Sum("cantidad"))["t"]
        return total or 0

    def _conteos(self, skus):
        from apps.inventario.models import Conteo
        from apps.inventario.services import generar_conteo_ciclico, registrar_conteo

        generar_conteo_ciclico()  # tareas del día (idempotente)
        if Conteo.objects.exists():
            return
        exactos = ["COLIMITA-SIX", "PARAMO-SIX", "CAYACO-SIX", "PIEDRA-LISA-SIX", "MN-JOVEN-750"]
        for codigo in exactos:
            sku = skus[codigo]
            registrar_conteo(sku, self._vendible(sku), "jefe")
        # Diferencia de −2 en Ticús: bajo el umbral de settings.TORRE (no abre DES);
        # se corrige con el ajuste de doble firma.
        ticus = skus["TICUS-SIX"]
        registrar_conteo(ticus, self._vendible(ticus) - 2, "jefe")

    def _sync(self, tiendas):
        from apps.integraciones.services import push_inventario, reconciliar_pedidos

        for tienda in tiendas.values():
            reconciliar_pedidos(tienda)
        return push_inventario()

    def _ajuste(self, skus):
        from apps.inventario.models import Ajuste, Conteo

        if Ajuste.objects.filter(motivo=Ajuste.MOTIVO_CONTEO).exists():
            return
        from apps.inventario.services import aplicar_ajuste

        ticus = skus["TICUS-SIX"]
        conteo = Conteo.objects.filter(sku=ticus).order_by("-ts").first()
        aplicar_ajuste(
            ticus, -2, Ajuste.MOTIVO_CONTEO,
            "piso1", "1111", "jefe", "2222",
            lote=None, conteo=conteo, incidencia_ref="",
        )

    # ────────────────────────────────────────────────────────────────────
    # Orquestación
    # ────────────────────────────────────────────────────────────────────

    @transaction.atomic
    def handle(self, *args, **options):
        colima, nocturno = self._clientes()
        tiendas = self._tiendas(colima, nocturno)
        usuarios = self._usuarios(colima, nocturno)
        skus, lotes = self._catalogo(colima, nocturno)
        self._plantillas(colima)
        self._reglas_envio(colima)
        self._stock_inicial(colima, skus, lotes, STOCK_COLIMA, dias_atras=7)
        self._stock_inicial(nocturno, skus, lotes, STOCK_NOCTURNO, dias_atras=7)
        self._asn_anunciada(colima, skus)
        pedidos = self._pedidos(colima, nocturno, tiendas, skus, usuarios)
        self._incidencias(colima, pedidos, usuarios)
        self._conteos(skus)
        self._sync(tiendas)
        self._ajuste(skus)  # después del push: deja 1 SKU en la cola (se ve viva)
        self._resumen()

    def _resumen(self):
        from apps.core.models import Cliente, EventoAuditoria
        from apps.incidencias.models import Incidencia
        from apps.integraciones.models import Tienda
        from apps.inventario.models import Ajuste, Conteo, Movimiento, Saldo
        from apps.pedidos.models import Pedido

        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING("Torre · seed demo listo — Local 380 E"))
        w(f"  Clientes: {Cliente.objects.count()} · Tiendas: {Tienda.objects.count()} · "
          f"Pedidos: {Pedido.objects.count()} · Incidencias: {Incidencia.objects.count()}")
        w(f"  Saldos: {Saldo.objects.count()} · Movimientos (kardex): {Movimiento.objects.count()} · "
          f"Conteos: {Conteo.objects.count()} · Ajustes: {Ajuste.objects.count()} · "
          f"Eventos de auditoría: {EventoAuditoria.objects.count()}")
        w("")
        w(self.style.MIGRATE_HEADING("Usuarios demo (usuario · password · rol)"))
        etiquetas = {
            "admin": "superusuario Django (/admin/)",
            "karina": "portal · Cervecería Colima (/portal/)",
            "mario": "portal · Cervecería Colima (/portal/)",
            "nocturno": "portal · Mezcal Nocturno — prueba de aislamiento (/portal/)",
            "piso1": "piso · PIN de firma 1111 (/piso/)",
            "jefe": "piso · PIN de firma 2222 (/piso/)",
            "mesa1": "mesa de control · PIN de firma 3333 (/mesa/)",
        }
        for username, password, _rol, _cliente, _pin, _nombre, _super in USUARIOS:
            w(f"  {username:<9} {password:<14} {etiquetas[username]}")
        w("")
        # División de envíos + tokens de rastreo para los pedidos del demo
        try:
            from apps.envios.cotizador import planificar_envio
            from apps.rastreo.services import obtener_o_crear_token, url_publica
            from apps.pedidos.models import Pedido as _Pedido
            ejemplo = None
            for pedido in _Pedido.objects.all():
                try:
                    planificar_envio(pedido)
                except ValueError:
                    pass
                obtener_o_crear_token(pedido)
                if ejemplo is None and pedido.paquetes.count() > 1:
                    ejemplo = pedido
            if ejemplo is not None:
                w(f"  Rastreo brandeado de ejemplo (pedido dividido): {url_publica(ejemplo)}")
        except Exception as exc:  # el seed no muere por el plan de envíos
            w(self.style.WARNING(f"  Aviso: división/rastreo parcial: {exc}"))
        w(self.style.SUCCESS("Listo: entra a /mesa/ con mesa1 para ver la torre de control viva."))
