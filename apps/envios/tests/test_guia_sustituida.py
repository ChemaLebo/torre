"""Guía sustituida por una reposición (Chema 2026-09-29): al aprobar la
reposición, las guías de las cajas que contenían lo repuesto quedan marcadas
con el motivo; el poller las sigue, pero una entrega tardía es "entrega
duplicada" en la incidencia y no mueve el pedido; se ven en los expedientes."""
from decimal import Decimal

from django.test import TestCase

from apps.catalogo.models import SKU
from apps.core.models import EventoAuditoria
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.services import _aplicar_efectos, guias_del_pedido
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Compensacion, Incidencia
from apps.incidencias.services import abrir_incidencia, marcar_guias_sustituidas
from apps.pedidos.models import LineaPedido, Pedido


class GuiaSustituidaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente, crear_tienda(self.cliente), estado=Pedido.ENTREGADO)
        sku = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", precio_declarado=Decimal("300"))
        self.linea = LineaPedido.objects.create(pedido=self.pedido, sku=sku, cantidad=1, cantidad_despachada=1, reservada=True)
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=Decimal("4"), carrier="imile", estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=self.caja, linea_pedido=self.linea, cantidad=1)
        self.guia = Guia.objects.create(pedido=self.pedido, paquete=self.caja, carrier="imile", numero="IM-1", proveedor="envia",
                                        estado=Guia.RECOLECTADO)
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="Rota")
        self.comp = Compensacion.objects.create(
            incidencia=self.inc, tipo=Compensacion.TIPO_REPOSICION, monto=Decimal("300"), motivo="danada",
            lineas=[{"linea_id": self.linea.pk, "sku": "SIX", "cantidad": 1}], estado=Compensacion.APROBADA,
        )

    def test_marca_las_guias_de_las_cajas_repuestas_y_las_lista_con_su_caja_nueva(self):
        marcadas = marcar_guias_sustituidas(self.comp, actor=None)
        self.guia.refresh_from_db()
        self.assertEqual([g.pk for g in marcadas], [self.guia.pk])
        self.assertEqual(self.guia.sustituida_motivo, "danada")
        self.assertTrue(self.guia.sustituida)
        self.assertIsNotNone(self.guia.ts_sustituida)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(self.guia.pk), accion="guia_sustituida").exists())
        # La caja de reposición que la cubre, cuando existe:
        repuesta = LineaPedido.objects.create(pedido=self.pedido, sku=self.linea.sku, cantidad=1, reposicion_de=self.linea)
        caja3 = Paquete.objects.create(pedido=self.pedido, numero=3, peso_kg=Decimal("4"), carrier="local")
        PaqueteLinea.objects.create(paquete=caja3, linea_pedido=repuesta, cantidad=1)
        self.assertEqual(self.guia.cajas_reposicion, [3])
        self.assertEqual(guias_del_pedido(self.pedido)[0]["sustituida"], "sustituida · paquete dañado · por caja 3")
        self.assertEqual(marcar_guias_sustituidas(self.comp, actor=None), [])  # no se marca dos veces

    def test_entrega_tardia_es_duplicada_y_no_mueve_el_pedido(self):
        marcar_guias_sustituidas(self.comp, actor=None)
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.EN_TRANSITO)
        self.pedido.refresh_from_db()
        self.guia.refresh_from_db()
        self.guia.estado = Guia.ENTREGADO
        self.guia.save(update_fields=["estado"])
        _aplicar_efectos(self.guia, Guia.ENTREGADO, "Entregado en domicilio")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_TRANSITO)  # no se mueve
        nota = self.inc.mensajes.order_by("-pk").first()
        self.assertIn("Entrega duplicada", nota.texto)
        self.assertIn("IM-1", nota.texto)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(self.guia.pk), accion="entrega_duplicada").exists())
