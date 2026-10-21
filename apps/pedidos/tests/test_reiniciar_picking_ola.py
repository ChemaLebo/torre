"""reiniciar_picking en una ola de reposición (PED-00528, Chema 2026-10-21):
las cajas de olas anteriores —despachadas o fantasma— no impiden el reinicio,
y "cero" es lo que ya salió (cantidad_despachada), no 0. Una caja pesada de
ESTA ola sí lo impide."""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import Pedido, PickLinea
from apps.piso.tests.base import PisoTestCase

TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


@override_settings(ENVIA_API_KEY="", TORRE=TORRE_CARRIERS_CLASICOS)
class ReiniciarPickingOlaTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=20)
        self.otro = self.crear_operador("otro")

    def crear_operador(self, nombre):
        from django.contrib.auth import get_user_model
        from apps.core.models import PerfilUsuario
        usuario = get_user_model().objects.create_user(nombre, password="x12345678")
        PerfilUsuario.objects.create(usuario=usuario, rol="piso")
        return usuario

    def _entregado_y_repuesto(self):
        """Pedido entregado (caja 1 despachada) con una reposición de 1 pieza en picking, ya escaneada."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=1))
        linea = pedido.lineas.get()
        caja1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta", estado=Paquete.EMPACADO)
        PaqueteLinea.objects.create(paquete=caja1, linea_pedido=linea, cantidad=1)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"):
            services.marcar_recolectado(pedido, self.operador)
        Guia.objects.filter(paquete=caja1).update(estado=Guia.ENTREGADO, ts_recolectado_carrier=timezone.now())
        pedido.refresh_from_db()
        pedido.transicionar(Pedido.ENTREGADO)
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 1, caja1)], self.operador)
        pedido.refresh_from_db()
        services.iniciar_picking(pedido, self.operador)
        linea.refresh_from_db()
        services.confirmar_linea_pick(linea, 1, self.operador, ubicacion=self.ubic_picking)
        pedido.refresh_from_db()
        linea.refresh_from_db()
        caja1.refresh_from_db()
        self.assertEqual((caja1.estado, linea.cantidad_pickeada, linea.cantidad_despachada, pedido.lineas_completas), (Paquete.DESPACHADO, 2, 1, True))
        return pedido, linea, caja1

    def test_la_caja_despachada_de_la_ola_anterior_no_impide_el_reinicio_y_cero_es_lo_despachado(self):
        pedido, linea, caja1 = self._entregado_y_repuesto()
        self.assertTrue(services.reiniciar_picking(pedido, self.otro))
        linea.refresh_from_db()
        caja1.refresh_from_db()
        pedido.refresh_from_db()
        self.assertEqual((linea.cantidad_pickeada, linea.cantidad_despachada, linea.por_pickear), (1, 1, 1))
        self.assertFalse(pedido.lineas_completas)  # vuelve a picking: 0 de 1
        self.assertEqual(caja1.estado, Paquete.DESPACHADO)  # la que ya salió ni se toca
        self.assertFalse(PickLinea.objects.filter(linea=linea, estado=PickLinea.PICKEADO).exists())
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="picking_reiniciado")
        self.assertEqual(evento.delta["avance_anterior"], [{"sku": self.sku.codigo, "pickeada": 2, "despachada": 1}])

    def test_una_caja_fantasma_tampoco_lo_impide(self):
        pedido, linea, caja1 = self._entregado_y_repuesto()
        Paquete.objects.filter(pk=caja1.pk).update(estado=Paquete.PLANEADO)  # dato viejo: quedó planeada con guía entregada
        self.assertTrue(Paquete.objects.get(pk=caja1.pk).ya_salio)
        self.assertTrue(services.reiniciar_picking(pedido, self.otro))
        linea.refresh_from_db()
        self.assertEqual(linea.cantidad_pickeada, 1)

    def test_una_caja_pesada_de_esta_ola_si_lo_impide(self):
        pedido, linea, caja1 = self._entregado_y_repuesto()
        caja2 = pedido.paquetes.get(numero=2)
        Paquete.objects.filter(pk=caja2.pk).update(estado=Paquete.EMPACADO)
        self.assertFalse(services.reiniciar_picking(pedido, self.otro))
        linea.refresh_from_db()
        self.assertEqual(linea.cantidad_pickeada, 2)

    def test_al_tomarlo_otro_operador_el_piso_lo_manda_a_picking_en_0_de_1(self):
        from django.urls import reverse
        pedido, linea, caja1 = self._entregado_y_repuesto()
        services.detener_pedido(pedido, self.operador, "Detener a petición de Chema")
        services.reanudar_pedido(pedido, self.operador)
        self.client.force_login(self.otro)
        respuesta = self.client.post(reverse("piso:home"), {"accion": "siguiente", "pedido_id": pedido.pk, "empezado": "1"})
        self.assertRedirects(respuesta, reverse("piso:picking_pedido", args=[pedido.pk]), fetch_redirect_response=False)
        pagina = self.client.get(reverse("piso:picking_pedido", args=[pedido.pk]))
        self.assertContains(pagina, "0 de 1")
        self.assertContains(pagina, "el avance se reinicia")
