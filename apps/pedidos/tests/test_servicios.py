"""Servicios de pedidos: ingesta idempotente, empaque validado, salida y cancelación.

Los servicios de otras apps (inventario, incidencias, mensajeria, envios) se
mockean: aquí se prueba el contrato del lado de pedidos.
"""
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente, EventoAuditoria, EvidenciaFoto
from apps.integraciones.models import Tienda
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def payload_shopify(order_id=5501234, sku="COL-SIX", cantidad=2, cp="28017", cancelado=False):
    payload = {
        "id": order_id,
        "name": f"#{order_id}",
        "email": "comprador@example.com",
        "financial_status": "paid",
        "total_price": "378.00",
        "note": "",
        "customer": {"first_name": "Luis", "last_name": "Mendoza", "phone": "+523121234567"},
        "shipping_address": {
            "name": "Luis Mendoza",
            "address1": "Av. Constitución 380",
            "city": "Colima",
            "province": "Colima",
            "zip": cp,
            "country": "México",
            "phone": "+523121234567",
        },
        "line_items": [{"sku": sku, "quantity": cantidad, "price": "189.00", "title": "Colimita six"}],
    }
    if cancelado:
        payload["cancelled_at"] = "2026-07-01T12:00:00-06:00"
    return payload


def foto(nombre="evidencia.jpg"):
    return SimpleUploadedFile(nombre, b"bytes-de-foto", content_type="image/jpeg")


class BaseServicios(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        # location_id vacío: la suite ejercita el camino legado (orden completa,
        # sin consulta de fulfillment orders). El camino acotado a nuestro
        # ticket tiene su propia clase (IngestaPorTicketTests) con el seam
        # de integraciones parchado.
        cls.tienda = Tienda.objects.create(
            cliente=cls.cliente,
            plataforma="shopify",
            dominio="colima-mx.myshopify.com",
            token="tok-demo",
            location_id="",
        )
        cls.sku = SKU.objects.create(
            cliente=cls.cliente,
            codigo="COL-SIX",
            codigo_barras="7501234567890",
            descripcion="Colimita six pack",
            peso_gr=2400,
            largo_cm=25,
            ancho_cm=17,
            alto_cm=24,
            unidad="six",
            precio_declarado=Decimal("189.00"),
            punto_reorden=10,
        )

    def pedido_directo(self, estado=Pedido.PENDIENTE, **extra):
        return Pedido.objects.create(
            cliente=self.cliente,
            tienda=None,
            origen="manual",
            comprador_nombre="Ana Prueba",
            cp="28017",
            es_local=True,
            estado=estado,
            **extra,
        )


class IngestaTests(BaseServicios):
    def _ingerir(self, payload, reservar_ok=True):
        # captureOnCommitCallbacks: la plantilla A sale en transaction.on_commit
        # (jamás dentro del atomic de la ingesta) — aquí se ejecuta el commit
        # simulado para poder afirmar sobre el envío.
        with patch("apps.inventario.services.reservar", return_value=reservar_ok) as reservar, \
             patch("apps.mensajeria.services.enviar_confirmacion") as confirmacion, \
             patch("apps.incidencias.services.abrir_incidencia") as abrir, \
             self.captureOnCommitCallbacks(execute=True):
            pedido = services.ingerir_pedido_shopify(self.tienda, payload)
        return pedido, reservar, confirmacion, abrir

    def test_ingesta_crea_pedido_con_lineas_y_reserva(self):
        pedido, reservar, confirmacion, abrir = self._ingerir(payload_shopify())
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertEqual(pedido.cliente, self.cliente)
        self.assertEqual(pedido.lineas.count(), 1)
        linea = pedido.lineas.get()
        self.assertEqual(linea.sku, self.sku)
        self.assertEqual(linea.cantidad, 2)
        self.assertTrue(linea.reservada)
        self.assertEqual(pedido.valor_declarado, Decimal("378.00"))
        self.assertEqual(pedido.peso_esperado_gr, 4800)
        reservar.assert_called_once_with(self.sku, 2, pedido.folio)
        confirmacion.assert_called_once()
        abrir.assert_not_called()

    def test_ingesta_respeta_current_quantity(self):
        """Orden que llega ya parcialmente reembolsada: se surte lo que QUEDA."""
        payload = payload_shopify()
        payload["line_items"][0]["current_quantity"] = 1  # pidió 2, le reembolsaron 1
        pedido, reservar, _, _ = self._ingerir(payload)
        linea = pedido.lineas.get()
        self.assertEqual(linea.cantidad, 1)
        self.assertEqual(pedido.peso_esperado_gr, 2400)
        reservar.assert_called_once_with(self.sku, 1, pedido.folio)

    def test_linea_totalmente_removida_no_crea_linea(self):
        payload = payload_shopify()
        payload["line_items"].append({
            "sku": "COL-SIX", "quantity": 1, "current_quantity": 0,
            "price": "189.00", "title": "Colimita six",
        })
        pedido, _, _, _ = self._ingerir(payload)
        self.assertEqual(pedido.lineas.count(), 1)  # solo la línea viva

    def test_ingesta_idempotente_no_duplica(self):
        with patch("apps.inventario.services.reservar", return_value=True) as reservar, \
             patch("apps.mensajeria.services.enviar_confirmacion"):
            primero = services.ingerir_pedido_shopify(self.tienda, payload_shopify())
            segundo = services.ingerir_pedido_shopify(self.tienda, payload_shopify())
        self.assertEqual(primero.pk, segundo.pk)
        self.assertEqual(Pedido.objects.count(), 1)
        self.assertEqual(LineaPedido.objects.count(), 1)
        # La segunda ingesta no re-reserva stock.
        self.assertEqual(reservar.call_count, 1)

    def test_sin_stock_abre_incidencia_fal_y_queda_pendiente(self):
        pedido, reservar, confirmacion, abrir = self._ingerir(payload_shopify(), reservar_ok=False)
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertTrue(pedido.incidencia_activa)
        linea = pedido.lineas.get()
        self.assertFalse(linea.reservada)
        abrir.assert_called_once()
        self.assertEqual(abrir.call_args[0][1], "FAL")

    def test_es_local_por_cp_cdmx(self):
        # Bodega en Olivar de los Padres (01780): local = CDMX (prefijos 00-16).
        local, *_ = self._ingerir(payload_shopify(order_id=1001, cp="06700"))
        local2, *_ = self._ingerir(payload_shopify(order_id=1003, cp="16010"))
        foraneo, *_ = self._ingerir(payload_shopify(order_id=1002, cp="28017"))
        foraneo2, *_ = self._ingerir(payload_shopify(order_id=1004, cp="44100"))
        self.assertTrue(local.es_local)
        self.assertTrue(local2.es_local)
        self.assertFalse(foraneo.es_local)
        self.assertFalse(foraneo2.es_local)

    def test_estampa_corte_vigente_desde_settings(self):
        pedido, *_ = self._ingerir(payload_shopify(order_id=1003))
        hora, minuto = str(settings.TORRE["CORTE_CONTRACTUAL"]).split(":")
        self.assertIsNotNone(pedido.corte_vigente_al_ingreso)
        self.assertEqual(pedido.corte_vigente_al_ingreso.hour, int(hora))
        self.assertEqual(pedido.corte_vigente_al_ingreso.minute, int(minuto))

    def test_orden_nueva_ya_fulfilled_no_se_ingiere(self):
        payload = payload_shopify()
        payload["fulfillment_status"] = "fulfilled"
        resultado, reservar, confirmacion, _ = self._ingerir(payload)
        self.assertIsNone(resultado)
        self.assertEqual(Pedido.objects.count(), 0)
        reservar.assert_not_called()
        confirmacion.assert_not_called()
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id="5501234", accion="ingesta_omitida_fulfilled",
            ).exists()
        )

    def test_orden_conocida_que_vuelve_fulfilled_sigue_idempotente(self):
        # Nuestro propio write-back bumpéa updated_at: la orden regresa por el
        # sync ya fulfilled y NO debe desaparecer ni duplicarse.
        primero, _, _, _ = self._ingerir(payload_shopify())
        payload = payload_shopify()
        payload["fulfillment_status"] = "fulfilled"
        segundo, _, _, _ = self._ingerir(payload)
        self.assertEqual(segundo.pk, primero.pk)
        self.assertEqual(Pedido.objects.count(), 1)

    def test_orden_nueva_parcialmente_fulfilled_no_se_ingiere(self):
        payload = payload_shopify()
        payload["fulfillment_status"] = "partial"
        resultado, reservar, _, _ = self._ingerir(payload)
        self.assertIsNone(resultado)
        self.assertEqual(Pedido.objects.count(), 0)
        reservar.assert_not_called()

    def test_orden_no_pagada_no_se_ingiere(self):
        payload = payload_shopify()
        payload["financial_status"] = "pending"
        resultado, reservar, confirmacion, _ = self._ingerir(payload)
        self.assertIsNone(resultado)
        self.assertEqual(Pedido.objects.count(), 0)
        reservar.assert_not_called()
        confirmacion.assert_not_called()
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id="5501234", accion="ingesta_omitida_pago",
            ).exists()
        )

    def test_orden_pendiente_entra_sola_cuando_se_paga(self):
        pendiente = payload_shopify()
        pendiente["financial_status"] = "pending"
        self._ingerir(pendiente)
        self.assertEqual(Pedido.objects.count(), 0)
        # El pago bumpea updated_at y la orden regresa por sync/webhook: ahora entra.
        pagado, _, _, _ = self._ingerir(payload_shopify())
        self.assertIsNotNone(pagado)
        self.assertEqual(Pedido.objects.count(), 1)

    def test_reembolso_parcial_cuenta_como_pagado(self):
        payload = payload_shopify()
        payload["financial_status"] = "partially_refunded"
        pedido, _, _, _ = self._ingerir(payload)
        self.assertIsNotNone(pedido)
        self.assertEqual(Pedido.objects.count(), 1)

    def test_payload_sin_financial_status_no_se_ingiere(self):
        # Fail-closed: un payload sin el campo no es una orden Shopify sana.
        payload = payload_shopify()
        payload.pop("financial_status")
        resultado, _, _, _ = self._ingerir(payload)
        self.assertIsNone(resultado)
        self.assertEqual(Pedido.objects.count(), 0)

    def test_payload_sin_id_truena(self):
        with self.assertRaises(ValueError):
            services.ingerir_pedido_shopify(self.tienda, {"line_items": []})


class LineasKitTests(BaseServicios):
    """es_kit: real para picking, invisible para inventario (7A·1)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kit = SKU.objects.create(
            cliente=cls.cliente, codigo="TEABOX", descripcion="TeaBox mensual",
            peso_gr=400, es_kit=True, requiere_lote=False,
        )

    def _payload_kit_y_normal(self, order_id):
        payload = payload_shopify(order_id=order_id)
        payload["line_items"] = [
            {"id": 11, "sku": "TEABOX", "quantity": 1, "price": "499.00", "title": "TeaBox"},
            {"id": 12, "sku": "COL-SIX", "quantity": 2, "price": "189.00", "title": "Six"},
        ]
        return payload

    def _ingerir(self, payload, reservar_ok=True):
        with patch("apps.inventario.services.reservar", return_value=reservar_ok) as reservar, \
             patch("apps.mensajeria.services.enviar_confirmacion"), \
             patch("apps.incidencias.services.abrir_incidencia") as abrir, \
             self.captureOnCommitCallbacks(execute=True):
            pedido = services.ingerir_pedido_shopify(self.tienda, payload)
        return pedido, reservar, abrir

    def test_kit_no_reserva_y_no_bloquea(self):
        pedido, reservar, abrir = self._ingerir(self._payload_kit_y_normal(8101))
        linea_kit = pedido.lineas.get(sku=self.kit)
        self.assertTrue(linea_kit.reservada)  # bookkeeping: "no bloquea"
        reservar.assert_called_once_with(self.sku, 2, pedido.folio)  # solo la normal
        abrir.assert_not_called()
        self.assertEqual(pedido.peso_esperado_gr, 400 + 4800)

    def test_sin_stock_el_fal_es_solo_de_la_linea_normal(self):
        pedido, _, abrir = self._ingerir(self._payload_kit_y_normal(8102), reservar_ok=False)
        abrir.assert_called_once()
        texto = abrir.call_args.kwargs.get("texto") or abrir.call_args.args[-1]
        self.assertIn("COL-SIX", texto)
        self.assertNotIn("TEABOX", texto)

    def test_cancelar_pendiente_no_libera_el_kit(self):
        pedido, _, _ = self._ingerir(self._payload_kit_y_normal(8103))
        with patch("apps.inventario.services.liberar_reserva") as liberar:
            services.cancelar(pedido, actor="mesa1", motivo="prueba")
        liberar.assert_called_once_with(self.sku, 2, pedido.folio)  # jamás el kit
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)

    def _pedido_picado(self):
        pedido = self.pedido_directo(estado=Pedido.EN_PICKING)
        LineaPedido.objects.create(
            pedido=pedido, sku=self.kit, cantidad=1, cantidad_pickeada=1, reservada=True,
        )
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=1, cantidad_pickeada=1, reservada=True,
        )
        return pedido

    def test_empacar_no_confirma_el_kit_en_inventario(self):
        pedido = self._pedido_picado()
        kit_linea = pedido.lineas.get(sku=self.kit)
        # El gate de 7A·2 exige contenido: hija declarada (riel normal).
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2,
            reservada=True, parte_de_kit=kit_linea,
        )
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar(pedido, "packer1", peso_real_gr=2800, fotos=[foto()])
        skus_confirmados = [c.args[0] for c in confirmar.call_args_list]
        self.assertNotIn(self.kit, skus_confirmados)  # el kit jamás toca inventario
        self.assertEqual(confirmar.call_count, 2)  # la línea normal y la hija
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)

    def test_recolectado_no_despacha_el_kit(self):
        pedido = self._pedido_picado()
        pedido.estado = Pedido.GUIA_GENERADA
        pedido.save(update_fields=["estado"])
        with patch("apps.inventario.services.despachar") as despachar, \
             patch("apps.mensajeria.services.enviar_en_camino"), \
             self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, "salida1")
        despachar.assert_called_once_with(self.sku, 1, pedido.folio)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)


class ContenidoKitTests(BaseServicios):
    """Declaración del contenido del kit en empaque (7A·2): hijas por los rieles normales."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kit = SKU.objects.create(
            cliente=cls.cliente, codigo="TEABOX", descripcion="TeaBox mensual",
            peso_gr=400, es_kit=True, requiere_lote=False,
        )
        cls.te = SKU.objects.create(
            cliente=cls.cliente, codigo="TE-CHAI", descripcion="Chai masala",
            peso_gr=120, requiere_lote=False,
        )

    def _pedido_con_kit(self, estado=Pedido.EN_PICKING):
        pedido = self.pedido_directo(estado=estado)
        linea = LineaPedido.objects.create(
            pedido=pedido, sku=self.kit, cantidad=1, cantidad_pickeada=1, reservada=True,
        )
        return pedido, linea

    def test_declarar_reserva_y_crea_hijas_pickeadas(self):
        pedido, kit = self._pedido_con_kit()
        with patch("apps.inventario.services.reservar", return_value=True) as reservar:
            services.declarar_contenido_kit(kit, [(self.te, 2), (self.sku, 1)], "packer1")
        hijas = list(kit.componentes.select_related("sku"))
        self.assertEqual(len(hijas), 2)
        for hija in hijas:
            self.assertEqual(hija.cantidad_pickeada, hija.cantidad)  # nacen pickeadas
            self.assertTrue(hija.reservada)
        reservar.assert_any_call(self.te, 2, pedido.folio)
        pedido.refresh_from_db()
        self.assertEqual(pedido.peso_esperado_gr, 120 * 2 + 2400)
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(pedido.pk), accion="kit_contenido",
        )
        self.assertEqual(evento.delta["kit"], "TEABOX")

    def test_sin_stock_de_un_te_nada_se_declara(self):
        _, kit = self._pedido_con_kit()
        with patch("apps.inventario.services.reservar", side_effect=[True, False]):
            with self.assertRaises(ValueError):
                services.declarar_contenido_kit(kit, [(self.te, 2), (self.sku, 1)], "packer1")
        self.assertEqual(kit.componentes.count(), 0)  # completo-o-nada

    def test_empacar_bloqueado_sin_contenido_y_fluye_con_el(self):
        pedido, kit = self._pedido_con_kit()
        with self.assertRaises(ValueError) as ctx:
            services.empacar(pedido, "packer1", peso_real_gr=500, fotos=[foto()])
        self.assertIn("Declara el contenido", str(ctx.exception))
        with patch("apps.inventario.services.reservar", return_value=True):
            services.declarar_contenido_kit(kit, [(self.te, 3)], "packer1")
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar(pedido, "packer1", peso_real_gr=360, fotos=[foto()])
        confirmar.assert_called_once_with(self.te, 3, pedido.folio)  # la hija, jamás el kit
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)

    def test_quitar_libera_y_borra_las_hijas(self):
        pedido, kit = self._pedido_con_kit()
        with patch("apps.inventario.services.reservar", return_value=True):
            services.declarar_contenido_kit(kit, [(self.te, 3)], "packer1")
        with patch("apps.inventario.services.liberar_reserva") as liberar, \
             self.captureOnCommitCallbacks(execute=True):
            services.quitar_contenido_kit(kit, "packer1")
        liberar.assert_called_once_with(self.te, 3, pedido.folio)
        self.assertEqual(kit.componentes.count(), 0)
        pedido.refresh_from_db()
        self.assertEqual(pedido.peso_esperado_gr, 0)

    def test_recolectado_despacha_las_hijas_no_el_kit(self):
        pedido, kit = self._pedido_con_kit(estado=Pedido.GUIA_GENERADA)
        LineaPedido.objects.create(
            pedido=pedido, sku=self.te, cantidad=3, cantidad_pickeada=3,
            reservada=True, parte_de_kit=kit,
        )
        with patch("apps.inventario.services.despachar") as despachar, \
             patch("apps.mensajeria.services.enviar_en_camino"), \
             self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, "salida1")
        despachar.assert_called_once_with(self.te, 3, pedido.folio)

    def test_declarar_fuera_de_picking_truena(self):
        _, kit = self._pedido_con_kit(estado=Pedido.EMPACADO)
        with self.assertRaises(ValueError):
            services.declarar_contenido_kit(kit, [(self.te, 3)], "packer1")

    def test_kit_dentro_de_kit_truena(self):
        _, kit = self._pedido_con_kit()
        otro_kit = SKU.objects.create(
            cliente=self.cliente, codigo="TEABOX-2", descripcion="Otro kit", es_kit=True,
        )
        with self.assertRaises(ValueError):
            services.declarar_contenido_kit(kit, [(otro_kit, 1)], "packer1")


class IngestaPorTicketTests(BaseServicios):
    """Stage 2 multi-location: se ingieren solo líneas/cantidades de NUESTRO ticket."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sku_caja = SKU.objects.create(
            cliente=cls.cliente, codigo="COL-C12", descripcion="Caja 12 Colimita",
            peso_gr=8000, precio_declarado=Decimal("400.00"),
        )

    def _payload_dos_lineas(self, order_id):
        payload = payload_shopify(order_id=order_id)
        payload["line_items"] = [
            {"id": 111, "sku": "COL-SIX", "quantity": 2, "price": "189.00", "title": "Six"},
            {"id": 222, "sku": "COL-C12", "quantity": 1, "price": "400.00", "title": "Caja"},
        ]
        payload["total_price"] = "778.00"
        return payload

    def _ingerir_con_ticket(self, payload, ticket):
        with patch("apps.integraciones.services.lineas_fulfillment_nuestras", **ticket), \
             patch("apps.inventario.services.reservar", return_value=True) as reservar, \
             patch("apps.mensajeria.services.enviar_confirmacion"), \
             self.captureOnCommitCallbacks(execute=True):
            pedido = services.ingerir_pedido_shopify(self.tienda, payload)
        return pedido, reservar

    def test_subset_ingiere_solo_lo_nuestro(self):
        ticket = {"parcial": True, "cantidades": {"111": 2}, "fos": ["gid://shopify/FulfillmentOrder/1"]}
        pedido, reservar = self._ingerir_con_ticket(
            self._payload_dos_lineas(8001), {"return_value": ticket},
        )
        self.assertEqual(pedido.lineas.count(), 1)
        linea = pedido.lineas.get()
        self.assertEqual(linea.sku, self.sku)
        self.assertEqual(linea.cantidad, 2)
        self.assertTrue(pedido.parcial_de_orden)
        self.assertEqual(pedido.valor_declarado, Decimal("378.00"))  # solo lo nuestro
        self.assertEqual(pedido.peso_esperado_gr, 4800)
        reservar.assert_called_once_with(self.sku, 2, pedido.folio)
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(pedido.pk), accion="ingesta",
        )
        self.assertTrue(evento.delta["parcial"])
        self.assertEqual(evento.delta["fos"], ["gid://shopify/FulfillmentOrder/1"])

    def test_split_de_linea_toma_solo_lo_asignado(self):
        ticket = {"parcial": True, "cantidades": {"111": 1}, "fos": ["gid://x/1"]}
        pedido, reservar = self._ingerir_con_ticket(
            self._payload_dos_lineas(8002), {"return_value": ticket},
        )
        self.assertEqual(pedido.lineas.get().cantidad, 1)
        reservar.assert_called_once_with(self.sku, 1, pedido.folio)

    def test_nada_nuestro_omite_sin_crear_pedido(self):
        ticket = {"parcial": True, "cantidades": {}, "fos": []}
        pedido, _ = self._ingerir_con_ticket(
            self._payload_dos_lineas(8003), {"return_value": ticket},
        )
        self.assertIsNone(pedido)
        self.assertEqual(Pedido.objects.count(), 0)
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id="8003", accion="ingesta_omitida_otra_location",
        ).exists())

    def test_todo_nuestro_no_es_parcial_y_conserva_el_total(self):
        ticket = {"parcial": False, "cantidades": {"111": 2, "222": 1}, "fos": ["gid://x/1"]}
        pedido, _ = self._ingerir_con_ticket(
            self._payload_dos_lineas(8004), {"return_value": ticket},
        )
        self.assertEqual(pedido.lineas.count(), 2)
        self.assertFalse(pedido.parcial_de_orden)
        self.assertEqual(pedido.valor_declarado, Decimal("778.00"))  # total de la orden

    def test_sin_datos_ingiere_completo(self):
        pedido, _ = self._ingerir_con_ticket(
            self._payload_dos_lineas(8005), {"return_value": None},
        )
        self.assertEqual(pedido.lineas.count(), 2)
        self.assertFalse(pedido.parcial_de_orden)

    def test_error_de_shopify_se_propaga_para_replay(self):
        from apps.integraciones.shopify import ShopifyError
        with self.assertRaises(ShopifyError):
            self._ingerir_con_ticket(
                self._payload_dos_lineas(8006), {"side_effect": ShopifyError("caída")},
            )
        self.assertEqual(Pedido.objects.count(), 0)


class EdicionOrdenTests(BaseServicios):
    """Refund parcial / edición de orden sobre pedidos ya ingeridos (2f)."""

    def _ingerido(self, order_id, cantidad=2):
        with patch("apps.inventario.services.reservar", return_value=True), \
             patch("apps.mensajeria.services.enviar_confirmacion"), \
             self.captureOnCommitCallbacks(execute=True):
            return services.ingerir_pedido_shopify(
                self.tienda, payload_shopify(order_id=order_id, cantidad=cantidad),
            )

    def _actualizar(self, pedido, payload, reservar_ok=True):
        with patch("apps.inventario.services.liberar_reserva") as liberar, \
             patch("apps.inventario.services.reservar", return_value=reservar_ok) as reservar, \
             patch("apps.incidencias.services.abrir_incidencia") as abrir, \
             self.captureOnCommitCallbacks(execute=True):
            services.ingerir_pedido_shopify(self.tienda, payload)
        pedido.refresh_from_db()
        return liberar, reservar, abrir

    def _payload_con(self, order_id, current_quantity):
        payload = payload_shopify(order_id=order_id)
        payload["line_items"][0]["current_quantity"] = current_quantity
        return payload

    def test_reduccion_libera_y_encoge(self):
        pedido = self._ingerido(7001)
        liberar, _, abrir = self._actualizar(pedido, self._payload_con(7001, 1))
        linea = pedido.lineas.get()
        self.assertEqual(linea.cantidad, 1)
        self.assertEqual(pedido.peso_esperado_gr, 2400)
        liberar.assert_called_once_with(self.sku, 1, pedido.folio)
        abrir.assert_not_called()
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(pedido.pk), accion="edicion_orden",
        )
        self.assertEqual(evento.delta["reducidas"], [{"sku": "COL-SIX", "quitadas": 1}])

    def test_reduccion_a_cero_borra_la_linea(self):
        pedido = self._ingerido(7002)
        liberar, _, _ = self._actualizar(pedido, self._payload_con(7002, 0))
        self.assertEqual(pedido.lineas.count(), 0)
        liberar.assert_called_once_with(self.sku, 2, pedido.folio)

    def test_reduccion_con_avance_fisico_abre_can_sin_tocar_cantidades(self):
        pedido = self._ingerido(7003)
        pedido.estado = Pedido.EN_PICKING
        pedido.save(update_fields=["estado"])
        linea = pedido.lineas.get()
        linea.cantidad_pickeada = 1
        linea.save(update_fields=["cantidad_pickeada"])
        liberar, _, abrir = self._actualizar(pedido, self._payload_con(7003, 1))
        linea.refresh_from_db()
        self.assertEqual(linea.cantidad, 2)
        liberar.assert_not_called()
        abrir.assert_called_once()
        self.assertEqual(abrir.call_args.args[1], "CAN")
        self.assertTrue(pedido.incidencia_activa)

    def test_aumento_en_pendiente_crea_linea_y_reserva(self):
        pedido = self._ingerido(7004)
        liberar, reservar, abrir = self._actualizar(pedido, self._payload_con(7004, 4))
        self.assertEqual(pedido.lineas.count(), 2)
        self.assertEqual(sum(l.cantidad for l in pedido.lineas.all()), 4)
        nueva = pedido.lineas.latest("pk")
        self.assertEqual(nueva.cantidad, 2)
        self.assertTrue(nueva.reservada)
        reservar.assert_called_once_with(self.sku, 2, pedido.folio)
        liberar.assert_not_called()
        abrir.assert_not_called()

    def test_aumento_sin_stock_abre_fal(self):
        pedido = self._ingerido(7005)
        _, _, abrir = self._actualizar(pedido, self._payload_con(7005, 4), reservar_ok=False)
        nueva = pedido.lineas.latest("pk")
        self.assertFalse(nueva.reservada)
        abrir.assert_called_once()
        self.assertEqual(abrir.call_args.args[1], "FAL")
        self.assertTrue(pedido.incidencia_activa)

    def test_aumento_fuera_de_pendiente_es_conflicto(self):
        pedido = self._ingerido(7006)
        pedido.estado = Pedido.EMPACADO
        pedido.save(update_fields=["estado"])
        _, reservar, abrir = self._actualizar(pedido, self._payload_con(7006, 4))
        self.assertEqual(pedido.lineas.count(), 1)
        reservar.assert_not_called()
        self.assertEqual(abrir.call_args.args[1], "CAN")

    def test_payload_sin_current_quantity_es_noop(self):
        pedido = self._ingerido(7007)
        liberar, _, abrir = self._actualizar(pedido, payload_shopify(order_id=7007))
        self.assertEqual(pedido.lineas.get().cantidad, 2)
        liberar.assert_not_called()
        abrir.assert_not_called()
        self.assertFalse(EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), accion="edicion_orden",
        ).exists())

    def test_mismo_update_dos_veces_solo_actua_una(self):
        pedido = self._ingerido(7008)
        self._actualizar(pedido, self._payload_con(7008, 1))
        liberar, _, _ = self._actualizar(pedido, self._payload_con(7008, 1))
        liberar.assert_not_called()
        self.assertEqual(pedido.lineas.get().cantidad, 1)

    def test_estado_terminal_no_ajusta(self):
        pedido = self._ingerido(7009)
        pedido.estado = Pedido.ENTREGADO
        pedido.save(update_fields=["estado"])
        liberar, _, abrir = self._actualizar(pedido, self._payload_con(7009, 1))
        self.assertEqual(pedido.lineas.get().cantidad, 2)
        liberar.assert_not_called()
        abrir.assert_not_called()


class ReintentoReservasTests(BaseServicios):
    """Auto-retry al entrar stock + botón manual de Mesa (pedidos nacidos sin stock)."""

    def _pedido_sin_reserva(self, order_id):
        with patch("apps.inventario.services.reservar", return_value=False), \
             patch("apps.mensajeria.services.enviar_confirmacion"), \
             patch("apps.incidencias.services.abrir_incidencia"), \
             self.captureOnCommitCallbacks(execute=True):
            return services.ingerir_pedido_shopify(self.tienda, payload_shopify(order_id=order_id))

    def test_reintento_por_sku_es_fifo_por_antiguedad(self):
        viejo = self._pedido_sin_reserva(1001)
        nuevo = self._pedido_sin_reserva(1002)
        # Solo alcanza stock para UNA línea: se la lleva el pedido más viejo.
        with patch("apps.inventario.services.reservar", side_effect=[True, False]):
            logrados = services.reintentar_reservas_sku(self.sku)
        self.assertEqual(logrados, [viejo.folio])
        self.assertTrue(viejo.lineas.get().reservada)
        self.assertFalse(nuevo.lineas.get().reservada)
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(viejo.pk), accion="reserva_reintentada",
        )
        self.assertTrue(evento.delta["pedido_completo"])

    def test_hook_de_inventario_dispara_el_reintento_en_on_commit(self):
        from apps.inventario.services import _reintentar_pendientes

        with patch("apps.pedidos.services.reintentar_reservas_sku") as reintento, \
             self.captureOnCommitCallbacks(execute=True):
            _reintentar_pendientes(self.sku)
        reintento.assert_called_once_with(self.sku)

    def test_boton_reserva_el_pedido_y_reporta(self):
        pedido = self._pedido_sin_reserva(1003)
        with patch("apps.inventario.services.reservar", return_value=True):
            mensaje = services.reintentar_reservas_pedido(pedido, "mesa1")
        self.assertIn("todas sus líneas quedaron reservadas", mensaje)
        self.assertTrue(pedido.lineas.get().reservada)

    def test_boton_reporta_lo_que_sigue_sin_stock(self):
        pedido = self._pedido_sin_reserva(1004)
        with patch("apps.inventario.services.reservar", return_value=False):
            mensaje = services.reintentar_reservas_pedido(pedido, "mesa1")
        self.assertIn("0 de 1", mensaje)
        self.assertFalse(pedido.lineas.get().reservada)

    def test_boton_fuera_de_pendiente_truena(self):
        pedido = self._pedido_sin_reserva(1005)
        pedido.estado = Pedido.EN_PICKING
        pedido.save(update_fields=["estado"])
        with self.assertRaises(ValueError):
            services.reintentar_reservas_pedido(pedido, "mesa1")

    def test_boton_sin_lineas_pendientes_avisa(self):
        with patch("apps.inventario.services.reservar", return_value=True), \
             patch("apps.mensajeria.services.enviar_confirmacion"), \
             self.captureOnCommitCallbacks(execute=True):
            pedido = services.ingerir_pedido_shopify(self.tienda, payload_shopify(order_id=1006))
        mensaje = services.reintentar_reservas_pedido(pedido, "mesa1")
        self.assertIn("ya tiene todas sus líneas reservadas", mensaje)


class PickingTests(BaseServicios):
    def setUp(self):
        self.pedido = self.pedido_directo(estado=Pedido.EN_PICKING)
        self.linea = LineaPedido.objects.create(
            pedido=self.pedido, sku=self.sku, cantidad=2, reservada=True,
        )

    def test_confirmar_pick_por_escaneo(self):
        services.confirmar_linea_pick(
            self.linea, 1, actor=None, codigo_escaneado="7501234567890",
        )
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_pickeada, 1)

    def test_codigo_de_barras_equivocado_truena(self):
        with self.assertRaises(ValueError):
            services.confirmar_linea_pick(
                self.linea, 1, actor=None, codigo_escaneado="0000000000000",
            )
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_pickeada, 0)

    def test_sobrepick_truena(self):
        services.confirmar_linea_pick(self.linea, 2, actor=None)
        with self.assertRaises(ValueError):
            services.confirmar_linea_pick(self.linea, 1, actor=None)

    def test_pick_fuera_de_picking_truena(self):
        pedido = self.pedido_directo(estado=Pedido.PENDIENTE)
        linea = LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=1)
        with self.assertRaises(ValueError):
            services.confirmar_linea_pick(linea, 1, actor=None)

    def test_confirmar_pick_valida_avance_fresco_no_el_de_memoria(self):
        # Carrera de escaneos: dos requests con instancias separadas de la
        # MISMA línea. El servicio re-lee bajo lock: el segundo escaneo suma
        # sobre el avance REAL y el sobrepick truena aunque su instancia en
        # memoria diga 0.
        linea_vieja = LineaPedido.objects.get(pk=self.linea.pk)
        services.confirmar_linea_pick(self.linea, 2, actor=None)
        self.assertEqual(self.linea.cantidad_pickeada, 2)  # instancia sincronizada
        self.assertEqual(linea_vieja.cantidad_pickeada, 0)  # copia rezagada
        with self.assertRaises(ValueError) as ctx:
            services.confirmar_linea_pick(linea_vieja, 1, actor=None)
        self.assertIn("ya llevas 2", str(ctx.exception))
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_pickeada, 2)  # nada de picks fantasma


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class EmpaqueTests(BaseServicios):
    def setUp(self):
        self.pedido = self.pedido_directo(estado=Pedido.EN_PICKING, peso_esperado_gr=4800)
        self.linea = LineaPedido.objects.create(
            pedido=self.pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2, reservada=True,
        )

    def test_empacar_sin_foto_de_contenido_truena(self):
        # Contrato del carril único: empacar exige ≥1 foto de CONTENIDO (la de
        # caja cerrada ya no se pide aquí: la guía aún no existe).
        with patch("apps.inventario.services.confirmar_pick"):
            with self.assertRaises(ValueError) as ctx:
                services.empacar(self.pedido, actor=None, peso_real_gr=4800, fotos=[])
        self.assertIn("foto del contenido", str(ctx.exception).lower())
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    def test_empacar_basta_una_foto_de_contenido(self):
        with patch("apps.inventario.services.confirmar_pick"):
            services.empacar(self.pedido, actor=None, peso_real_gr=4800, fotos=[foto()])
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EMPACADO)
        fotos = EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk),
        )
        self.assertEqual(fotos.count(), 1)
        # Las fotos del empaque son de contenido; la caja_cerrada llega
        # después vía cerrar_caja, con la etiqueta pegada.
        self.assertEqual(set(fotos.values_list("tipo", flat=True)), {"contenido"})

    def test_empacar_peso_fuera_de_tolerancia_truena(self):
        with patch("apps.inventario.services.confirmar_pick"):
            with self.assertRaises(ValueError) as ctx:
                services.empacar(
                    self.pedido, actor=None, peso_real_gr=6000, fotos=[foto(), foto("caja.jpg")],
                )
        self.assertIn("peso", str(ctx.exception).lower())
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        # El peso truena antes de persistir evidencia.
        self.assertEqual(EvidenciaFoto.objects.count(), 0)

    def test_empacar_con_lineas_sin_pickear_truena(self):
        self.linea.cantidad_pickeada = 1
        self.linea.save(update_fields=["cantidad_pickeada"])
        with patch("apps.inventario.services.confirmar_pick"):
            with self.assertRaises(ValueError):
                services.empacar(
                    self.pedido, actor=None, peso_real_gr=4800, fotos=[foto(), foto("caja.jpg")],
                )

    def test_empacar_ok_transiciona_y_confirma_picks(self):
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar(
                self.pedido, actor=None, peso_real_gr=4850,  # ~1% de diferencia, dentro de ±3%
                fotos=[foto("contenido.jpg"), foto("caja.jpg")],
            )
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EMPACADO)
        self.assertIsNotNone(self.pedido.ts_empacado)
        self.assertEqual(self.pedido.peso_real_gr, 4850)
        self.assertEqual(
            EvidenciaFoto.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk)).count(),
            2,
        )
        confirmar.assert_called_once_with(self.sku, 2, self.pedido.folio)


class GuiaYSalidaTests(BaseServicios):
    def test_generar_guia_transiciona(self):
        pedido = self.pedido_directo(estado=Pedido.EMPACADO)

        class GuiaDemo:
            numero = "MOCK-0001"

        with patch("apps.envios.services.generar_guia", return_value=GuiaDemo()) as generar:
            guia = services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        self.assertIsNotNone(pedido.ts_guia)
        self.assertEqual(guia.numero, "MOCK-0001")
        generar.assert_called_once_with(pedido)

    def test_generar_guia_sin_empacar_truena(self):
        pedido = self.pedido_directo(estado=Pedido.PENDIENTE)
        with self.assertRaises(ValueError):
            services.generar_guia(pedido)

    def test_marcar_recolectado_despacha_y_manda_plantilla_b(self):
        pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA)
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2, reservada=True,
        )
        with patch("apps.inventario.services.despachar") as despachar, \
             patch("apps.mensajeria.services.enviar_en_camino") as en_camino:
            services.marcar_recolectado(pedido, actor=None)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
        self.assertIsNotNone(pedido.ts_recolectado)
        despachar.assert_called_once_with(self.sku, 2, pedido.folio)
        # Plantilla B ("en camino"): SOLO aquí, exactamente una vez.
        en_camino.assert_called_once()
        self.assertEqual(en_camino.call_args[0][0].pk, pedido.pk)

    def test_marcar_recolectado_sin_guia_truena(self):
        pedido = self.pedido_directo(estado=Pedido.EN_PICKING)
        with self.assertRaises(ValueError):
            services.marcar_recolectado(pedido, actor=None)


class CancelacionTests(BaseServicios):
    def test_cancelar_pendiente_se_cancela_solo_y_libera(self):
        pedido = self.pedido_directo()
        LineaPedido.objects.create(pedido=pedido, sku=self.sku, cantidad=2, reservada=True)
        with patch("apps.inventario.services.liberar_reserva") as liberar:
            services.cancelar(pedido, actor=None, motivo="Comprador se arrepintió")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        liberar.assert_called_once_with(self.sku, 2, pedido.folio)

    def test_cancelar_post_picking_pasa_por_cancelacion_pendiente(self):
        pedido = self.pedido_directo(estado=Pedido.EN_PICKING)
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=2, cantidad_pickeada=1, reservada=True,
        )
        services.cancelar(pedido, actor=None, motivo="Cancelación en pleno picking")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELACION_PENDIENTE)
        # El restock del piso cierra la cancelación.
        with patch("apps.inventario.services.liberar_reserva") as liberar, \
             patch("apps.inventario.services.retornar") as retornar:
            services.confirmar_restock(pedido, actor=None)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        # Sin empaque confirmado, todo seguía reservado: se libera completo.
        liberar.assert_called_once_with(self.sku, 2, pedido.folio)
        retornar.assert_not_called()

    def test_restock_de_pedido_empacado_reingresa_lo_pickeado(self):
        pedido = self.pedido_directo(estado=Pedido.EMPACADO, ts_empacado=timezone.now())
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2, reservada=True,
        )
        services.cancelar(pedido, actor=None, motivo="Cancelación tras empaque")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELACION_PENDIENTE)
        with patch("apps.inventario.services.liberar_reserva") as liberar, \
             patch("apps.inventario.services.retornar") as retornar:
            services.confirmar_restock(pedido, actor=None)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        retornar.assert_called_once_with(self.sku, 2, pedido.folio, None)
        liberar.assert_not_called()

    def test_cancelar_despachado_abre_incidencia_can(self):
        pedido = self.pedido_directo(estado=Pedido.RECOLECTADO)
        with patch("apps.incidencias.services.abrir_incidencia") as abrir:
            services.cancelar(pedido, actor=None, motivo="Ya no lo quiere")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)  # el estado no cambia
        self.assertTrue(pedido.incidencia_activa)
        abrir.assert_called_once()
        self.assertEqual(abrir.call_args[0][1], "CAN")

    def test_cancelar_entregado_truena(self):
        pedido = self.pedido_directo(estado=Pedido.ENTREGADO)
        with self.assertRaises(ValueError):
            services.cancelar(pedido, actor=None, motivo="tarde")

    def test_confirmar_restock_sin_cancelacion_pendiente_truena(self):
        pedido = self.pedido_directo(estado=Pedido.EN_PICKING)
        with self.assertRaises(ValueError):
            services.confirmar_restock(pedido, actor=None)


class EntregaPresuntaTests(BaseServicios):
    def test_cierra_en_transito_viejo_y_respeta_reciente(self):
        viejo = self.pedido_directo(estado=Pedido.EN_TRANSITO)
        reciente = self.pedido_directo(estado=Pedido.EN_TRANSITO)
        Pedido.objects.filter(pk=viejo.pk).update(
            ts_en_transito=timezone.now() - timedelta(days=30),
        )
        Pedido.objects.filter(pk=reciente.pk).update(
            ts_en_transito=timezone.now() - timedelta(days=1),
        )
        cerrados = services.cerrar_entregas_presuntas()
        viejo.refresh_from_db()
        reciente.refresh_from_db()
        self.assertEqual(viejo.estado, Pedido.ENTREGA_PRESUNTA)
        self.assertEqual(reciente.estado, Pedido.EN_TRANSITO)
        self.assertEqual([p.pk for p in cerrados], [viejo.pk])

    def test_idempotente(self):
        viejo = self.pedido_directo(estado=Pedido.EN_TRANSITO)
        Pedido.objects.filter(pk=viejo.pk).update(
            ts_en_transito=timezone.now() - timedelta(days=30),
        )
        primera = services.cerrar_entregas_presuntas()
        segunda = services.cerrar_entregas_presuntas()
        self.assertEqual(len(primera), 1)
        self.assertEqual(len(segunda), 0)
