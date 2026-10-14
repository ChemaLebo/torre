"""Sustituir guía para una caja que ya salió (Chema 2026-10-14; PED-00261):
la entrega falló y el carrier la entrega si le damos otra guía, en vez de
regresar el paquete y esperar una reposición. La vieja queda SUSTITUIDA (no
se cancela con el carrier, conserva su costo), la nueva apunta a ella, estrena
compromiso y es la que ve el comprador; la caja sigue despachada y el pedido
no cambia de estado. Shopify se actualiza encolado y en segundo plano."""
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.envios.services import guia_sustituible, sustituir_guia
from apps.envios.tests.base import crear_tienda
from apps.integraciones.models import EscrituraShopifyPendiente
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class SustituirGuiaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)

    def caja_en_la_calle(self, estado_guia=Guia.EN_TRANSITO, estado_pedido=Pedido.EN_TRANSITO):
        """Una caja con guía comprada (mock) que ya salió: despachada, guía en la calle."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        guia = caja.guia_activa
        Paquete.objects.filter(pk=caja.pk).update(estado=Paquete.DESPACHADO)
        Guia.objects.filter(pk=guia.pk).update(estado=estado_guia, fecha_compromiso=timezone.localdate())
        Pedido.objects.filter(pk=pedido.pk).update(estado=estado_pedido)
        pedido.refresh_from_db()
        caja.refresh_from_db()
        guia.refresh_from_db()
        return pedido, caja, guia

    def test_guia_nueva_para_la_misma_caja_sin_regresar_a_picking(self):
        pedido, caja, vieja = self.caja_en_la_calle()
        tienda = crear_tienda(self.cliente, token="shpat_prueba")
        Pedido.objects.filter(pk=pedido.pk).update(tienda=tienda, shopify_order_id="5555")
        Paquete.objects.filter(pk=caja.pk).update(shopify_fulfillment_id="gid://shopify/Fulfillment/F1")
        pedido.refresh_from_db()
        caja.refresh_from_db()
        self.assertEqual(guia_sustituible(caja), vieja)
        with patch("apps.integraciones.services.drenar_escrituras_en_segundo_plano") as drenar, \
             self.captureOnCommitCallbacks(execute=True):
            nueva = sustituir_guia(caja, "estafeta", "Retorno falso: 99minutos entrega con guía nueva", None)
        vieja.refresh_from_db()
        caja.refresh_from_db()
        pedido.refresh_from_db()
        self.assertTrue(nueva.numero.startswith("MOCK-"))
        self.assertNotEqual(nueva.numero, vieja.numero)
        self.assertEqual((nueva.paquete_id, nueva.sustituye_a_id, nueva.estado), (caja.pk, vieja.pk, Guia.GUIA_CREADA))
        self.assertEqual(nueva.sustitucion_motivo, "Retorno falso: 99minutos entrega con guía nueva")
        self.assertEqual((vieja.estado, vieja.es_activa, vieja.sustituida_por), (Guia.SUSTITUIDA, False, nueva))
        self.assertEqual(caja.guia_activa, nueva)
        self.assertIsNotNone(nueva.fecha_compromiso)  # corre desde hoy: la caja ya está en la calle
        self.assertGreaterEqual(nueva.fecha_compromiso, timezone.localdate())
        self.assertEqual((caja.estado, pedido.estado), (Paquete.DESPACHADO, Pedido.EN_TRANSITO))  # nada regresa a bodega
        self.assertGreater(nueva.costo_preferencial, 0)  # se cobra (finanzas: las dos)
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="guia_sustituida")
        self.assertEqual((evento.delta["caja"], evento.delta["anterior"], evento.delta["nueva"]), (1, vieja.numero, nueva.numero))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="guia", entidad_id=str(vieja.pk), accion="guia_sustituida").exists())
        # Shopify: el fulfillment de la caja apunta a la guía nueva, encolado y drenado en segundo plano.
        escritura = EscrituraShopifyPendiente.objects.get()
        self.assertEqual((escritura.accion, escritura.datos), ("tracking", {"caja": caja.pk, "fid": "gid://shopify/Fulfillment/F1", "guia": nueva.pk, "notificar": False}))
        drenar.assert_called_once_with(pedido)
        # La página de rastreo ve la guía nueva y avisa del cambio; la vieja ya no se consulta.
        from apps.rastreo.views import _paquetes
        paquete = _paquetes(pedido)[0]
        self.assertEqual(paquete["guia"], nueva.numero)
        self.assertIn("Cambiamos la guía de este paquete; la anterior ya no vale.", paquete["notas"])
        self.assertNotIn(vieja, Guia.objects.exclude(estado__in=list(Guia.ESTADOS_TERMINALES)))
        # Ya sustituida, se puede volver a sustituir (la nueva es la viva).
        self.assertEqual(guia_sustituible(caja), nueva)

    def test_reglas_de_cuando_aplica(self):
        pedido, caja, vieja = self.caja_en_la_calle()
        with self.assertRaises(ValueError) as ctx:
            sustituir_guia(caja, "estafeta", "", None)
        self.assertIn("motivo", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            sustituir_guia(caja, "local", "x", None)
        self.assertIn("paquetería de la lista", str(ctx.exception))
        Guia.objects.filter(pk=vieja.pk).update(sustituida_motivo="no_entregada", ts_sustituida=timezone.now())
        caja = Paquete.objects.get(pk=caja.pk)
        self.assertIsNone(guia_sustituible(caja))
        with self.assertRaises(ValueError) as ctx:
            sustituir_guia(caja, "estafeta", "x", None)
        self.assertIn("reposición en curso", str(ctx.exception))
        Guia.objects.filter(pk=vieja.pk).update(sustituida_motivo="", ts_sustituida=None, estado=Guia.ENTREGADO)
        caja = Paquete.objects.get(pk=caja.pk)
        self.assertIsNone(guia_sustituible(caja))
        with self.assertRaises(ValueError) as ctx:
            sustituir_guia(caja, "estafeta", "x", None)
        self.assertIn("ya se entregó", str(ctx.exception))
        Guia.objects.filter(pk=vieja.pk).update(estado=Guia.EN_TRANSITO, carrier="local")
        caja = Paquete.objects.get(pk=caja.pk)
        self.assertIsNone(guia_sustituible(caja))
        Guia.objects.filter(pk=vieja.pk).update(carrier="estafeta")
        Paquete.objects.filter(pk=caja.pk).update(estado=Paquete.EMPACADO)
        caja = Paquete.objects.get(pk=caja.pk)
        self.assertIsNone(guia_sustituible(caja))
        with self.assertRaises(ValueError) as ctx:
            sustituir_guia(caja, "estafeta", "x", None)
        self.assertIn("no ha salido", str(ctx.exception))
        Paquete.objects.filter(pk=caja.pk).update(estado=Paquete.DESPACHADO, cancelada_por_reembolso=timezone.now())
        caja = Paquete.objects.get(pk=caja.pk)
        self.assertIsNone(guia_sustituible(caja))
        self.assertFalse(EventoAuditoria.objects.filter(accion="guia_sustituida").exists())

    def test_en_retorno_tambien_y_la_direccion_pendiente_se_aplica_antes_de_comprar(self):
        pedido, caja, vieja = self.caja_en_la_calle(estado_guia=Guia.RETORNO, estado_pedido=Pedido.RETORNADO)
        self.assertIsNone(caja.guia_activa)  # un retorno desactiva la guía
        self.assertEqual(guia_sustituible(caja), vieja)
        direccion = {"address1": "Nueva calle 5", "city": "Guadalajara", "province_code": "JAL", "zip": "44100"}
        Pedido.objects.filter(pk=pedido.pk).update(direccion_pendiente=direccion)
        with self.captureOnCommitCallbacks(execute=True):
            nueva = sustituir_guia(caja, "estafeta", "El carrier la entrega con guía nueva", None)
        vieja.refresh_from_db()
        pedido.refresh_from_db()
        caja.refresh_from_db()
        self.assertEqual((vieja.estado, nueva.sustituye_a_id, caja.guia_activa), (Guia.RETORNO, vieja.pk, nueva))  # la vieja conserva su retorno
        self.assertEqual((pedido.direccion, pedido.direccion_pendiente, pedido.estado), (direccion, None, Pedido.RETORNADO))
        self.assertFalse(EscrituraShopifyPendiente.objects.exists())  # sin tienda: nada que apuntar en Shopify
