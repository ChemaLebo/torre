"""Base de pruebas del piso: bodega mínima del Local 380 E.

Arma cliente, operador, ubicaciones (recepción, picking y corrales SAL-*) y un
SKU de cerveza. El stock SIEMPRE se crea por la puerta oficial (recibir →
ubicar de inventario.services), nunca tocando Saldo directo: así las pruebas
validan el flujo real del contrato.
"""
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, PerfilUsuario
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.pedidos.models import LineaPedido, Pedido

_MEDIA_TEMPORAL = tempfile.mkdtemp(prefix="torre-piso-tests-")


@override_settings(MEDIA_ROOT=_MEDIA_TEMPORAL)
class PisoTestCase(TestCase):
    """Fixture común: bodega mínima + operador de piso logueable."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.cliente = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", buffer_stock=0,
        )
        cls.operador = User.objects.create_user("piso1", password="pin-piso")
        PerfilUsuario.objects.create(usuario=cls.operador, rol=PerfilUsuario.ROL_PISO, pin="1111")
        cls.usuario_portal = User.objects.create_user("karina", password="portal")
        PerfilUsuario.objects.create(
            usuario=cls.usuario_portal, rol=PerfilUsuario.ROL_PORTAL, cliente=cls.cliente,
        )

        cls.ubic_recepcion = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        cls.ubic_picking = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        for codigo, carriers in (("SAL-PQX", "paquetexpress"), ("SAL-LOCAL", "local"), ("SAL-OTRO", "")):
            Ubicacion.objects.create(codigo=codigo, tipo=Ubicacion.SALIDA, carriers=carriers)

        cls.sku = SKU.objects.create(
            cliente=cls.cliente,
            codigo="COLIMITA-SIX",
            codigo_barras="7501234567890",
            descripcion="Colimita six pack",
            peso_gr=2000,
            requiere_lote=False,
            precio_declarado=Decimal("180.00"),
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TEMPORAL, ignore_errors=True)

    # ── Helpers ──

    def login_piso(self):
        self.client.force_login(self.operador)

    def foto(self, nombre="foto.jpg"):
        return SimpleUploadedFile(nombre, b"foto-de-prueba", content_type="image/jpeg")

    def crear_stock(self, sku=None, cantidad=50):
        """Stock vendible por la puerta oficial: ASN → recibir → ubicar."""
        from apps.inventario.services import recibir, ubicar

        sku = sku or self.sku
        orden = OrdenEntrada.objects.create(cliente=sku.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=cantidad)
        recibir(linea, cantidad, 0, self.operador)
        ubicar(sku, cantidad, self.ubic_picking, None, self.operador)
        orden.refresh_from_db()
        return orden

    def crear_pedido(self, cantidad=3, es_local=False, estado=Pedido.PENDIENTE,
                     reservar_stock=True, **extra):
        """Pedido manual con una línea del SKU base; reserva stock si se pide."""
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            origen="manual",
            estado=estado,
            es_local=es_local,
            comprador_nombre="Ana Prueba",
            comprador_tel="+523121234567",
            cp="28017" if es_local else "44100",
            direccion={"address1": "Av. Prueba 123", "city": "Colima" if es_local else "Guadalajara"},
            peso_esperado_gr=self.sku.peso_gr * cantidad,
        )
        for campo, valor in extra.items():
            setattr(pedido, campo, valor)
        if extra:
            pedido.save()
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=cantidad)
        if reservar_stock:
            from apps.inventario.services import reservar
            if reservar(self.sku, cantidad, pedido.folio):
                linea.reservada = True
                linea.save(update_fields=["reservada"])
        return pedido

    def dejar_empacado(self, pedido):
        """Avanza el pedido hasta EMPACADO por los servicios oficiales.

        Contrato del carril único: al empacar solo se exige la foto del
        CONTENIDO (la de caja cerrada se toma después, con la etiqueta pegada
        — ver evidencia_cierre / cerrar_caja).
        """
        from apps.pedidos.services import confirmar_linea_pick, empacar, iniciar_picking

        if pedido.estado == Pedido.PENDIENTE:
            iniciar_picking(pedido, self.operador)
        for linea in pedido.lineas.all():
            faltan = linea.cantidad - linea.cantidad_pickeada
            if faltan > 0:
                confirmar_linea_pick(linea, faltan, self.operador)
        empacado = empacar(
            pedido, self.operador, pedido.peso_esperado_gr,
            [self.foto("contenido.jpg")],
        )
        empacado.refresh_from_db()
        return empacado

    def evidencia_cierre(self, pedido, nombre="cierre.jpg"):
        """Foto de caja cerrada (etiqueta pegada) ligada al pedido.

        Atajo de fixture para pedidos sin empaque por caja (cajas del plan en
        PLANEADO): equivale a la evidencia que cerrar_caja adjunta en el
        flujo por caja.
        """
        from apps.core.models import EvidenciaFoto

        return EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(pedido.pk), tipo="caja_cerrada",
            archivo=self.foto(nombre), tomada_por=self.operador.username,
        )
