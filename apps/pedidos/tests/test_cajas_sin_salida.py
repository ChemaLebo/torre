"""Cajas sin salida (Chema 2026-10-12): la guía lleva más de 24 h (sin contar
domingos), ya cerró una salida de su paquetería sin la caja y el carrier no la
reporta → incidencia interna "Caja sin salida" (una por pedido), que se
resuelve sola cuando ya nada queda por salir. Mesa puede darla por salida."""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import PerfilUsuario
from apps.envios.models import Guia, Manifiesto, Paquete, PaqueteLinea
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def _en(dia, hora):
    return timezone.make_aware(datetime(2026, 10, dia, hora, 0))


class CajasSinSalidaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.tienda = crear_tienda(self.cliente)
        self.sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", peso_gr=4000)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def _caja_lista(self, nacio, carrier="estafeta", numero=1, pedido=None):
        """Pedido con guía lista y una caja EMPACADO cuya guía nació en `nacio`."""
        if pedido is None:
            pedido = crear_pedido(self.cliente, self.tienda, estado=Pedido.GUIA_GENERADA, es_local=False)
            LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=1, reservada=True, cantidad_pickeada=1)
        caja = Paquete.objects.create(
            pedido=pedido, numero=numero, peso_kg=Decimal("4"), carrier=carrier, estado=Paquete.EMPACADO,
            ts_cierre=nacio, ts_etiquetas=nacio, ts_listo_salida=nacio,
        )
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=pedido.lineas.first(), cantidad=1)
        guia = Guia.objects.create(pedido=pedido, paquete=caja, carrier=carrier, numero=f"G-{pedido.pk}-{numero}", proveedor="mock", estado=Guia.GUIA_CREADA)
        Guia.objects.filter(pk=guia.pk).update(creado=nacio)
        return pedido, caja, guia

    def _salida(self, carrier, ts):
        hoja = Manifiesto.objects.create(carrier=carrier, corral="SAL-OTRO", chofer="Chofer")
        Manifiesto.objects.filter(pk=hoja.pk).update(ts=ts)
        return hoja

    def test_regla_24h_sin_domingos_manifiesto_y_sin_reporte_del_carrier(self):
        # Jueves 8 de octubre 10:00 nace la guía; viernes 9 a las 12:00 ya pasó un camión.
        pedido, caja, guia = self._caja_lista(_en(8, 10))
        self.assertEqual(services.cajas_sin_salida(_en(9, 9)), [])  # 23 h: todavía no
        self.assertEqual(services.cajas_sin_salida(_en(9, 12)), [])  # 26 h pero ningún camión pasó
        self._salida("estafeta", _en(9, 11))
        filas = services.cajas_sin_salida(_en(9, 12))
        self.assertEqual([(f["pedido"].pk, f["caja"].pk, f["horas"]) for f in filas], [(pedido.pk, caja.pk, 26)])
        self._salida("imile", _en(9, 11))  # una salida de OTRA paquetería no cuenta
        otro, _, _ = self._caja_lista(_en(8, 10), carrier="imile")
        Manifiesto.objects.filter(carrier="imile").update(ts=_en(8, 9))  # antes de que naciera: no cuenta
        self.assertEqual([f["pedido"].pk for f in services.cajas_sin_salida(_en(9, 12))], [pedido.pk])
        # El carrier la reportó: ya no es "sin salida".
        Guia.objects.filter(pk=guia.pk).update(ts_recolectado_carrier=_en(9, 11))
        self.assertEqual(services.cajas_sin_salida(_en(9, 12)), [])

    def test_una_caja_sin_confirmar_en_salida_no_es_caja_sin_salida(self):
        """Sin "Lista en salida" la caja no está en el corral: es Completar empaquetado, no CSS (2026-10-20)."""
        pedido, caja, guia = self._caja_lista(_en(8, 10))
        self._salida("estafeta", _en(9, 11))
        Paquete.objects.filter(pk=caja.pk).update(ts_listo_salida=None)
        self.assertEqual(services.cajas_sin_salida(_en(9, 12)), [])
        Paquete.objects.filter(pk=caja.pk).update(ts_listo_salida=_en(8, 10))
        self.assertEqual([f["caja"].pk for f in services.cajas_sin_salida(_en(9, 12))], [caja.pk])

    def test_el_domingo_no_cuenta(self):
        # Sábado 10 de octubre 18:00; el lunes 12 a las 17:00 son 47 h con un domingo en medio: faltan.
        pedido, caja, guia = self._caja_lista(_en(10, 18))
        self._salida("estafeta", _en(12, 11))
        self.assertEqual(services.cajas_sin_salida(_en(12, 17)), [])
        self.assertEqual(len(services.cajas_sin_salida(_en(12, 19))), 1)  # 49 h: 24 + 24 del domingo

    def test_abre_una_incidencia_por_pedido_y_la_resuelve_cuando_sale(self):
        pedido, caja, guia = self._caja_lista(_en(8, 10))
        self._caja_lista(_en(8, 10), numero=2, pedido=pedido)
        self._salida("estafeta", _en(9, 11))
        with patch("apps.mensajeria.services.notificar_cliente_incidencia") as avisar:
            resumen = services.revisar_cajas_sin_salida(_en(9, 12))
        self.assertEqual(resumen, {"abiertas": 1, "resueltas": 0})
        inc = Incidencia.objects.get(pedido=pedido)
        self.assertEqual((inc.tipo, inc.prioridad, inc.interna, inc.origen), ("CSS", "P1", True, "auto"))
        self.assertIn("caja 1 (guía", inc.mensajes.first().texto)
        self.assertIn("caja 2 (guía", inc.mensajes.first().texto)
        avisar.assert_not_called()  # interna: el cliente no se entera
        self.assertEqual(services.revisar_cajas_sin_salida(_en(9, 13)), {"abiertas": 0, "resueltas": 0})  # no duplica
        # Mesa da por salida la caja 1; la 2 sigue: la incidencia sigue abierta.
        with patch("apps.mensajeria.services.enviar_en_camino"), patch("apps.integraciones.services.marcar_fulfillment"), \
             patch("apps.inventario.services.despachar"), self.captureOnCommitCallbacks(execute=True):
            services.salida_sin_registro(pedido, caja, self.mesa)
        caja.refresh_from_db()
        pedido.refresh_from_db()
        inc.refresh_from_db()
        self.assertEqual((caja.estado, pedido.estado, inc.estado), (Paquete.DESPACHADO, Pedido.PARCIALMENTE_DESPACHADO, Incidencia.ABIERTA))
        evento = pedido.paquetes.get(numero=1)
        self.assertEqual(evento.estado, Paquete.DESPACHADO)
        # Sale la caja 2 (el carrier la reporta): el job resuelve la incidencia.
        caja2 = pedido.paquetes.get(numero=2)
        with patch("apps.mensajeria.services.enviar_en_camino"), patch("apps.integraciones.services.marcar_fulfillment"), \
             patch("apps.inventario.services.despachar"), self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(services.salida_por_carrier(pedido, caja2.guia_activa, _en(9, 15), "Picked up"))
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
        self.assertEqual(services.revisar_cajas_sin_salida(_en(9, 16)), {"abiertas": 0, "resueltas": 1})
        inc.refresh_from_db()
        self.assertEqual(inc.estado, Incidencia.RESUELTA)

    def test_salida_sin_registro_valida(self):
        pedido, caja, guia = self._caja_lista(_en(8, 10))
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EN_PICKING)
        pedido.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "no tiene cajas listas"):
            services.salida_sin_registro(pedido, caja, self.mesa)
