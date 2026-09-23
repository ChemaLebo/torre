"""Incidencia "Cambio de dirección" en Mesa: botón para cancelar la guía y
regresar a empaque solo cuando nada ha salido y Torre ya tiene la dirección
nueva; si ya salió, el aviso en vez del botón (Chema 2026-09-23)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.envios.models import Guia, Paquete
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.pedidos.models import Pedido


@override_settings(ENVIA_API_KEY="")
class CambioDireccionMesaTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        self.client.force_login(self.mesa)
        self.pedido = Pedido.objects.create(
            cliente=self.colima, origen="manual", comprador_nombre="Ana", cp="44100",
            direccion={"zip": "44100", "city": "Guadalajara", "province_code": "JAL"}, estado=Pedido.GUIA_GENERADA,
        )
        self.guia = Guia.objects.create(pedido=self.pedido, carrier="estafeta", numero="EST-1", proveedor="mock")
        self.incidencia = abrir_incidencia(
            self.colima, Incidencia.TIPO_CDR, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido,
            texto="Nueva dirección: Av. Vallarta 500, Guadalajara 44100",
        )
        self.url = reverse("mesa:incidencia_detalle", args=[self.incidencia.pk])

    def _con_direccion_pendiente(self):
        self.pedido.direccion_pendiente = {"address1": "Av. Vallarta 500", "city": "Guadalajara", "province_code": "JAL", "zip": "44100"}
        self.pedido.save(update_fields=["direccion_pendiente"])

    def test_nace_p1_y_ofrece_el_boton_si_nada_ha_salido(self):
        self.assertEqual(self.incidencia.prioridad, Incidencia.P1)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Cancelar guía y regresar a empaquetado")
        self.assertContains(respuesta, "Shopify todavía no manda una dirección distinta")
        self._con_direccion_pendiente()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "La guía va a: <strong>Guadalajara, JAL, 44100</strong>")
        self.assertContains(respuesta, "Shopify ahora dice: <strong>Av. Vallarta 500, Guadalajara, JAL, 44100</strong>")

    def test_sin_direccion_nueva_en_torre_no_cancela(self):
        respuesta = self.client.post(self.url, {"accion": "regresar_a_empaque"}, follow=True)
        self.assertContains(respuesta, "aún no recibe una dirección distinta")
        self.guia.refresh_from_db()
        self.assertEqual(self.guia.estado, Guia.GUIA_CREADA)

    def test_con_direccion_nueva_cancela_y_regresa(self):
        self._con_direccion_pendiente()
        respuesta = self.client.post(self.url, {"accion": "regresar_a_empaque"}, follow=True)
        self.assertContains(respuesta, "regresó a empaquetado")
        self.pedido.refresh_from_db()
        self.guia.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.guia.estado), (Pedido.EMPACADO, Guia.CANCELADA))
        self.assertEqual(self.pedido.direccion["address1"], "Av. Vallarta 500")
        self.assertIsNone(self.pedido.direccion_pendiente)
        self.assertTrue(self.incidencia.mensajes.filter(interno=True, texto__contains="Av. Vallarta 500").exists())
        self.assertNotContains(self.client.get(self.url), "Cancelar guía y regresar a empaquetado")

    def test_si_ya_salio_avisa_en_vez_del_boton(self):
        Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=2, carrier="estafeta", estado=Paquete.DESPACHADO)
        self.guia.paquete_id = Paquete.objects.get().pk
        self.guia.save(update_fields=["paquete"])
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.RECOLECTADO)
        self._con_direccion_pendiente()
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, "Cancelar guía y regresar a empaquetado")
        self.assertContains(respuesta, "Ya salió")
        self.assertContains(respuesta, "estafeta EST-1")
        self.assertContains(respuesta, "El pedido conserva la dirección a la que viajó")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.direccion["zip"], "44100")
