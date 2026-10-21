"""Empaque en piso, contrato del carril único: checklist por cliente, kits con
reserva real, y el candado "sin plan de cajas no se empaca" (Chema 2026-10-20).
El empaque por caja vive en test_wizard_cajas; el empaque entero ya no existe."""
from unittest.mock import patch

from django.conf import settings
from django.db.models import Sum
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EvidenciaFoto
from apps.envios.models import Guia
from apps.inventario.models import Saldo
from apps.pedidos.models import Pedido

from .base import PisoTestCase

# La lista blanca de producción cambia con el negocio; estas pruebas planean con los carriers de la tabla mock.
TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


class EmpaqueKitTests(PisoTestCase):
    """Kit (7A·2): candado de contenido + declaración con reserva REAL del kardex."""

    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)  # stock real del té (self.sku)
        from apps.catalogo.models import SKU as ModeloSKU
        self.kit_sku = ModeloSKU.objects.create(
            cliente=self.cliente, codigo="TEABOX", descripcion="TeaBox",
            peso_gr=400, es_kit=True, requiere_lote=False,
        )
        from apps.pedidos.models import LineaPedido
        self.pedido = Pedido.objects.create(
            cliente=self.cliente, origen="manual", estado=Pedido.PENDIENTE,
            comprador_nombre="Ana Prueba", cp="44100",
            direccion={"address1": "Av. Prueba 123", "city": "Guadalajara"},
            peso_esperado_gr=400,
        )
        self.linea_kit = LineaPedido.objects.create(
            pedido=self.pedido, sku=self.kit_sku, cantidad=1, reservada=True,
        )
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        confirmar_linea_pick(self.linea_kit, 1, self.operador)
        self.url = reverse("piso:empaque_pedido", args=[self.pedido.pk])

    def _reservado(self):
        return (
            Saldo.objects.filter(sku=self.sku, estado="reservado")
            .aggregate(total=Sum("cantidad"))["total"] or 0
        )

    def test_candado_declaracion_y_reserva_real(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "falta declarar")

        caja = self.pedido.paquetes.get()  # el plan nació al abrir el wizard (sin plan no se empaca)
        bloqueo = self.client.post(self.url, {
            "accion": "empacar_caja", "paquete_id": caja.pk, "peso_real_gr": "400", "foto_contenido": self.foto(),
        }, follow=True)
        self.assertContains(bloqueo, "Declara el contenido")

        declarar = self.client.post(self.url, {
            "accion": "contenido_kit", "linea_kit": self.linea_kit.pk,
            "sku_1": self.sku.pk, "cantidad_1": "2",
        }, follow=True)
        self.assertContains(declarar, "declarado")
        hija = self.linea_kit.componentes.get()
        self.assertEqual(hija.cantidad, 2)
        self.assertEqual(hija.cantidad_pickeada, 2)  # nace pickeada
        self.assertEqual(self._reservado(), 2)  # kardex REAL apartado
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.peso_esperado_gr, 400 + 2 * 2000)

    def test_quitar_regresa_el_stock_al_pool(self):
        self.client.post(self.url, {
            "accion": "contenido_kit", "linea_kit": self.linea_kit.pk,
            "sku_1": self.sku.pk, "cantidad_1": "2",
        })
        self.assertEqual(self._reservado(), 2)
        quitar = self.client.post(self.url, {
            "accion": "quitar_contenido_kit", "linea_kit": self.linea_kit.pk,
        }, follow=True)
        self.assertContains(quitar, "liberado")
        self.assertEqual(self.linea_kit.componentes.count(), 0)
        self.assertEqual(self._reservado(), 0)


@override_settings(TORRE=TORRE_CARRIERS_CLASICOS)
class EmpaquePisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=3)  # peso esperado: 6000 g
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        confirmar_linea_pick(self.pedido.lineas.get(), 3, self.operador)
        self.pedido.refresh_from_db()
        self.url = reverse("piso:empaque_pedido", args=[self.pedido.pk])

    def test_checklist_foraneo_exige_insumos_del_cliente(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "insumos oficiales")
        self.assertContains(respuesta, self.pedido.cliente.nombre)

    def test_checklist_local_avisa_naked_packing(self):
        local = self.crear_pedido(cantidad=1, es_local=True)
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(local, self.operador)
        confirmar_linea_pick(local.lineas.get(), 1, self.operador)
        respuesta = self.client.get(reverse("piso:empaque_pedido", args=[local.pk]))
        self.assertContains(respuesta, "NAKED PACKING")
        self.assertContains(respuesta, "JAMÁS cinta del 3PL")

    def test_sin_plan_de_cajas_no_se_empaca_abre_paq_y_detiene(self):
        """Candado (Chema 2026-10-20): si llega a empaque sin cajas y nadie cotiza,
        nace la PAQ y el pedido se detiene ANTES de pesar nada; el empaque entero ya no existe."""
        from apps.core.models import EventoAuditoria
        from apps.envios.cotizador import SinCotizacion
        from apps.incidencias.models import Incidencia

        with patch("apps.envios.cotizador.planificar_envio", side_effect=SinCotizacion("Ningún carrier cotiza el pedido")):
            respuesta = self.client.get(self.url, follow=True)
        self.assertContains(respuesta, "no tiene paquetería que cotice")
        self.pedido.refresh_from_db()
        self.assertEqual(
            (self.pedido.estado, self.pedido.detenido, self.pedido.asignado_a, self.pedido.paquetes.count()),
            (Pedido.EN_PICKING, True, None, 0),
        )
        paq = Incidencia.objects.get(pedido=self.pedido, tipo=Incidencia.TIPO_PAQ)
        self.assertTrue(paq.interna)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk), accion="empaque_sin_plan").exists())
        # Ni por POST: detenido, el wizard lo regresa a Mi turno sin tocar nada.
        self.client.post(self.url, {"peso_real_gr": "6100", "foto_contenido": self.foto("contenido.jpg")})
        self.pedido.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.pedido.peso_real_gr), (Pedido.EN_PICKING, None))

    def test_con_plan_recien_hecho_el_wizard_abre_la_caja_1(self):
        """Red de seguridad: sin cajas pero con carriers que cotizan, el plan nace aquí y sigue el wizard por caja."""
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["paso"], "caja")
        self.assertGreaterEqual(self.pedido.paquetes.count(), 1)
        self.assertContains(respuesta, 'value="empacar_caja"')
        self.assertNotContains(respuesta, 'value="empacar"')


class ListaEmpaqueTests(PisoTestCase):
    """La lista de Empaque (Chema 2026-09-22): lo listo para empacar y lo que
    está en empaquetado con lo que le falta; lo que sigue en picking no va
    aquí. Del operador; Mesa ve todos."""

    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.url = reverse("piso:empaque")

    def test_listo_y_en_empaquetado_con_lo_que_falta(self):
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking

        listo = self.crear_pedido(cantidad=1)
        iniciar_picking(listo, self.operador)
        confirmar_linea_pick(listo.lineas.get(), 1, self.operador)
        a_medias = self.crear_pedido(cantidad=2)
        iniciar_picking(a_medias, self.operador)
        empacado = self.dejar_empacado(self.crear_pedido(cantidad=1))  # sin guía
        respuesta = self.client.get(self.url)
        self.assertEqual([p.pk for p in respuesta.context["listos"]], [listo.pk])
        self.assertEqual([p.pk for p in respuesta.context["en_empaque"]], [empacado.pk])
        self.assertContains(respuesta, "En empaquetado")
        self.assertContains(respuesta, "sin guía")
        self.assertContains(respuesta, "CONTINUAR en Mi turno")  # lista informativa: sin botón por pedido
        self.assertNotContains(respuesta, f"empaque/{empacado.pk}/")
        self.assertNotContains(respuesta, a_medias.folio)  # el picking a medias vive en Picking
        self.assertNotContains(respuesta, "Terminar picking")

    def test_lo_de_otro_operador_no_es_mio_pero_mesa_lo_ve(self):
        from django.contrib.auth import get_user_model

        from apps.core.models import PerfilUsuario

        otro = get_user_model().objects.create_user("piso2", password="x")
        PerfilUsuario.objects.create(usuario=otro, rol=PerfilUsuario.ROL_PISO, pin="2222")
        empacado = self.dejar_empacado(self.crear_pedido(cantidad=1))
        Pedido.objects.filter(pk=empacado.pk).update(asignado_a=otro)
        self.assertNotContains(self.client.get(self.url), empacado.folio)
        mesa = get_user_model().objects.create_user("mesa1", password="x")
        PerfilUsuario.objects.create(usuario=mesa, rol=PerfilUsuario.ROL_MESA, pin="3333")
        self.client.force_login(mesa)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, empacado.folio)
        self.assertContains(respuesta, "piso2")


class DetenerEnEmpaqueTests(PisoTestCase):
    """Detener también vive en el wizard de empaque (Chema 2026-10-06)."""

    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)

    def test_detener_desde_el_wizard(self):
        from apps.incidencias.models import Incidencia

        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        url = reverse("piso:empaque_pedido", args=[pedido.pk])
        respuesta = self.client.get(url)
        self.assertContains(respuesta, 'value="detener"')
        self.assertContains(respuesta, '<option value="No encuentro el producto">')  # el dropdown trae motivos en todos los pasos (2026-10-07)
        respuesta = self.client.post(url, {"accion": "detener", "motivo": "No cabe / falta insumo de empaque"}, follow=True)
        self.assertRedirects(respuesta, reverse("piso:home"), fetch_redirect_response=False)
        pedido.refresh_from_db()
        self.assertEqual((pedido.detenido, pedido.asignado_a, pedido.estado), (True, None, Pedido.EMPACADO))
        self.assertTrue(Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_DET, interna=True).exists())
        self.assertNotContains(self.client.get(reverse("piso:empaque")), "Completar")  # no está en la mesa
        self.assertRedirects(self.client.get(url), reverse("piso:home"), fetch_redirect_response=False)
        respuesta = self.client.post(url, {"accion": "detener", "motivo": "Otro", "detalle": ""}, follow=True)
        self.assertContains(respuesta, "no está en tu cola")  # ya detenido: ni se abre
