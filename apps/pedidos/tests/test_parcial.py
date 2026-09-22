"""Fulfillment parcial (Chema 2026-09-22): una línea sin inventario no detiene
el pedido. Lo que sí hay se pickea, empaca y sale; la faltante espera con el
tag "Sin inventario" y sale en una segunda ola cuando llega stock. Aquí: la
marca de faltante, el contador de lo despachado, el kardex de la segunda ola
(nada se confirma ni despacha dos veces) y la cancelación con una parte ya en
la calle. El stock entra siempre por la puerta oficial (recibir → ubicar)."""
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, PerfilUsuario
from apps.core.models import EventoAuditoria
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Incidencia
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo
from apps.inventario.services import disponible, recibir, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


def suma(sku, estado):
    """Piezas del SKU en ese estado del kardex."""
    return Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"] or 0


def foto():
    return SimpleUploadedFile("contenido.jpg", b"bytes", content_type="image/jpeg")


@override_settings(ENVIA_API_KEY="", MEDIA_ROOT=tempfile.mkdtemp(prefix="torre-parcial-"))
class BaseParcial(TestCase):
    """Bodega mínima: SKU A con 10 piezas ubicadas, SKU B sin existencias."""

    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", integracion_envios="envia",
        )
        self.a = SKU.objects.create(
            cliente=self.cliente, codigo="A-SIX", descripcion="Six A", peso_gr=2000,
            requiere_lote=False, precio_declarado=Decimal(180),
        )
        self.b = SKU.objects.create(
            cliente=self.cliente, codigo="B-SIX", descripcion="Six B", peso_gr=2000,
            requiere_lote=False, precio_declarado=Decimal(180),
        )
        self.rec = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.operador = get_user_model().objects.create_user(username="piso1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.operador, rol="piso", pin="1111")
        self.entra(self.a, 10)

    def entra(self, sku, piezas):
        """Stock por la puerta oficial: recibir → ubicar."""
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=sku, cantidad_anunciada=piezas)
        recibir(linea, piezas, 0, self.operador)
        ubicar(sku, piezas, self.pic, None, self.operador)

    def pedido_parcial(self, estado=Pedido.PENDIENTE):
        """A × 2 con reserva, B × 1 sin inventario (faltante)."""
        pedido = Pedido.objects.create(
            cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana",
            cp="01780", estado=estado,
        )
        la = LineaPedido.objects.create(pedido=pedido, sku=self.a, cantidad=2)
        lb = LineaPedido.objects.create(pedido=pedido, sku=self.b, cantidad=1)
        self.assertTrue(services._reservar_linea(la))
        self.assertFalse(services._reservar_linea(lb))
        return pedido, la, lb

    def primera_ola_fuera(self):
        """Pedido parcial pickeado, empacado y con manifiesto firmado: A salió, B espera."""
        pedido, la, lb = self.pedido_parcial(Pedido.EN_PICKING)
        services.confirmar_linea_pick(la, 2, self.operador)
        services.empacar(pedido, self.operador, 4200, [foto()])
        pedido.transicionar(Pedido.GUIA_GENERADA)
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()
        la.refresh_from_db()
        lb.refresh_from_db()
        return pedido, la, lb


class MarcaFaltanteTests(BaseParcial):
    def test_linea_sin_reserva_es_faltante_y_no_cuenta(self):
        pedido, la, lb = self.pedido_parcial()
        self.assertEqual((la.faltante, la.pendiente), (False, 2))
        self.assertEqual((lb.faltante, lb.pendiente), (True, 0))
        self.assertEqual([l.pk for l in pedido.lineas_por_surtir], [la.pk])
        self.assertEqual([l.pk for l in pedido.lineas_faltantes], [lb.pk])
        self.assertTrue(pedido.tiene_faltantes)
        self.assertFalse(pedido.lineas_completas)
        la.cantidad_pickeada = 2
        la.save(update_fields=["cantidad_pickeada"])
        self.assertTrue(pedido.lineas_completas)  # B no cuenta: va con "Sin inventario"

    def test_kit_con_hija_faltante_espera_completo(self):
        kit = SKU.objects.create(
            cliente=self.cliente, codigo="TEABOX", descripcion="TeaBox", peso_gr=400,
            es_kit=True, requiere_lote=False,
        )
        pedido = Pedido.objects.create(
            cliente=self.cliente, origen="manual", comprador_nombre="Ana", cp="01780",
        )
        lk = LineaPedido.objects.create(pedido=pedido, sku=kit, cantidad=1, reservada=True)
        LineaPedido.objects.create(pedido=pedido, sku=self.b, cantidad=1, parte_de_kit=lk)  # sin stock
        self.assertTrue(lk.faltante)
        self.assertEqual(lk.pendiente, 0)
        self.assertEqual(pedido.lineas_por_surtir, [])


class KardexSegundaOlaTests(BaseParcial):
    def test_manifiesto_despacha_solo_lo_pickeado_y_lo_estampa(self):
        pedido, la, lb = self.primera_ola_fuera()
        self.assertEqual((la.cantidad_despachada, lb.cantidad_despachada), (2, 0))
        self.assertEqual(suma(self.a, Saldo.EN_EMPAQUE), 0)
        self.assertEqual(suma(self.a, Saldo.UBICADO_VENDIBLE), 8)
        self.assertTrue(pedido.tiene_despachadas)
        self.assertTrue(pedido.tiene_faltantes)
        self.assertEqual(pedido.lineas_por_surtir, [])  # A ya salió, B espera

    def test_segunda_ola_no_reconfirma_ni_redespacha_la_primera(self):
        pedido, la, lb = self.primera_ola_fuera()
        self.entra(self.b, 5)
        self.assertTrue(services._reservar_linea(lb))
        # La reapertura del pedido llega con la pieza C3; aquí solo el kardex.
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EN_PICKING)
        pedido.refresh_from_db()
        self.assertEqual([l.pk for l in pedido.lineas_por_surtir], [lb.pk])
        services.confirmar_linea_pick(lb, 1, self.operador)
        services.empacar(pedido, self.operador, 2100, [foto()])
        self.assertEqual(suma(self.a, Saldo.EN_EMPAQUE), 0)  # A no se reconfirma
        self.assertEqual(suma(self.b, Saldo.EN_EMPAQUE), 1)
        pedido.transicionar(Pedido.GUIA_GENERADA)
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        la.refresh_from_db()
        lb.refresh_from_db()
        self.assertEqual((la.cantidad_despachada, lb.cantidad_despachada), (2, 1))
        self.assertEqual(suma(self.a, Saldo.UBICADO_VENDIBLE), 8)  # A salió una sola vez
        self.assertEqual(suma(self.b, Saldo.UBICADO_VENDIBLE), 4)
        self.assertEqual(suma(self.b, Saldo.EN_EMPAQUE), 0)

    def test_empacar_caja_ignora_la_faltante(self):
        pedido, la, _lb = self.pedido_parcial(Pedido.EN_PICKING)
        services.confirmar_linea_pick(la, 2, self.operador)
        caja = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("4.2"), carrier="estafeta", servicio="ground",
        )
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=la, cantidad=2)
        services.empacar_caja(caja, self.operador, 4200, foto())
        caja.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((caja.estado, pedido.estado), (Paquete.EMPACADO, Pedido.EMPACADO))
        self.assertEqual(suma(self.a, Saldo.EN_EMPAQUE), 2)


class CancelacionMixtaTests(BaseParcial):
    def _segunda_ola_reservada(self, estado=Pedido.PENDIENTE):
        pedido, la, lb = self.primera_ola_fuera()
        self.entra(self.b, 5)
        self.assertTrue(services._reservar_linea(lb))
        Pedido.objects.filter(pk=pedido.pk).update(estado=estado)  # la reapertura llega en C3
        pedido.refresh_from_db()
        return pedido, la, lb

    def test_pendiente_con_la_primera_en_la_calle(self):
        pedido, la, lb = self._segunda_ola_reservada()
        self.assertEqual(suma(self.b, Saldo.RESERVADO), 1)
        services.cancelar(pedido, self.operador, motivo="Ya no lo quiere")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertTrue(pedido.cancelacion_tardia)
        self.assertEqual(pedido.reingreso_estado, Pedido.REINGRESO_PENDIENTE)
        self.assertEqual(suma(self.b, Saldo.RESERVADO), 0)
        self.assertEqual(disponible(self.b), 5)
        self.assertEqual(suma(self.a, Saldo.UBICADO_VENDIBLE), 8)  # lo de la calle no se toca
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo="CAN").exists())
        self.assertIn(pedido, list(services.reingresos_por_decidir()))
        orden = services.registrar_reingreso(pedido, self.operador)
        self.assertEqual(
            [(l.sku_id, l.cantidad_anunciada) for l in orden.lineas.all()], [(self.a.pk, 2)],
        )
        la.refresh_from_db()
        lb.refresh_from_db()
        self.assertEqual((la.reservada, lb.reservada), (True, False))

    def test_en_picking_reingresa_lo_de_bodega_y_deja_la_calle_a_mesa(self):
        pedido, _la, lb = self._segunda_ola_reservada(Pedido.EN_PICKING)
        services.confirmar_linea_pick(lb, 1, self.operador)
        services.cancelar(pedido, self.operador, motivo="Cancelado a medio pick")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.CANCELADO)
        self.assertTrue(pedido.cancelacion_tardia)
        self.assertEqual(pedido.reingreso_estado, Pedido.REINGRESO_PENDIENTE)  # la calle sigue por decidir
        self.assertEqual(suma(self.b, Saldo.EN_PUTAWAY), 1)
        self.assertEqual(suma(self.b, Saldo.RESERVADO), 0)
        self.assertEqual(pedido.reingresos.get().lineas.get().cantidad_recibida, 1)
        orden = services.registrar_reingreso(pedido, self.operador)  # lo de la calle: solo A
        self.assertEqual(
            [(l.sku_id, l.cantidad_anunciada) for l in orden.lineas.all()], [(self.a.pk, 2)],
        )


class SegundaOlaTests(BaseParcial):
    """El manifiesto con faltantes deja el pedido esperando inventario; al
    entrar stock vuelve a PENDIENTE sin dueño y con cajas nuevas solo con lo
    pendiente; el tracking de lo que salió no lo entrega antes de tiempo; la
    segunda ola cierra en RECOLECTADO con su propio "va en camino" y su
    correo de Shopify."""

    def test_manifiesto_con_faltantes_queda_esperando_inventario(self):
        pedido, _la, _lb = self.primera_ola_fuera()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)
        self.assertTrue(pedido.esperando_inventario)
        self.assertTrue(pedido.pendiente_de_completar)
        evento = EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), accion="cambio_estado",
        ).latest("ts")
        self.assertIn("B-SIX", evento.motivo)

    def test_llega_stock_y_vuelve_a_pendiente_sin_duenio_y_con_caja_nueva(self):
        pedido, _la, lb = self.primera_ola_fuera()
        pedido.asignado_a = self.operador
        pedido.save(update_fields=["asignado_a"])
        with self.captureOnCommitCallbacks(execute=True):
            self.entra(self.b, 5)  # ubicar → reintentar_reservas_sku en on_commit
        pedido.refresh_from_db()
        lb.refresh_from_db()
        self.assertTrue(lb.reservada)
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertIsNone(pedido.asignado_a)
        self.assertFalse(pedido.esperando_inventario)
        self.assertEqual([l.pk for l in pedido.lineas_por_surtir], [lb.pk])
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(pedido.pk), accion="pedido_reabierto",
        ).exists())
        cajas = list(pedido.paquetes.all())
        self.assertEqual(len(cajas), 1)
        self.assertEqual([(pl.linea_pedido_id, pl.cantidad) for pl in cajas[0].lineas.all()], [(lb.pk, 1)])

    def test_con_dos_faltantes_espera_a_tener_todas(self):
        c = SKU.objects.create(
            cliente=self.cliente, codigo="C-SIX", descripcion="Six C", peso_gr=2000,
            requiere_lote=False, precio_declarado=Decimal(180),
        )
        pedido, la, lb = self.pedido_parcial(Pedido.EN_PICKING)
        lc = LineaPedido.objects.create(pedido=pedido, sku=c, cantidad=1)
        services.confirmar_linea_pick(la, 2, self.operador)
        services.empacar(pedido, self.operador, 4200, [foto()])
        pedido.transicionar(Pedido.GUIA_GENERADA)
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        with self.captureOnCommitCallbacks(execute=True):
            self.entra(self.b, 5)
        pedido.refresh_from_db()
        lb.refresh_from_db()
        self.assertTrue(lb.reservada)
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)  # C sigue sin inventario
        with self.captureOnCommitCallbacks(execute=True):
            self.entra(c, 5)
        pedido.refresh_from_db()
        lc.refresh_from_db()
        self.assertTrue(lc.reservada)
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)

    def test_el_boton_de_mesa_tambien_reabre(self):
        pedido, _la, lb = self.primera_ola_fuera()
        self.entra(self.b, 5)  # sin on_commit: el reintento automático no corre
        lb.refresh_from_db()
        self.assertFalse(lb.reservada)
        with self.captureOnCommitCallbacks(execute=True):
            mensaje = services.reintentar_reservas_pedido(pedido, self.operador)
        self.assertIn("vuelve a picking", mensaje)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)

    def test_el_tracking_no_entrega_el_pedido_mientras_espera(self):
        from apps.envios.services import _aplicar_efectos
        pedido, _la, _lb = self.primera_ola_fuera()
        guia = Guia.objects.create(
            pedido=pedido, carrier="estafeta", numero="G-1", proveedor="mock", estado=Guia.ENTREGADO,
        )
        _aplicar_efectos(guia, Guia.ENTREGADO, "Entregado")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)

    def test_el_tracking_no_mueve_el_pedido_durante_la_segunda_ola(self):
        from apps.envios.services import _aplicar_efectos
        pedido, _la, lb = self.primera_ola_fuera()
        guia = Guia.objects.create(
            pedido=pedido, carrier="estafeta", numero="G-1", proveedor="mock", estado=Guia.EN_TRANSITO,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.entra(self.b, 5)
        pedido.refresh_from_db()
        services.iniciar_picking(pedido, self.operador)
        lb.refresh_from_db()
        services.confirmar_linea_pick(lb, 1, self.operador)
        caja = pedido.paquetes.get()
        services.empacar_caja(caja, self.operador, 2100, foto())
        pedido.refresh_from_db()
        pedido.transicionar(Pedido.GUIA_GENERADA)
        guia.estado = Guia.ENTREGADO
        guia.save(update_fields=["estado"])
        _aplicar_efectos(guia, Guia.ENTREGADO, "Entregado")
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)  # la segunda ola sigue en el corral

    def test_la_segunda_ola_cierra_recolectado_y_avisa_otra_vez(self):
        pedido, la, lb = self.primera_ola_fuera()
        with self.captureOnCommitCallbacks(execute=True):
            self.entra(self.b, 5)
        pedido.refresh_from_db()
        services.iniciar_picking(pedido, self.operador)
        lb.refresh_from_db()
        services.confirmar_linea_pick(lb, 1, self.operador)
        caja = pedido.paquetes.get()
        services.empacar_caja(caja, self.operador, 2100, foto())
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)
        self.assertEqual(suma(self.a, Saldo.EN_EMPAQUE), 0)  # A no se reconfirma
        pedido.transicionar(Pedido.GUIA_GENERADA)
        with (
            patch("apps.mensajeria.services.enviar_en_camino") as en_camino,
            patch("apps.integraciones.services.marcar_fulfillment") as fulfillment,
            self.captureOnCommitCallbacks(execute=True),
        ):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()
        la.refresh_from_db()
        lb.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
        self.assertEqual((la.cantidad_despachada, lb.cantidad_despachada), (2, 1))
        self.assertEqual(suma(self.b, Saldo.UBICADO_VENDIBLE), 4)
        en_camino.assert_called_once()  # segundo "va en camino": es otra salida
        fulfillment.assert_called_once_with(pedido, cajas=[caja], notificar=True)
        self.assertFalse(pedido.pendiente_de_completar)

    def test_si_shopify_quita_la_faltante_el_pedido_se_cierra(self):
        pedido, _la, _lb = self.primera_ola_fuera()
        payload = {"line_items": [
            {"sku": "A-SIX", "current_quantity": 2},
            {"sku": "B-SIX", "current_quantity": 0},
        ]}
        services._aplicar_cambios_cantidades(pedido, payload, "webhook")
        pedido.refresh_from_db()
        self.assertFalse(pedido.lineas.filter(sku=self.b).exists())
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)


class EntregaSinGuiaParcialTests(BaseParcial):
    def test_rechaza_pedidos_con_faltantes(self):
        pedido, _, _ = self.pedido_parcial()
        with self.assertRaises(ValueError) as ctx:
            services.entregar_sin_guia(pedido, self.operador, recibio="Ana")
        self.assertIn("B-SIX", str(ctx.exception))
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
