"""Mesa → Operación → Entregas locales (Chema 2026-10-05): el POD de la
entrega propia es administrativo y vive en Mesa, no en Piso. Lista de cajas
en reparto, por salir y entregadas hoy; POD por caja con foto y quién
recibió (sin verificación de edad), reutilizando la foto del POD anterior
si salieron juntas."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.core.models import EvidenciaFoto, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class EntregasLocalesMesaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def pedido_local(self, cantidad=4, cajas=2):
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=cantidad, es_local=True))
        linea = pedido.lineas.get()
        for n in range(1, cajas + 1):
            caja = Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal("4"), carrier="local", carrier_forzado="local",
                servicio="entrega_local", estado=Paquete.EMPACADO, ts_cierre=timezone.now(),
            )
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=cantidad // cajas)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        return pedido

    def en_reparto(self, pedido):
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()
        return pedido

    def test_lista_en_reparto_por_salir_y_entregadas_hoy(self):
        fuera = self.en_reparto(self.pedido_local())
        en_bodega = self.pedido_local(cantidad=2, cajas=1)
        otro = self.crear_pedido(cantidad=1, estado=Pedido.RECOLECTADO, reservar_stock=False)  # carrier real: no aparece
        Guia.objects.create(pedido=otro, carrier="estafeta", numero="ETQ-1")
        respuesta = self.client.get(reverse("mesa:entregas_locales"))
        html = respuesta.content.decode()
        self.assertContains(respuesta, "Entregas locales")
        self.assertIn(fuera.folio, html)
        self.assertIn("caja 1, caja 2", html)
        self.assertIn(reverse("mesa:entrega_local_pedido", args=[fuera.pk]), html)
        self.assertIn(en_bodega.folio, html)  # por salir
        self.assertNotIn(otro.folio, html)
        self.assertIn("Todavía nada entregado hoy", html)
        # El menú de Mesa la lista; en Piso ya no existe.
        self.assertIn(reverse("mesa:entregas_locales"), html)
        with self.assertRaises(NoReverseMatch):
            reverse("piso:entrega_local")
        self.client.force_login(self.operador)
        self.assertNotIn("Ver POD", self.client.get(reverse("piso:home")).content.decode())
        self.assertNotIn("Entregas locales (POD)", self.client.get(reverse("piso:salida")).content.decode())

    def test_pod_por_caja_con_foto_reutilizada_y_pedido_entregado_al_final(self):
        pedido = self.en_reparto(self.pedido_local())
        g1, g2 = services.guias_entrega_propia(pedido)
        url = reverse("mesa:entrega_local_pedido", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertEqual(html.count('name="foto_pod"'), 2)  # un formulario por caja
        self.assertNotIn('id="misma-', html)  # todavía no hay POD que reutilizar
        self.assertNotIn("mayoría de edad", html)  # sin verificación de edad (Chema 2026-10-05)
        # Sin quién recibió, no se cierra.
        respuesta = self.client.post(url, {"guia": g1.pk, "recibio": "", "foto_pod": self.foto("pod.jpg")}, follow=True)
        self.assertContains(respuesta, "quien recibió")
        with patch("apps.integraciones.services.registrar_evento_fulfillment"), self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"guia": g1.pk, "recibio": "Dulce Pérez", "foto_pod": self.foto("pod.jpg")}, follow=True)
        self.assertContains(respuesta, "Caja 1 de " + pedido.folio + " entregada a Dulce Pérez; faltan cajas")
        html = respuesta.content.decode()
        self.assertEqual(html.count('name="foto_pod"'), 1)  # solo la caja 2 sigue abierta
        self.assertIn("Usar la misma foto del POD anterior", html)
        self.assertIn("recibió Dulce Pérez", html)
        e1 = EvidenciaFoto.objects.get(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod")
        self.assertIn(reverse("core:evidencia", args=[e1.pk]), html)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
        # Caja 2 con la misma foto: registro propio, pedido entregado y de vuelta a la lista.
        with patch("apps.integraciones.services.registrar_evento_fulfillment"), self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"guia": g2.pk, "recibio": "Dulce Pérez", "misma_foto": "si", "reusar": e1.pk}, follow=True)
        self.assertContains(respuesta, "Todas las cajas entregadas")
        self.assertRedirects(respuesta, reverse("mesa:entregas_locales"), fetch_redirect_response=False)
        self.assertEqual(EvidenciaFoto.objects.filter(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod").count(), 2)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.ENTREGADO)
        html = respuesta.content.decode()
        self.assertIn("Dulce Pérez", html)  # entregadas hoy, con quién recibió
        self.assertNotIn("Registrar entrega", html)
        # El detalle del pedido ya no ofrece el POD; antes de entregar sí lo ligaba por caja.
        self.assertNotIn("Registrar entrega (POD)", self.client.get(reverse("mesa:pedido_detalle", args=[pedido.pk])).content.decode())

    def test_el_detalle_del_pedido_liga_el_pod_por_caja_y_las_fotos_viajan_al_rastreo(self):
        pedido = self.en_reparto(self.pedido_local())
        html = self.client.get(reverse("mesa:pedido_detalle", args=[pedido.pk])).content.decode()
        self.assertEqual(html.count("Registrar entrega (POD)"), 2)
        self.assertIn(reverse("mesa:entrega_local_pedido", args=[pedido.pk]), html)
        # Una foto de otro pedido no se puede reutilizar aquí (404 por el filtro del pedido).
        ajeno = self.crear_pedido(cantidad=1, es_local=True)
        ajena = EvidenciaFoto.objects.create(entidad="entrega_local", entidad_id=str(ajeno.pk), tipo="pod", archivo=self.foto("x.jpg"))
        g1, _ = services.guias_entrega_propia(pedido)
        respuesta = self.client.post(reverse("mesa:entrega_local_pedido", args=[pedido.pk]), {"guia": g1.pk, "recibio": "Juan", "misma_foto": "si", "reusar": ajena.pk})
        self.assertEqual(respuesta.status_code, 404)
        # Un pedido sin entrega propia no tiene POD.
        respuesta = self.client.get(reverse("mesa:entrega_local_pedido", args=[ajeno.pk]), follow=True)
        self.assertContains(respuesta, "no tiene cajas de entrega propia")
