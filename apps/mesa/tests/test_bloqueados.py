"""Reanudar DET desde el pedido y vista "Bloqueados" (Chema 2026-10-20/21):
el botón resuelve SOLO las DET con el motivo fijo "pedido reanudado"; el modal
avisa qué seguirá bloqueando (sin paquetería, producto no registrado, sin
plan, sin inventario); la pantalla lista los detenidos del más viejo al más
nuevo con su bloqueo y el botón correcto; badge en el menú."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.configuracion.models import CorreoIncidencias
from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_sin_paqueteria, bloqueos_de
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class BloqueadosTests(PisoTestCase):
    def setUp(self):
        super().setUp()
        CorreoIncidencias.objects.create(correo="ops@torre.mx")
        self.crear_stock(cantidad=20)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)

    def _detenido(self, motivo="No encuentro el producto", **extra):
        pedido = self.crear_pedido(cantidad=1, **extra)
        with self.captureOnCommitCallbacks(execute=True):
            services.detener_pedido(pedido, self.mesa, motivo, origen="mesa")
        pedido.refresh_from_db()
        return pedido

    def test_reanudar_desde_el_pedido_resuelve_solo_la_det(self):
        pedido = self._detenido()
        det = Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_DET)
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn('id="modal-reanudar"', html)
        self.assertIn("Nada más lo detiene", html)
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "reanudar_det", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "reanudado: vuelve a la cola")
        pedido.refresh_from_db(); det.refresh_from_db()
        self.assertEqual((pedido.detenido, det.estado), (False, Incidencia.RESUELTA))
        self.assertTrue(det.mensajes.filter(texto__contains="pedido reanudado").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="det_resuelta_desde_pedido").exists())
        self.assertNotIn('id="modal-reanudar"', self.client.get(url).content.decode())

    def test_con_otra_incidencia_viva_la_det_se_resuelve_pero_sigue_detenido(self):
        pedido = self._detenido()
        with self.captureOnCommitCallbacks(execute=True):
            abrir_sin_paqueteria(pedido, "ningún carrier cotiza el CP")
        pedido.refresh_from_db()
        url = reverse("mesa:pedido_detalle", args=[pedido.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("seguirá detenido por", html)
        self.assertIn("sin paquetería que cotice", html)
        self.assertIn("Resolver la DET", html)
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "reanudar_det", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "sigue detenido por: sin paquetería")
        pedido.refresh_from_db()
        self.assertTrue(pedido.detenido)
        self.assertEqual(Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_DET).estado, Incidencia.RESUELTA)
        self.assertEqual(Incidencia.objects.get(pedido=pedido, tipo=Incidencia.TIPO_PAQ).estado, Incidencia.ABIERTA)
        # Sin DET abierta, el botón ya no aparece y el POST avisa.
        self.assertNotIn('id="modal-reanudar"', self.client.get(url).content.decode())
        respuesta = self.client.post(url, {"accion": "reanudar_det", "folio": pedido.folio}, follow=True)
        self.assertContains(respuesta, "no tiene una incidencia DET abierta")

    def test_pantalla_bloqueados_lista_del_mas_viejo_al_mas_nuevo_con_su_boton(self):
        viejo = self._detenido("Producto dañado")
        nuevo = self._detenido("Otro")
        with self.captureOnCommitCallbacks(execute=True):
            abrir_sin_paqueteria(nuevo, "ningún carrier cotiza")
        solo_paq = self.crear_pedido(cantidad=1)
        with self.captureOnCommitCallbacks(execute=True):
            abrir_sin_paqueteria(solo_paq, "ningún carrier cotiza")
        url = reverse("mesa:bloqueados")
        respuesta = self.client.get(url)
        html = respuesta.content.decode()
        self.assertLess(html.index(viejo.folio), html.index(nuevo.folio))
        self.assertIn('Bloqueados <span class="pill crit">3</span>', html)
        self.assertIn("detenido en piso", html)
        self.assertIn("sin paquetería que cotice", html)
        self.assertIn('>Reanudar</button>', html)
        self.assertIn('>Resolver DET</button>', html)
        self.assertIn("se resuelve en su incidencia", html)
        self.assertEqual([b["tipo"] for b in bloqueos_de(nuevo)], ["DET", "PAQ"])
        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(url, {"accion": "reanudar_det", "folio": viejo.folio}, follow=True)
        self.assertRedirects(respuesta, url, fetch_redirect_response=False)
        viejo.refresh_from_db()
        self.assertFalse(viejo.detenido)
        self.assertNotContains(respuesta, reverse("mesa:pedido_detalle", args=[viejo.pk]))  # ya no está en la lista (el aviso sí lo nombra)
        # El dashboard enlaza a la pantalla.
        self.assertContains(self.client.get(reverse("mesa:dashboard")), reverse("mesa:bloqueados"))

    def test_piso_no_entra(self):
        self.client.force_login(self.operador)
        self.assertEqual(self.client.get(reverse("mesa:bloqueados")).status_code, 403)
