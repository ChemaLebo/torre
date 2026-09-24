"""Incidencias internas (Chema 2026-09-24): son de la bodega, no del cliente.
No se pausan, no avisan al cliente, no marcan pedido.incidencia_activa y el
portal jamás las lista. "Sin paquetería que cotice" nace así, una por pedido."""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.core.models import EventoAuditoria
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia, abrir_sin_paqueteria, cerrar, sin_paqueteria_abierta

from .utils import crear_cliente, crear_pedido


class InternasTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente)

    def test_interna_no_avisa_ni_marca_el_pedido_ni_se_pausa(self):
        self.cliente.incidencias_auto_pausadas_hasta = timezone.localdate()
        self.cliente.save()
        with patch("apps.mensajeria.services.notificar_cliente_incidencia") as avisar:
            inc = abrir_incidencia(self.cliente, Incidencia.TIPO_PAQ, Incidencia.ORIGEN_AUTO,
                                   pedido=self.pedido, texto="Nadie cotiza", interna=True)
        self.assertIsNotNone(inc)  # la pausa no aplica a las internas
        self.assertTrue(inc.interna)
        avisar.assert_not_called()
        self.pedido.refresh_from_db()
        self.assertFalse(self.pedido.incidencia_activa)
        # Una pública sí avisa y sí marca.
        with patch("apps.mensajeria.services.notificar_cliente_incidencia") as avisar:
            abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL, pedido=self.pedido, texto="a mano")
        avisar.assert_called_once()
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.incidencia_activa)

    def test_cerrar_ignora_las_internas_para_el_flag(self):
        abrir_incidencia(self.cliente, Incidencia.TIPO_PAQ, Incidencia.ORIGEN_AUTO, pedido=self.pedido, texto="x", interna=True)
        publica = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_MANUAL, pedido=self.pedido, texto="y")
        publica.transicionar(Incidencia.RESUELTA)
        cerrar(publica, None)
        self.pedido.refresh_from_db()
        self.assertFalse(self.pedido.incidencia_activa)  # la interna abierta no lo retiene

    def test_sin_paqueteria_nace_p1_interna_y_no_se_duplica(self):
        primera = abrir_sin_paqueteria(self.pedido, "Ningún carrier cotiza el pedido PED-00001 a CP 45200")
        self.assertEqual((primera.tipo, primera.prioridad, primera.interna, primera.origen),
                         (Incidencia.TIPO_PAQ, Incidencia.P1, True, Incidencia.ORIGEN_AUTO))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk), accion="sin_paqueteria").exists())
        otra_vez = abrir_sin_paqueteria(self.pedido, "Ningún carrier cotiza el pedido PED-00001 a CP 45200")
        self.assertEqual(otra_vez.pk, primera.pk)
        self.assertEqual(primera.mensajes.count(), 1)  # mismo texto: sin nota repetida
        abrir_sin_paqueteria(self.pedido, "Tampoco con paquetes ≤20 kg")
        self.assertEqual(primera.mensajes.count(), 2)  # detalle nuevo: se anota
        self.assertEqual(Incidencia.objects.filter(tipo=Incidencia.TIPO_PAQ).count(), 1)
        self.assertEqual(sin_paqueteria_abierta(self.pedido).pk, primera.pk)
        primera.transicionar(Incidencia.RESUELTA)
        cerrar(primera, None)
        self.assertIsNone(sin_paqueteria_abierta(self.pedido))
