"""Prepara el demo en vivo para el cliente: datos frescos de HOY, cada vez.

Corre esto antes de cada demo (`manage.py demo_en_vivo`). Es idempotente por
día y re-ejecutable:
  1. Avanza los pedidos demo de días anteriores hasta entregarse (se vuelven
     historial realista, no backlog rancio).
  2. Cierra las recepciones demo viejas (ubica su put-away y las cierra).
  3. Crea el set de HOY: una recepción descargándose ahora, una cita para
     mañana, 8 pedidos en cada etapa del ciclo y una incidencia conversada.
Todo pasa por los servicios oficiales: kardex, auditoría y SLAs quedan vivos.
"""
import datetime

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

DEMO_EMAIL_PREFIJO = "demo-"
DOMINIO = "@wop.partners"


def _svg(texto, color="#9E2B25"):
    contenido = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600">'
        f'<rect width="800" height="600" fill="#111"/><rect x="24" y="24" width="752" height="552" fill="none" stroke="{color}" stroke-width="6"/>'
        f'<text x="400" y="285" fill="#FFFAF8" font-family="monospace" font-size="30" text-anchor="middle">{texto}</text>'
        f'<text x="400" y="330" fill="{color}" font-family="monospace" font-size="20" text-anchor="middle">EVIDENCIA · CENTRO DE DISTRIBUCION</text></svg>'
    )
    return ContentFile(contenido.encode(), name=f"{texto.lower().replace(' ', '-')[:38]}.svg")


class Command(BaseCommand):
    help = "Refresca los datos del demo en vivo (fechados HOY). Re-ejecutable."

    def handle(self, *args, **opciones):
        from apps.catalogo.models import SKU, Ubicacion, Lote
        from apps.core.models import Cliente, EvidenciaFoto
        from apps.inventario.models import OrdenEntrada, LineaASN, Saldo
        from apps.inventario.services import recibir, ubicar, cerrar_recepcion
        from apps.pedidos.models import Pedido
        from apps.pedidos.services import (
            crear_pedido_manual, iniciar_picking, confirmar_linea_pick,
            empacar, generar_guia, marcar_recolectado,
        )

        colima = Cliente.objects.filter(slug__startswith="colima").first()
        mesa = User.objects.get(username="mesa1")
        piso = User.objects.get(username="piso1")
        jefe = User.objects.get(username="jefe")
        hoy = timezone.localdate()
        sentinel_hoy = f"{DEMO_EMAIL_PREFIJO}{hoy:%Y%m%d}{DOMINIO}"

        def foto(entidad, entidad_id, tipo, texto, quien="piso1"):
            if not EvidenciaFoto.objects.filter(entidad=entidad, entidad_id=str(entidad_id), tipo=tipo).exists():
                EvidenciaFoto.objects.create(entidad=entidad, entidad_id=str(entidad_id),
                                             tipo=tipo, archivo=_svg(texto), tomada_por=quien)

        def empacar_con_fotos(pedido):
            foto("pedido", pedido.pk, "contenido", f"CONTENIDO {pedido.folio}")
            foto("pedido", pedido.pk, "caja_cerrada", f"CAJA CERRADA {pedido.folio}")
            fotos = list(EvidenciaFoto.objects.filter(entidad="pedido", entidad_id=str(pedido.pk)))
            empacar(pedido, piso, pedido.peso_esperado_gr, fotos)

        # ── 1. Avanzar pedidos demo de días anteriores a su final feliz ──
        viejos = Pedido.objects.filter(
            comprador_email__startswith=DEMO_EMAIL_PREFIJO, creado__date__lt=hoy,
        ).exclude(estado__in=[Pedido.ENTREGADO, Pedido.CANCELADO, Pedido.RETORNADO])
        viejos = list(viejos) + list(Pedido.objects.filter(
            comprador_email__in=["demo-grab@wop.partners", "qr-demo@wop.partners"],
        ).exclude(estado__in=[Pedido.ENTREGADO, Pedido.CANCELADO, Pedido.RETORNADO]))
        avanzados = 0
        for p in viejos:
            try:
                if p.estado == Pedido.PENDIENTE:
                    iniciar_picking(p, piso)
                p.refresh_from_db()
                if p.estado == Pedido.EN_PICKING:
                    for ln in p.lineas.all():
                        faltan = ln.cantidad - ln.cantidad_pickeada
                        if faltan > 0:
                            confirmar_linea_pick(ln, faltan, piso, codigo_escaneado=ln.sku.codigo)
                    empacar_con_fotos(p)
                p.refresh_from_db()
                if p.estado == Pedido.EMPACADO:
                    generar_guia(p)
                p.refresh_from_db()
                if p.estado == Pedido.GUIA_GENERADA:
                    marcar_recolectado(p, piso)
                p.refresh_from_db()
                if p.estado == Pedido.RECOLECTADO:
                    if p.es_local:
                        foto("entrega_local", p.pk, "pod", f"POD {p.folio}")
                        p.transicionar(Pedido.ENTREGADO, actor=piso)
                        guia = p.guias.order_by("-id").first()
                        if guia:
                            try:
                                guia.transicionar("ENTREGADO", actor="demo")
                            except ValueError:
                                pass
                    else:
                        p.transicionar(Pedido.EN_TRANSITO, actor=piso)
                avanzados += 1
            except Exception as exc:  # noqa: BLE001 — demo: seguir con el resto
                self.stdout.write(f"  (viejo {p.folio} se quedó en {p.estado}: {exc})")

        # ── 2. Cerrar recepciones demo viejas ──
        cerradas = 0
        for orden in OrdenEntrada.objects.filter(
            cliente=colima, creado__date__lt=hoy,
            estado__in=[OrdenEntrada.EN_RECEPCION, OrdenEntrada.RECIBIDA],
        ):
            try:
                destino = Ubicacion.objects.filter(tipo="picking", activo=True).first()
                for linea in orden.lineas.all():
                    pendiente = Saldo.objects.filter(sku=linea.sku, estado="en_putaway").first()
                    while pendiente:
                        lote = pendiente.lote or Lote.objects.get_or_create(
                            sku=linea.sku, codigo=f"L-{hoy:%y%m}",
                            defaults={"fecha_caducidad": hoy + datetime.timedelta(days=240)})[0]
                        ubicar(linea.sku, pendiente.cantidad, destino, lote, jefe)
                        pendiente = Saldo.objects.filter(sku=linea.sku, estado="en_putaway").first()
                cerrar_recepcion(orden, jefe, tarimas_recibidas=orden.tarimas or None)
                cerradas += 1
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(f"  ({orden.folio} no se pudo cerrar: {exc})")

        # ── 3. El set de HOY ──
        creados = []
        if colima and not Pedido.objects.filter(comprador_email=sentinel_hoy).exists():
            def sku(codigo):
                return SKU.objects.get(cliente=colima, codigo=codigo)

            def pedido(nombre, cp, calle, ciudad, lineas):
                return crear_pedido_manual(
                    colima, comprador_nombre=nombre, comprador_tel="+525512345678",
                    comprador_email=sentinel_hoy,
                    direccion={"name": nombre, "address1": calle, "city": ciudad, "zip": cp},
                    cp=cp, lineas=[(sku(c), n) for c, n in lineas], actor=mesa)

            def pickear(p):
                iniciar_picking(p, piso)
                for ln in p.lineas.all():
                    confirmar_linea_pick(ln, ln.cantidad, piso, codigo_escaneado=ln.sku.codigo)
                return p

            # Recepción descargándose AHORA + cita mañana
            asn = OrdenEntrada.objects.create(cliente=colima, fecha_compromiso=hoy, tarimas=22)
            lineas_asn = {c: LineaASN.objects.create(orden=asn, sku=sku(c), cantidad_anunciada=n)
                          for c, n in [("COLIMITA-C24", 60), ("PARAMO-C12", 48), ("CAYACO-SIX", 36)]}
            foto("asn", asn.folio, "llegada", "LLEGADA CAMION 22 TARIMAS", "jefe")
            recibir(lineas_asn["COLIMITA-C24"], 38, 0, jefe)
            recibir(lineas_asn["PARAMO-C12"], 30, 1, jefe)
            OrdenEntrada.objects.create(cliente=colima, fecha_compromiso=hoy + datetime.timedelta(days=1), tarimas=18)

            p1 = pedido("Ana Sofía Rivera", "06700", "Av. Álvaro Obregón 185, Roma Norte", "CDMX",
                        [("COLIMITA-SIX", 2), ("PARAMO-SIX", 1)])
            p2 = pedido("Héctor Manzano", "64060", "Av. Constitución 1500", "Monterrey",
                        [("COLIMITA-C24", 1)])
            p3 = pedido("Paola Guzmán", "44160", "Av. Chapultepec 480", "Guadalajara",
                        [("TICUS-SIX", 3)])
            iniciar_picking(p3, piso)
            confirmar_linea_pick(p3.lineas.first(), 1, piso, codigo_escaneado="TICUS-SIX")
            p4 = pickear(pedido("Rodrigo Palma", "97130", "Calle 21 #99, Itzimná", "Mérida",
                                [("TICUS-C12", 2)]))
            p5 = pickear(pedido("Camila Estrada", "03230", "Gabriel Mancera 830, Del Valle", "CDMX",
                                [("COLIMITA-SIX", 2), ("CAYACO-SIX", 1)]))
            empacar_con_fotos(p5)
            p6 = pickear(pedido("Iván Robledo", "01780", "Camino Real 77, Olivar de los Padres", "CDMX",
                                [("PIEDRA-LISA-SIX", 3)]))
            empacar_con_fotos(p6)
            generar_guia(p6)
            p7 = pickear(pedido("Fernanda Iturbe", "76048", "Av. Zaragoza 250, Centro", "Querétaro",
                                [("PARAMO-C12", 2)]))
            empacar_con_fotos(p7)
            generar_guia(p7)
            marcar_recolectado(p7, piso)
            p7.refresh_from_db()
            p7.transicionar(Pedido.EN_TRANSITO, actor=piso)
            g7 = p7.guias.order_by("-id").first()
            try:
                g7.transicionar("EN_TRANSITO", actor="demo")
            except ValueError:
                pass
            p8 = pickear(pedido("Valentina Cruz", "01780", "Av. Toluca 620", "CDMX",
                                [("COLIMITA-SIX", 2)]))
            empacar_con_fotos(p8)
            generar_guia(p8)
            marcar_recolectado(p8, piso)
            foto("entrega_local", p8.pk, "pod", f"POD {p8.folio} RECIBIO V CRUZ")
            p8.refresh_from_db()
            p8.transicionar(Pedido.ENTREGADO, actor=piso)

            from apps.incidencias.services import abrir_incidencia, responder
            inc = abrir_incidencia(colima, "RET", "cliente", pedido=p7,
                                   texto="Nuestra clienta de Querétaro pregunta cuándo llega su pedido.")
            responder(inc, "mesa1", "mesa",
                      "Va en tránsito — salió hoy en la primera recolección. Le compartimos su página "
                      "de rastreo y cualquier cambio lo avisamos aquí mismo.")
            creados = [p1, p2, p3, p4, p5, p6, p7, p8]

        # ── 4. Chuleta para el demo ──
        from apps.rastreo.services import obtener_o_crear_token_etiqueta, url_publica_etiqueta
        import os
        base = os.environ.get("BASE_URL_PUBLICA", "http://127.0.0.1:8380").rstrip("/")
        self.stdout.write(self.style.SUCCESS(
            f"\n═══ DEMO LISTO · {hoy} · {avanzados} pedidos viejos avanzados · {cerradas} recepciones cerradas ═══"))
        self.stdout.write(f"Base: {base}  (mesa1/mesa2026 · piso1/piso2026 · karina/colima2026)")
        hoy_qs = Pedido.objects.filter(comprador_email=sentinel_hoy).order_by("pk")
        for p in hoy_qs:
            self.stdout.write(f"  {p.folio} · {p.get_estado_display():24s} · {p.comprador_nombre} · CP {p.cp}")
        for p in hoy_qs.filter(estado__in=[Pedido.GUIA_GENERADA, Pedido.EN_TRANSITO, Pedido.ENTREGADO]):
            for g in p.guias.all():
                obtener_o_crear_token_etiqueta(g)
                self.stdout.write(f"  ETIQUETA {p.folio}: {base}/piso/etiqueta/{g.pk}/  →  QR: {url_publica_etiqueta(g)}")
        en_transito = hoy_qs.filter(estado=Pedido.EN_TRANSITO).first()
        if en_transito:
            from apps.rastreo.services import obtener_o_crear_token
            acc = obtener_o_crear_token(en_transito)
            token = getattr(acc, "token", acc)
            self.stdout.write(f"  RASTREO COMPRADOR: {base}/r/{token}/")
        self.stdout.write(f"  EMPACAR EN VIVO: {base}/piso/empaque/  (pedido de Mérida listo para empacar)")
        self.stdout.write(f"  BODEGA EN VIVO: {base}/mesa/bodega/  ·  PORTAL: {base}/portal/bodega/")
