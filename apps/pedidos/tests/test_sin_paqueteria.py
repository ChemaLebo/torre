"""Ningún carrier cotiza (Chema 2026-09-24): no se compra una guía con el
pedido entero (cajas de 30 kg); nace la incidencia interna "Sin paquetería
que cotice" y Mesa fuerza una paquetería que replanea las cajas. Un pedido
empacado entero que queda con varias cajas se reempaca caja por caja."""
from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.adapters import ErrorCarrier, MockAdapter
from apps.envios.models import Guia, Paquete
from apps.envios.services import (
    CARRIER_POOL_ENVIA, ReempaquePendiente, SinPaqueteria, carrier_preferido, carriers_del_pedido,
    elegir_carrier,
)
from apps.incidencias.models import Incidencia
from apps.incidencias.services import sin_paqueteria_abierta
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase
from apps.piso.views import _que_falta_empaque

NADIE_COTIZA = {**settings.TORRE, "CARRIERS_COTIZAR": ["fantasma"], "CARRIER_PRIORITARIO": ""}
POOL_CLASICO = {**settings.TORRE, "CARRIERS_COTIZAR": ["estafeta", "fedex", "paquetexpress"], "CARRIER_PRIORITARIO": ""}


@override_settings(ENVIA_API_KEY="", TORRE=NADIE_COTIZA)
class SinPaqueteriaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)

    def test_al_planear_sin_carrier_nace_la_incidencia_interna(self):
        pedido = self.crear_pedido(cantidad=2)
        with self.captureOnCommitCallbacks(execute=True):
            services._planificar_best_effort(pedido)
        inc = sin_paqueteria_abierta(pedido)
        self.assertIsNotNone(inc)
        self.assertTrue(inc.interna)
        self.assertEqual(pedido.paquetes.count(), 0)

    def test_sin_plan_no_se_compra_guia_entera(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=12))  # 24 kg empacados "entero"
        with self.assertRaises(SinPaqueteria) as ctx:
            services.generar_guia(pedido)
        self.assertIsInstance(ctx.exception, ErrorCarrier)
        inc = sin_paqueteria_abierta(pedido)
        self.assertIn(inc.folio, str(ctx.exception))
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 0)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)
        with self.assertRaises(SinPaqueteria):
            services.generar_guia(pedido)  # el reintento no duplica la incidencia
        self.assertEqual(Incidencia.objects.filter(pedido=pedido, tipo=Incidencia.TIPO_PAQ).count(), 1)

    def test_mesa_fuerza_paqueteria_y_el_pedido_se_reempaca_por_caja(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=12))
        with self.assertRaises(SinPaqueteria):
            services.generar_guia(pedido)
        inc = sin_paqueteria_abierta(pedido)
        self.assertIsNotNone(pedido.asignado_a)
        with override_settings(TORRE=POOL_CLASICO):
            resultado = services.replanear_con_carrier(pedido, "estafeta", self.operador, incidencia=inc)
        pedido.refresh_from_db()
        inc.refresh_from_db()
        self.assertEqual(resultado["modo"], "replaneadas")
        self.assertEqual(len(resultado["cajas"]), 2)  # 24 kg → dos cajas ≤20 kg
        self.assertEqual({c.carrier for c in pedido.paquetes.all()}, {"estafeta"})
        self.assertEqual({c.estado for c in pedido.paquetes.all()}, {Paquete.PLANEADO})
        self.assertEqual((pedido.estado, pedido.carrier_forzado, pedido.asignado_a), (Pedido.EMPACADO, "estafeta", None))
        self.assertEqual(inc.estado, Incidencia.CERRADA)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="replaneo_carrier").exists())
        # La paquetería forzada manda en todo el ruteo.
        self.assertEqual(carriers_del_pedido(pedido), ["estafeta"])
        self.assertEqual(elegir_carrier(pedido), ("estafeta", "ground"))
        self.assertEqual(carrier_preferido(pedido), "estafeta")
        # El piso ve "sin empacar" y el wizard vuelve al paso de caja.
        self.assertEqual(_que_falta_empaque(pedido), "sin empacar: caja 1, 2")
        self.login_piso()
        respuesta = self.client.get(reverse("piso:empaque_pedido", args=[pedido.pk]))
        self.assertEqual(respuesta.context["paso"], "caja")
        # Reempaque caja por caja: sin segundo confirmar_pick ni transición.
        with override_settings(TORRE=POOL_CLASICO):
            for caja in pedido.paquetes.order_by("numero"):
                services.empacar_caja(caja, self.operador, int(caja.peso_kg * 1000), self.foto(f"c{caja.numero}.jpg"))
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)
        self.assertEqual({c.estado for c in pedido.paquetes.all()}, {Paquete.EMPACADO})
        self.assertEqual(pedido.peso_real_gr, sum(c.peso_real_gr for c in pedido.paquetes.all()))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="reempacado_por_caja").exists())
        self.assertEqual(_que_falta_empaque(pedido), "sin guía: caja 1, 2")
        with override_settings(TORRE=POOL_CLASICO):
            services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        self.assertEqual(sorted(g.carrier for g in pedido.guias.all()), ["estafeta", "estafeta"])

    def test_un_plan_de_una_caja_no_deshace_el_empaque(self):
        """Chema 2026-09-24: el reempaque solo toca a los que quedan en más de
        una caja; con una sola, la caja empacada ES la del plan."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))  # 4 kg: una caja
        with self.assertRaises(SinPaqueteria):
            services.generar_guia(pedido)
        with override_settings(TORRE=POOL_CLASICO):
            resultado = services.replanear_con_carrier(pedido, "estafeta", self.operador, incidencia=sin_paqueteria_abierta(pedido))
        self.assertEqual(len(resultado["cajas"]), 1)
        pedido.refresh_from_db()
        self.assertEqual(_que_falta_empaque(pedido), "sin guía")
        self.login_piso()
        respuesta = self.client.get(reverse("piso:empaque_pedido", args=[pedido.pk]))
        self.assertEqual(respuesta.context["paso"], "cierre")  # nada que volver a pesar
        with override_settings(TORRE=POOL_CLASICO):
            services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.guias.count()), (Pedido.GUIA_GENERADA, 1))

    @override_settings(TORRE=POOL_CLASICO)
    def test_plan_de_varias_cajas_al_pedir_guia_no_compra_hasta_pesarlas(self):
        """Pedido empacado entero cuyo plan nace al reintentar la guía con
        varias cajas: sin guías para cajas sin pesar; el wizard las pide."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=12))  # 24 kg: dos cajas
        with self.assertRaises(ReempaquePendiente):
            services.generar_guia(pedido)
        pedido.refresh_from_db()
        self.assertEqual((pedido.estado, pedido.paquetes.count(), pedido.guias.count()), (Pedido.EMPACADO, 2, 0))
        self.login_piso()
        self.assertEqual(self.client.get(reverse("piso:empaque_pedido", args=[pedido.pk])).context["paso"], "caja")

    def test_la_lista_de_envia_por_precio_como_paqueteria(self):
        pedido = self.crear_pedido(cantidad=2)
        with override_settings(TORRE=POOL_CLASICO):
            services.replanear_con_carrier(pedido, CARRIER_POOL_ENVIA, self.operador)
            pedido.refresh_from_db()
            self.assertEqual(pedido.carrier_forzado, "envia")
            self.assertEqual(carriers_del_pedido(pedido), ["estafeta", "fedex", "paquetexpress"])
            self.assertEqual(carrier_preferido(pedido), "")  # manda el precio
            self.assertEqual(pedido.paquetes.get().carrier, "estafeta")  # la más barata de la tabla mock

    def test_si_tampoco_cotiza_no_cambia_nada_y_queda_la_nota(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        with self.assertRaises(SinPaqueteria):
            services.generar_guia(pedido)
        inc = sin_paqueteria_abierta(pedido)
        with self.assertRaises(ValueError) as ctx:
            services.replanear_con_carrier(pedido, "fantasma", self.operador, incidencia=inc)
        self.assertIn("tampoco cotiza", str(ctx.exception))
        pedido.refresh_from_db()
        inc.refresh_from_db()
        self.assertEqual((pedido.carrier_forzado, pedido.paquetes.count(), inc.estado), ("", 0, Incidencia.ABIERTA))
        self.assertTrue(inc.mensajes.filter(texto__contains="tampoco cotiza").exists())
        with self.assertRaises(ValueError):
            services.replanear_con_carrier(pedido, "inventada", self.operador)  # fuera de la lista

    @override_settings(TORRE=POOL_CLASICO)
    def test_con_guia_comprada_cancela_y_recotiza_con_la_nueva(self):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=4, carrier="estafeta", estado=Paquete.EMPACADO)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        vieja = caja.guia_activa
        self.assertEqual((pedido.estado, vieja.carrier), (Pedido.GUIA_GENERADA, "estafeta"))
        resultado = services.replanear_con_carrier(pedido, "fedex", self.operador)
        pedido.refresh_from_db()
        caja.refresh_from_db()
        vieja.refresh_from_db()
        self.assertEqual(resultado["modo"], "recotizadas")
        self.assertEqual((pedido.estado, pedido.carrier_forzado, vieja.estado), (Pedido.EMPACADO, "fedex", Guia.CANCELADA))
        self.assertEqual((caja.carrier, caja.estado), ("fedex", Paquete.EMPACADO))  # la caja física se queda
        nueva = services.generar_guia(pedido)
        self.assertEqual(nueva.carrier, "fedex")
