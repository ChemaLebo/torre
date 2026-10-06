"""POD de entrega propia POR CAJA (Chema 2026-10-05): cada caja que sale con
guía interna "local" cierra con su propia foto y quién recibió (sin
verificación de edad); la foto del POD anterior se puede reutilizar para la
siguiente caja, pero cada caja queda con su registro. El pedido se entrega
solo cuando todas sus guías activas lo están."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.core.models import EventoAuditoria, EvidenciaFoto, PerfilUsuario
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import Pedido
from apps.piso.tests.base import PisoTestCase


@override_settings(ENVIA_API_KEY="")
class PodPorCajaTests(PisoTestCase):
    def setUp(self):
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=40)
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")

    def dos_cajas_locales_en_reparto(self):
        """Pedido en dos cajas con guía interna "local", ya con manifiesto (RECOLECTADO)."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=4, es_local=True))
        linea = pedido.lineas.get()
        cajas = []
        for n in (1, 2):
            caja = Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal("4"), carrier="local", carrier_forzado="local",
                servicio="entrega_local", estado=Paquete.EMPACADO, ts_cierre=timezone.now(),
            )
            PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
            cajas.append(caja)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"), self.captureOnCommitCallbacks(execute=True):
            services.marcar_recolectado(pedido, self.operador)
        pedido.refresh_from_db()
        guias = services.guias_entrega_propia(pedido)
        self.assertEqual([g.paquete.numero for g in guias], [1, 2])
        self.assertTrue(all(g.numero.startswith("LOCAL-") for g in guias))
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)
        return pedido, guias[0], guias[1]

    def test_cada_caja_cierra_con_su_pod_y_el_pedido_al_final(self):
        pedido, g1, g2 = self.dos_cajas_locales_en_reparto()
        self.assertEqual(services.motivo_sin_pod(g1), "")
        with patch("apps.integraciones.services.registrar_evento_fulfillment") as shopify, self.captureOnCommitCallbacks(execute=True):
            e1 = services.registrar_pod_caja(g1, self.mesa, foto=self.foto("pod.jpg"), recibio="Dulce Pérez")
        shopify.assert_called_once()
        self.assertEqual(shopify.call_args.args[1:3], (g1, Guia.ENTREGADO))
        pedido.refresh_from_db()
        g1.refresh_from_db()
        g2.refresh_from_db()
        # Una caja entregada no entrega el pedido.
        self.assertEqual((g1.estado, g2.estado, pedido.estado), (Guia.ENTREGADO, Guia.GUIA_CREADA, Pedido.RECOLECTADO))
        self.assertEqual((e1.entidad, e1.entidad_id, e1.tipo, e1.tomada_por), ("entrega_local", str(pedido.pk), "pod", "mesa1"))
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="pod_entrega_local")
        self.assertEqual((evento.delta["caja"], evento.delta["guia"], evento.delta["receptor"], evento.delta["evidencia_id"], evento.delta["foto_reutilizada"]), (1, g1.numero, "Dulce Pérez", e1.pk, False))
        self.assertIn("ya está entregada", services.motivo_sin_pod(g1))
        # La segunda caja reutiliza la foto: registro propio, mismo archivo y misma huella.
        with patch("apps.integraciones.services.registrar_evento_fulfillment"), self.captureOnCommitCallbacks(execute=True):
            e2 = services.registrar_pod_caja(g2, self.mesa, recibio="Dulce Pérez", reusar=e1)
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual((e2.archivo.name, e2.hash_sha256), (e1.archivo.name, e1.hash_sha256))
        self.assertEqual(EvidenciaFoto.objects.filter(entidad="entrega_local", entidad_id=str(pedido.pk), tipo="pod").count(), 2)
        pedido.refresh_from_db()
        g2.refresh_from_db()
        self.assertEqual((g2.estado, pedido.estado), (Guia.ENTREGADO, Pedido.ENTREGADO))
        self.assertIsNotNone(pedido.ts_entregado)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="pod_entrega_local", delta__foto_reutilizada=True).exists())

    def test_validaciones_del_pod(self):
        pedido, g1, g2 = self.dos_cajas_locales_en_reparto()
        with self.assertRaisesMessage(ValueError, "quien recibió"):
            services.registrar_pod_caja(g1, self.mesa, foto=self.foto(), recibio="  ")
        with self.assertRaisesMessage(ValueError, "Falta la foto"):
            services.registrar_pod_caja(g1, self.mesa, recibio="Dulce")
        # Una foto de otro pedido no se reutiliza.
        otro = self.crear_pedido(cantidad=1, es_local=True)
        ajena = EvidenciaFoto.objects.create(entidad="entrega_local", entidad_id=str(otro.pk), tipo="pod", archivo=self.foto("x.jpg"))
        with self.assertRaisesMessage(ValueError, "no es un POD de este pedido"):
            services.registrar_pod_caja(g1, self.mesa, recibio="Dulce", reusar=ajena)
        # Una guía de carrier la cierra el rastreo; una caja que no ha salido no tiene POD.
        foranea = Guia.objects.create(pedido=pedido, carrier="estafeta", numero="ETQ-9")
        self.assertIn("no de entrega propia", services.motivo_sin_pod(foranea))
        Paquete.objects.filter(pk=g1.paquete_id).update(estado=Paquete.EMPACADO)
        g1 = Guia.objects.select_related("paquete", "pedido").get(pk=g1.pk)
        self.assertIn("aún no sale a reparto", services.motivo_sin_pod(g1))
        with self.assertRaisesMessage(ValueError, "aún no sale a reparto"):
            services.registrar_pod_caja(g1, self.mesa, foto=self.foto(), recibio="Dulce")
        self.assertFalse(EvidenciaFoto.objects.filter(entidad="entrega_local", entidad_id=str(pedido.pk)).exists())
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.RECOLECTADO)

    def test_pedido_sin_plan_de_cajas_cierra_con_un_pod(self):
        # Legacy: una sola guía interna del pedido, sin caja (la línea ya salió: reservada y despachada).
        pedido = self.crear_pedido(cantidad=1, es_local=True, estado=Pedido.RECOLECTADO, reservar_stock=False)
        pedido.lineas.update(reservada=True, cantidad_pickeada=1, cantidad_despachada=1)
        guia = Guia.objects.create(pedido=pedido, carrier="local", proveedor="local", numero=f"LOCAL-{pedido.folio}")
        self.assertEqual(services.motivo_sin_pod(guia), "")
        with patch("apps.integraciones.services.registrar_evento_fulfillment"), self.captureOnCommitCallbacks(execute=True):
            services.registrar_pod_caja(guia, self.mesa, foto=self.foto(), recibio="Juan")
        pedido.refresh_from_db()
        guia.refresh_from_db()
        self.assertEqual((guia.estado, pedido.estado), (Guia.ENTREGADO, Pedido.ENTREGADO))
        self.assertIsNone(EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="pod_entrega_local").delta["caja"])
