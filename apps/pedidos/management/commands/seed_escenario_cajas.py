"""Siembra en la base LOCAL el escenario del PED-00281 para probar a mano el
fix del manifiesto por caja y el lote por pedido (Chema 2026-10-21):

- SEED-CAJAS-A: un pedido con tres cajas del mismo producto con lote, las tres
  empacadas, con guía y confirmadas en el corral SAL-OTRO. Manifiesta dos
  (Salida → palomear cajas 1 y 2): el kardex debe restar 2, no 3; luego la
  tercera; después "Quitar de salida" regresa la pieza a su posición con su lote.
- SEED-CAJAS-B: un pedido de UNA pieza reempacada en dos medias cajas: al
  manifestar la primera mitad sale la unidad entera (regla de medias cajas).

Solo corre con DEBUG=True (nunca en producción). Idempotente: borra y vuelve a
sembrar sus propios pedidos (folio SEED-CAJAS-*), su producto SEED-C24 y sus
posiciones si ya existían. Usa los servicios reales de inventario y picking.
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

PRODUCTO = "SEED-C24"
LOTE = "SEEDLOTE"


class Command(BaseCommand):
    help = "Siembra el escenario de tres cajas del mismo producto (y medias cajas) para probar el manifiesto por caja en local."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Solo en local: este seed necesita DEBUG=True.")
        from django.contrib.auth import get_user_model

        from apps.catalogo.models import SKU, Lote, Ubicacion
        from apps.catalogo.services import asignar_producto
        from apps.core.models import Cliente, PerfilUsuario
        from apps.envios.models import Guia, Paquete, PaqueteLinea
        from apps.inventario.models import LineaASN, OrdenEntrada
        from apps.inventario.services import recibir, reservar, ubicar
        from apps.pedidos.models import LineaPedido, Pedido
        from apps.pedidos.services import confirmar_linea_pick, empacar, iniciar_picking

        with transaction.atomic():
            cliente = Cliente.objects.filter(activo=True).order_by("pk").first()
            if cliente is None:
                cliente = Cliente.objects.create(nombre="Cliente demo", slug="demo", integracion_envios="envia")
            User = get_user_model()
            piso = User.objects.filter(perfil__rol="piso").first()
            if piso is None:
                piso = User.objects.create_user("seed-piso", password="seed-piso")
                PerfilUsuario.objects.create(usuario=piso, rol="piso", pin="9999")
            # Limpieza de una siembra anterior (la guía protege al pedido: primero ella).
            Guia.objects.filter(pedido__folio__startswith="SEED-CAJAS-").delete()
            Pedido.objects.filter(folio__startswith="SEED-CAJAS-").delete()
            rec = Ubicacion.objects.filter(tipo=Ubicacion.RECEPCION, activo=True).first() or Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
            rack, _ = Ubicacion.objects.get_or_create(codigo="SEED-A3", defaults={"tipo": Ubicacion.PICKING, "largo_cm": 180, "ancho_cm": 58, "alto_cm": 52})
            if not Ubicacion.objects.filter(tipo=Ubicacion.SALIDA, activo=True).exists():
                Ubicacion.objects.create(codigo="SAL-OTRO", tipo=Ubicacion.SALIDA)
            sku, _ = SKU.objects.get_or_create(
                cliente=cliente, codigo=PRODUCTO,
                defaults={"codigo_barras": "7509999000024", "descripcion": "Seed caja 24 (prueba manifiesto)", "peso_gr": 9000,
                          "requiere_lote": True, "precio_declarado": Decimal("500")},
            )
            lote, _ = Lote.objects.get_or_create(sku=sku, codigo=LOTE, defaults={"fecha_caducidad": "2027-06-01"})
            # Stock limpio: 10 piezas del lote en el rack fijo del producto.
            orden = OrdenEntrada.objects.create(cliente=cliente)
            linea_asn = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=10, lote_codigo=LOTE)
            recibir(linea_asn, 10, 0, piso)
            ubicar(sku, 10, rack, lote, piso)
            asignar_producto(rack, sku, actor="seed")

            def _pedido(folio, cantidad):
                pedido = Pedido.objects.create(
                    cliente=cliente, origen="manual", folio=folio, estado=Pedido.PENDIENTE, comprador_nombre="Prueba manifiesto",
                    comprador_tel="3120000000", cp="44100", direccion={"address1": "Av. Prueba 1", "city": "Guadalajara"},
                    peso_esperado_gr=sku.peso_gr * cantidad,
                )
                linea = LineaPedido.objects.create(pedido=pedido, sku=sku, cantidad=cantidad)
                if not reservar(sku, cantidad, pedido.folio):
                    raise CommandError("No alcanzó el stock sembrado para reservar.")
                linea.reservada = True
                linea.save(update_fields=["reservada"])
                iniciar_picking(pedido, piso)
                for _ in range(cantidad):  # un escaneo por pieza, con posición y lote como el QR
                    confirmar_linea_pick(linea, 1, piso, ubicacion=rack, lote=lote)
                return pedido, linea

            def _caja(pedido, linea, numero, cantidad, fraccion_de=1):
                caja = Paquete.objects.create(pedido=pedido, numero=numero, peso_kg=Decimal("9"), carrier="estafeta", estado=Paquete.EMPACADO)
                PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=cantidad, fraccion_de=fraccion_de)
                Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero=f"{pedido.folio}-C{numero}", proveedor="mock", estado=Guia.GUIA_CREADA)
                ahora = timezone.now()
                Paquete.objects.filter(pk=caja.pk).update(ts_etiquetas=ahora, ts_cierre=ahora, ts_listo_salida=ahora)
                return caja

            from django.core.files.uploadedfile import SimpleUploadedFile
            foto = lambda: SimpleUploadedFile("seed.jpg", b"seed", content_type="image/jpeg")  # noqa: E731

            a, la = _pedido("SEED-CAJAS-A", 3)
            empacar(a, piso, a.peso_esperado_gr, [foto()])
            for n in (1, 2, 3):
                _caja(a, la, n, 1)
            Pedido.objects.filter(pk=a.pk).update(estado=Pedido.GUIA_GENERADA, asignado_a=None)

            b, lb = _pedido("SEED-CAJAS-B", 1)
            empacar(b, piso, b.peso_esperado_gr, [foto()])
            _caja(b, lb, 1, 1, fraccion_de=2)
            _caja(b, lb, 2, 1, fraccion_de=2)
            Pedido.objects.filter(pk=b.pk).update(estado=Pedido.GUIA_GENERADA, asignado_a=None)

        self.stdout.write(self.style.SUCCESS(
            "Sembrado. Producto SEED-C24, lote SEEDLOTE, rack SEED-A3 (10 piezas: 6 vendibles + 4 en empaque).\n"
            "  SEED-CAJAS-A: 3 cajas de 1 pieza, en el corral. En Salida palomea las cajas 1 y 2 y firma: el kardex debe restar 2 (queda 1 en empaque, SEED-A3 · SEEDLOTE),\n"
            "    luego la caja 3 (resta 1). En Mesa → pedido, 'Quitar de salida' de una caja la regresa a SEED-A3 con su lote (antes: corral sin lote).\n"
            "  SEED-CAJAS-B: 1 pieza en dos medias cajas. Manifiesta la caja 1 sola: sale la unidad entera (en empaque queda 0); la caja 2 después no resta nada.\n"
            "  Revisa el kardex en Mesa → Inventario → producto SEED-C24 (una fila SALIDA por lote) y la columna Lotes del pedido."
        ))
