"""Incidencia "Cambio de dirección" en Mesa: botón para cancelar la guía y
regresar a empaque solo cuando nada ha salido y Torre ya tiene la dirección
nueva; si ya salió, el aviso en vez del botón (Chema 2026-09-23)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.core.services import registrar_evento
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

    def test_nace_p1_y_ofrece_el_boton_si_nada_ha_salido(self):
        self.assertEqual(self.incidencia.prioridad, Incidencia.P1)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Cancelar guía y regresar a empaquetado")

    def test_sin_direccion_nueva_en_torre_no_cancela(self):
        respuesta = self.client.post(self.url, {"accion": "regresar_a_empaque"}, follow=True)
        self.assertContains(respuesta, "aún no recibe la dirección nueva")
        self.guia.refresh_from_db()
        self.assertEqual(self.guia.estado, Guia.GUIA_CREADA)

    def test_con_direccion_nueva_cancela_y_regresa(self):
        registrar_evento("pedido", self.pedido.pk, "ingesta_repetida", actor="webhook", cliente=self.colima,
                         delta={"origen": "webhook", "campos": ["direccion", "cp"]})
        respuesta = self.client.post(self.url, {"accion": "regresar_a_empaque"}, follow=True)
        self.assertContains(respuesta, "regresó a empaquetado")
        self.pedido.refresh_from_db()
        self.guia.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.guia.estado), (Pedido.EMPACADO, Guia.CANCELADA))
        self.assertTrue(self.incidencia.mensajes.filter(interno=True, texto__contains="regresado a empaquetado").exists())
        self.assertNotContains(self.client.get(self.url), "Cancelar guía y regresar a empaquetado")

    def test_si_ya_salio_avisa_en_vez_del_boton(self):
        Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=2, carrier="estafeta", estado=Paquete.DESPACHADO)
        self.guia.paquete_id = Paquete.objects.get().pk
        self.guia.save(update_fields=["paquete"])
        Pedido.objects.filter(pk=self.pedido.pk).update(estado=Pedido.RECOLECTADO)
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, "Cancelar guía y regresar a empaquetado")
        self.assertContains(respuesta, "Ya salió")
        self.assertContains(respuesta, "estafeta EST-1")
