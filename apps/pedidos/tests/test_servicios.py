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
from apps.core.models import Cliente, EvidenciaFoto
from apps.integraciones.models import Tienda
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def payload_shopify(order_id=5501234, sku="COL-SIX", cantidad=2, cp="28017", cancelado=False):
    payload = {
        "id": order_id,
        "name": f"#{order_id}",
        "email": "comprador@example.com",
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
        cls.tienda = Tienda.objects.create(
            cliente=cls.cliente,
            plataforma="shopify",
            dominio="colima-mx.myshopify.com",
            token="tok-demo",
            location_id="1",
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

    def test_payload_sin_id_truena(self):
        with self.assertRaises(ValueError):
            services.ingerir_pedido_shopify(self.tienda, {"line_items": []})


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
