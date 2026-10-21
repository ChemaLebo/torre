"""Siembra en la base LOCAL los escenarios del lote 2026-10-22 para probarlo a
mano (Chema): el fix del manifiesto por caja, el lote por pedido, cancelar
una reposición ya empacada y los reingresos.

- SEED-CAJAS-A: tres cajas de UNA pieza del mismo producto con lote, empacadas,
  con guía y confirmadas en el corral. Manifiesta dos: el kardex resta 2, no 3.
- SEED-CAJAS-B: UNA pieza reempacada en dos medias cajas, en el corral: al
  manifestar la primera mitad sale la unidad entera.
- SEED-CAJAS-C: pedido de 2 piezas en picking, sin dueño y al FRENTE de la cola
  del piso (EMPEZAR lo ofrece primero): sirve para probar el QR obligatorio y
  la columna Lotes; luego se empaca con el wizard.
- SEED-CAJAS-D: pedido entregado con una reposición APROBADA de 1 pieza (caja 2
  planeada que repone a la caja 1), también en picking libre y segundo en la
  cola: se pickea, se empaca y después se cancela desde Mesa con el modal.

Solo con DEBUG=True (nunca en producción). Idempotente: borra y vuelve a
sembrar sus pedidos (folio SEED-CAJAS-*), usa el producto SEED-C24 (lote
SEEDLOTE) en el rack SEED-A3, y no habla con ningún carrier (las guías son
registros locales y el plan de cajas de la reposición se siembra a mano).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

PRODUCTO = "SEED-C24"
LOTE = "SEEDLOTE"


class Command(BaseCommand):
    help = "Siembra los escenarios del lote 2026-10-22 (manifiesto por caja, lote por pedido, reposición empacada) en la base local."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Solo en local: este seed necesita DEBUG=True.")
        from django.contrib.auth import get_user_model
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.catalogo.models import SKU, Lote, Ubicacion
        from apps.catalogo.services import asignar_producto
        from apps.core.models import Cliente, PerfilUsuario
        from apps.envios.models import Guia, Paquete, PaqueteLinea
        from apps.incidencias.models import Incidencia
        from apps.incidencias.services import abrir_incidencia, crear_compensacion
        from apps.inventario.models import LineaASN, OrdenEntrada
        from apps.inventario.services import recibir, reservar, ubicar
        from apps.pedidos.models import LineaPedido, Pedido
        from apps.pedidos.services import confirmar_linea_pick, empacar, iniciar_picking

        def foto():
            return SimpleUploadedFile("seed.jpg", b"seed", content_type="image/jpeg")

        hace = lambda dias: timezone.now() - timedelta(days=dias)  # noqa: E731

        # El plan de cajas de la reposición se siembra a mano: nada de cotizar con el carrier.
        with patch("apps.envios.cotizador.planificar_envio", lambda *a, **k: None), transaction.atomic():
            cliente = Cliente.objects.filter(activo=True).order_by("pk").first()
            if cliente is None:
                cliente = Cliente.objects.create(nombre="Cliente demo", slug="demo", integracion_envios="envia")
            User = get_user_model()
            piso = User.objects.filter(perfil__rol="piso", is_active=True).first()
            if piso is None:
                piso = User.objects.create_user("seed-piso", password="seed-piso")
                PerfilUsuario.objects.create(usuario=piso, rol="piso", pin="9999")
            # Limpieza de una siembra anterior (la guía protege al pedido: primero ella).
            from apps.incidencias.models import Compensacion
            Compensacion.objects.filter(incidencia__pedido__folio__startswith="SEED-CAJAS-").delete()
            Incidencia.objects.filter(pedido__folio__startswith="SEED-CAJAS-").delete()
            Guia.objects.filter(pedido__folio__startswith="SEED-CAJAS-").delete()
            Pedido.objects.filter(folio__startswith="SEED-CAJAS-").delete()
            if not Ubicacion.objects.filter(tipo=Ubicacion.RECEPCION, activo=True).exists():
                Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
            rack, _ = Ubicacion.objects.get_or_create(codigo="SEED-A3", defaults={"tipo": Ubicacion.PICKING, "largo_cm": 180, "ancho_cm": 58, "alto_cm": 52})
            if not Ubicacion.objects.filter(tipo=Ubicacion.SALIDA, activo=True).exists():
                Ubicacion.objects.create(codigo="SAL-OTRO", tipo=Ubicacion.SALIDA)
            sku, _ = SKU.objects.get_or_create(
                cliente=cliente, codigo=PRODUCTO,
                defaults={"codigo_barras": "7509999000024", "descripcion": "Seed caja 24 (prueba del lote 2026-10-22)", "peso_gr": 9000,
                          "requiere_lote": True, "precio_declarado": Decimal("500")},
            )
            lote, _ = Lote.objects.get_or_create(sku=sku, codigo=LOTE, defaults={"fecha_caducidad": "2027-06-01"})
            # Stock: 20 piezas del lote en el rack fijo del producto.
            orden = OrdenEntrada.objects.create(cliente=cliente)
            linea_asn = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=20, lote_codigo=LOTE)
            recibir(linea_asn, 20, 0, piso)
            ubicar(sku, 20, rack, lote, piso)
            asignar_producto(rack, sku, actor="seed")

            def _pedido(folio, cantidad, estado=Pedido.PENDIENTE):
                pedido = Pedido.objects.create(
                    cliente=cliente, origen="manual", folio=folio, estado=estado, comprador_nombre="Prueba lote 2026-10-22",
                    comprador_tel="3120000000", cp="44100", direccion={"address1": "Av. Prueba 1", "city": "Guadalajara"},
                    peso_esperado_gr=sku.peso_gr * cantidad,
                )
                linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=cantidad)
                return pedido, linea

            def _reservar(pedido, linea):
                if not reservar(sku, linea.cantidad, pedido.folio):
                    raise CommandError("No alcanzó el stock sembrado para reservar.")
                linea.reservada = True
                linea.save(update_fields=["reservada"])

            def _pickear_todo(pedido, linea):
                iniciar_picking(pedido, piso)
                for _ in range(linea.cantidad):  # un escaneo por pieza, con posición y lote como el QR
                    confirmar_linea_pick(linea, 1, piso, ubicacion=rack, lote=lote)

            def _caja(pedido, linea, numero, cantidad, fraccion_de=1, estado=Paquete.EMPACADO, con_guia=True, repone_a=None):
                caja = Paquete.objects.create(pedido=pedido, numero=numero, peso_kg=Decimal("9"), carrier="estafeta", estado=estado)
                PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=cantidad, fraccion_de=fraccion_de, repone_a=repone_a)
                if con_guia:
                    Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero=f"{pedido.folio}-C{numero}", proveedor="mock", estado=Guia.GUIA_CREADA)
                    ahora = timezone.now()
                    Paquete.objects.filter(pk=caja.pk).update(ts_etiquetas=ahora, ts_cierre=ahora, ts_listo_salida=ahora)
                return Paquete.objects.get(pk=caja.pk)

            # A: tres cajas de una pieza, en el corral.
            a, la = _pedido("SEED-CAJAS-A", 3)
            _reservar(a, la)
            _pickear_todo(a, la)
            empacar(a, piso, a.peso_esperado_gr, [foto()])
            for n in (1, 2, 3):
                _caja(a, la, n, 1)
            Pedido.objects.filter(pk=a.pk).update(estado=Pedido.GUIA_GENERADA, asignado_a=None)

            # B: una pieza en dos medias cajas, en el corral.
            b, lb = _pedido("SEED-CAJAS-B", 1)
            _reservar(b, lb)
            _pickear_todo(b, lb)
            empacar(b, piso, b.peso_esperado_gr, [foto()])
            _caja(b, lb, 1, 1, fraccion_de=2)
            _caja(b, lb, 2, 1, fraccion_de=2)
            Pedido.objects.filter(pk=b.pk).update(estado=Pedido.GUIA_GENERADA, asignado_a=None)

            # C: por pickear, al frente de la cola (picking libre más viejo).
            c, lc = _pedido("SEED-CAJAS-C", 2)
            _reservar(c, lc)
            iniciar_picking(c, piso)
            _caja(c, lc, 1, 2, estado=Paquete.PLANEADO, con_guia=False)
            Pedido.objects.filter(pk=c.pk).update(asignado_a=None, creado=hace(30), ts_picking=hace(30))

            # D: entregado, con una reposición aprobada de 1 pieza (caja 2 repone a la caja 1), por pickear.
            d, ld = _pedido("SEED-CAJAS-D", 1, estado=Pedido.ENTREGADO)
            LineaPedido.objects.filter(pk=ld.pk).update(reservada=True, cantidad_pickeada=1, cantidad_despachada=1)
            ld.refresh_from_db()
            caja1 = _caja(d, ld, 1, 1, estado=Paquete.DESPACHADO)
            Guia.objects.filter(paquete=caja1).update(estado=Guia.ENTREGADO)
            incidencia = abrir_incidencia(cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_CLIENTE, pedido=d, texto="Seed: el comprador dice que no llegó")
            crear_compensacion(incidencia, "reposicion", piso, "mesa", lineas=[(ld, 1, caja1.pk)], aprobar=True, motivo="no_entregada")
            d.refresh_from_db()
            _caja(d, ld, 2, 1, estado=Paquete.PLANEADO, con_guia=False, repone_a=caja1)
            iniciar_picking(d, piso)
            Pedido.objects.filter(pk=d.pk).update(asignado_a=None, creado=hace(20), ts_picking=hace(20))

        self.stdout.write(self.style.SUCCESS(
            f"Sembrado. Producto {PRODUCTO}, lote {LOTE}, rack SEED-A3 (20 piezas: 13 vendibles, 4 en empaque, 3 apartadas).\n"
            f"  SEED-CAJAS-A (pk {a.pk}): 3 cajas de 1 pieza en el corral SAL-OTRO. En Salida palomea las cajas 1 y 2 y firma: el kardex resta 2 (queda 1 en empaque), luego la 3.\n"
            f"  SEED-CAJAS-B (pk {b.pk}): 1 pieza en dos medias cajas. Manifiesta la caja 1 sola: sale la unidad entera; la caja 2 después no resta nada.\n"
            f"  SEED-CAJAS-C (pk {c.pk}): en picking, libre y PRIMERO en la cola. Mi turno → EMPEZAR lo ofrece; o abre /piso/picking/{c.pk}/ con tu usuario de Mesa.\n"
            f"  SEED-CAJAS-D (pk {d.pk}): reposición aprobada ({incidencia.folio}), segundo en la cola. Pickéalo y empácalo; luego en Mesa → pedido → caja 2 → Cancelar reposición.\n"
            "  Kardex: Mesa → Inventario → SEED-C24 (una fila PICK/SALIDA por lote). Lotes por línea: Mesa → pedido → Líneas."
        ))
