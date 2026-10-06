"""Reingresos en Mesa → Recepciones: la tarjeta de pedidos por decidir, los dos
botones, el badge del nav y la orden de reingreso vista desde el piso."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU
from apps.core.models import Cliente, PerfilUsuario
from apps.inventario.models import OrdenEntrada
from apps.pedidos.models import LineaPedido, Pedido


def crear_usuario(username, rol, pin=""):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, pin=pin)
    return user


class ReingresosMesaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.sku = SKU.objects.create(cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.piso = crear_usuario("piso1", "piso")
        cls.url = reverse("mesa:recepciones")

    def setUp(self):
        self.client.force_login(self.mesa)
        self.pedido = Pedido.objects.create(
            cliente=self.colima, tienda=None, origen="manual", comprador_nombre="Ana", estado=Pedido.RETORNADO,
        )
        LineaPedido.objects.create(pedido=self.pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2)

    def test_tarjeta_badge_y_botones(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Reingresos por decidir")
        self.assertContains(respuesta, self.pedido.folio)
        self.assertContains(respuesta, "Registrar reingreso")
        self.assertContains(respuesta, "Inventario no recuperado")
        self.assertContains(respuesta, 'Recepciones <span class="pill warn">1</span>')

    def test_cancelacion_tardia_enlaza_su_incidencia(self):
        from apps.incidencias.models import Incidencia

        tardia = Pedido.objects.create(
            cliente=self.colima, tienda=None, origen="manual", comprador_nombre="Eva", estado=Pedido.EN_TRANSITO,
        )
        Pedido.objects.filter(pk=tardia.pk).update(cancelacion_tardia=True)
        inc = Incidencia.objects.create(cliente=self.colima, pedido=tardia, tipo="CAN", origen="auto")
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, reverse("mesa:incidencia_detalle", args=[inc.pk]))
        self.assertContains(respuesta, inc.folio)
        # Ya CANCELADO (la CAN se resolvió antes) sigue en la lista hasta decidir la mercancía.
        Pedido.objects.filter(pk=tardia.pk).update(estado=Pedido.CANCELADO)
        self.assertContains(self.client.get(self.url), tardia.folio)

    def test_registrar_reingreso_crea_la_orden_y_el_piso_la_ve(self):
        respuesta = self.client.post(self.url, {"accion": "reingreso", "pedido_id": self.pedido.pk}, follow=True)
        self.assertContains(respuesta, "creado para")
        orden = OrdenEntrada.objects.get(tipo="reingreso", pedido=self.pedido)
        self.assertContains(respuesta, orden.folio)
        self.assertNotContains(respuesta, "Reingresos por decidir")
        self.client.force_login(self.piso)
        lista = self.client.get(reverse("piso:recepciones"))
        self.assertContains(lista, orden.folio)
        self.assertContains(lista, "reingreso · " + self.pedido.folio)
        detalle = self.client.get(reverse("piso:recepcion_detalle", args=[orden.pk]))
        self.assertContains(detalle, "Mercancía que vuelve de un pedido")

    def test_no_recuperado_exige_motivo_y_luego_cierra(self):
        respuesta = self.client.post(self.url, {"accion": "no_recuperado", "pedido_id": self.pedido.pk}, follow=True)
        self.assertContains(respuesta, "Escribe el motivo")
        respuesta = self.client.post(self.url, {
            "accion": "no_recuperado", "pedido_id": self.pedido.pk, "motivo": "Perdido en tránsito",
        }, follow=True)
        self.assertContains(respuesta, "no recuperado")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.reingreso_estado, Pedido.NO_RECUPERADO)
        self.assertEqual(OrdenEntrada.objects.count(), 0)

    def test_piso_no_decide(self):
        self.client.force_login(self.piso)
        self.assertEqual(self.client.post(self.url, {"accion": "reingreso", "pedido_id": self.pedido.pk}).status_code, 403)


class ReingresoPorCajaMesaTests(ReingresosMesaTests):
    """PED-00067 (2026-10-06): la caja regresada aparece por decidir con su
    número aunque el pedido esté entregado; el reingreso lleva solo esa caja."""

    def setUp(self):
        from decimal import Decimal

        from apps.envios.models import Guia, Paquete, PaqueteLinea

        super().setUp()
        self.pedido.delete()  # el retornado entero del fixture base; aquí el caso es por caja
        self.pedido = Pedido.objects.create(
            cliente=self.colima, tienda=None, origen="manual", comprador_nombre="Ana", estado=Pedido.ENTREGADO,
        )
        linea = LineaPedido.objects.create(pedido=self.pedido, sku=self.sku, cantidad=4, cantidad_pickeada=4, cantidad_despachada=4, reservada=True)
        c1 = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("5"), carrier="imile", estado=Paquete.DESPACHADO)
        c2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=Decimal("5"), carrier="imile", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=2)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=2)
        Guia.objects.create(pedido=self.pedido, paquete=c1, carrier="imile", numero="IM-1", estado=Guia.ENTREGADO)
        Guia.objects.create(pedido=self.pedido, paquete=c2, carrier="imile", numero="IM-2", estado=Guia.RETORNO)

    def test_la_caja_regresada_aparece_y_su_reingreso_lleva_solo_esa_caja(self):
        from apps.inventario.models import OrdenEntrada

        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, self.pedido.folio)
        self.assertContains(respuesta, "caja 2 de 2")
        self.assertContains(respuesta, "regresada por el carrier")
        respuesta = self.client.post(self.url, {"pedido_id": self.pedido.pk, "accion": "reingreso"}, follow=True)
        self.assertContains(respuesta, "creado para " + self.pedido.folio)
        orden = OrdenEntrada.objects.get(pedido=self.pedido, tipo=OrdenEntrada.TIPO_REINGRESO)
        self.assertEqual(orden.lineas.get().cantidad_anunciada, 2)
        self.assertNotContains(respuesta, "Reingresos por decidir")
