"""Servicios del contrato: abrir, responder, resolver, cerrar."""
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.core.models import EventoAuditoria, EvidenciaFoto
from apps.incidencias.models import Incidencia, MensajeIncidencia
from apps.incidencias.services import abrir_incidencia, cerrar, resolver, responder

from .utils import crear_cliente, crear_pedido


class ResponderTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.incidencia = abrir_incidencia(
            self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, texto="Llegó una caja rota."
        )

    def test_nota_interna_no_estampa_primera_respuesta(self):
        responder(
            self.incidencia, "mesa1", MensajeIncidencia.ROL_MESA,
            "Nota interna: revisar evidencia del empaque.", interno=True,
        )
        self.incidencia.refresh_from_db()
        self.assertIsNone(self.incidencia.ts_primera_respuesta)
        # La nota interna SÍ queda en el registro (privada pero auditable).
        self.assertTrue(self.incidencia.mensajes.filter(interno=True).exists())

    def test_auto_acuse_del_sistema_no_estampa(self):
        responder(
            self.incidencia, "Torre", MensajeIncidencia.ROL_SISTEMA,
            "Recibimos tu reporte. Un humano te responde en menos de 2 horas.",
        )
        self.incidencia.refresh_from_db()
        self.assertIsNone(self.incidencia.ts_primera_respuesta)

    def test_mensaje_entrante_del_cliente_no_estampa(self):
        responder(self.incidencia, "Karina", MensajeIncidencia.ROL_CLIENTE, "¿Alguna novedad?")
        self.incidencia.refresh_from_db()
        self.assertIsNone(self.incidencia.ts_primera_respuesta)

    def test_primera_respuesta_humana_de_mesa_estampa(self):
        mensaje = responder(
            self.incidencia, "mesa1", MensajeIncidencia.ROL_MESA,
            "Hola Karina, soy Diego. Ya estamos revisando el caso.",
        )
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.ts_primera_respuesta, mensaje.ts)

    def test_segunda_respuesta_no_mueve_la_estampa(self):
        primero = responder(self.incidencia, "mesa1", MensajeIncidencia.ROL_MESA, "Primera respuesta.")
        responder(self.incidencia, "mesa1", MensajeIncidencia.ROL_MESA, "Seguimiento.")
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.ts_primera_respuesta, primero.ts)

    def test_responder_registra_evento_auditoria(self):
        responder(self.incidencia, "mesa1", MensajeIncidencia.ROL_MESA, "Respuesta.", interno=False)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="incidencia", entidad_id=self.incidencia.folio, accion="responder"
            ).exists()
        )


class AbrirIncidenciaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()

    def test_abrir_registra_evento_y_mensaje_inicial(self):
        incidencia = abrir_incidencia(
            self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_COMPRADOR,
            texto="Mi pedido no ha llegado.",
        )
        self.assertEqual(incidencia.estado, Incidencia.ABIERTA)
        mensaje = incidencia.mensajes.get()
        self.assertEqual(mensaje.rol_autor, MensajeIncidencia.ROL_COMPRADOR)
        self.assertFalse(mensaje.interno)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="incidencia", entidad_id=incidencia.folio, accion="abrir"
            ).exists()
        )

    def test_prioridad_default_por_tipo_y_override(self):
        dan = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL)
        des = abrir_incidencia(self.cliente, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO)
        explicita = abrir_incidencia(
            self.cliente, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO, prioridad=Incidencia.P1
        )
        self.assertEqual(dan.prioridad, Incidencia.P1)
        self.assertEqual(des.prioridad, Incidencia.P3)
        self.assertEqual(explicita.prioridad, Incidencia.P1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="torre-test-media-"))
class IncidenciasConPedidoTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente)

    def test_n_incidencias_por_pedido_permitidas(self):
        dan = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido)
        ret = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_COMPRADOR, pedido=self.pedido)
        self.assertEqual(Incidencia.objects.filter(pedido=self.pedido).count(), 2)
        self.assertNotEqual(dan.folio, ret.folio)
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.incidencia_activa)

    def test_abrir_congela_evidencia_del_pedido(self):
        por_pk = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(self.pedido.pk), tipo="contenido",
            archivo=SimpleUploadedFile("contenido.jpg", b"foto-contenido"),
        )
        por_folio = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=self.pedido.folio, tipo="caja_cerrada",
            archivo=SimpleUploadedFile("caja.jpg", b"foto-caja"),
        )
        ajena = EvidenciaFoto.objects.create(
            entidad="asn", entidad_id="ASN-0001", tipo="llegada",
            archivo=SimpleUploadedFile("llegada.jpg", b"foto-llegada"),
        )
        abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido)
        por_pk.refresh_from_db()
        por_folio.refresh_from_db()
        ajena.refresh_from_db()
        self.assertTrue(por_pk.congelada)
        self.assertTrue(por_folio.congelada)
        self.assertFalse(ajena.congelada)

    def test_cerrar_desmarca_solo_sin_abiertas(self):
        primera = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido)
        segunda = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_COMPRADOR, pedido=self.pedido)

        resolver(primera, "Reposición enviada.", "mesa1")
        cerrar(primera, "mesa1")
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.incidencia_activa)  # la segunda sigue abierta

        resolver(segunda, "El paquete se entregó; comprador confirmó.", "mesa1")
        cerrar(segunda, "mesa1")
        self.pedido.refresh_from_db()
        self.assertFalse(self.pedido.incidencia_activa)

    def test_resolver_estampa_resolucion_y_cuenta_como_respuesta(self):
        incidencia = abrir_incidencia(
            self.cliente, Incidencia.TIPO_FAL, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido
        )
        resolver(incidencia, "Se repuso la pieza faltante con guía express.", "mesa1")
        incidencia.refresh_from_db()
        self.assertEqual(incidencia.estado, Incidencia.RESUELTA)
        self.assertIsNotNone(incidencia.ts_resolucion)
        self.assertIsNotNone(incidencia.ts_primera_respuesta)
        self.assertTrue(incidencia.mensajes.filter(rol_autor=MensajeIncidencia.ROL_MESA).exists())

    def test_cerrar_estampa_cierre(self):
        incidencia = abrir_incidencia(
            self.cliente, Incidencia.TIPO_DIR, Incidencia.ORIGEN_COMPRADOR, pedido=self.pedido
        )
        resolver(incidencia, "Dirección corregida con el comprador.", "mesa1")
        cerrar(incidencia, "mesa1")
        incidencia.refresh_from_db()
        self.assertEqual(incidencia.estado, Incidencia.CERRADA)
        self.assertIsNotNone(incidencia.ts_cierre)


class PushEnOnCommitTests(TestCase):
    """El push a la Mesa sale en transaction.on_commit — abrir_incidencia corre
    dentro de la transacción del caller (p.ej. ingesta Shopify con locks de
    Saldo tomados) y un push service colgado apilaría workers."""

    def test_push_a_mesa_sale_tras_el_commit_jamas_dentro(self):
        from unittest.mock import patch

        cliente = crear_cliente()
        with patch("apps.mensajeria.push.enviar_push_a_rol") as enviar:
            with self.captureOnCommitCallbacks(execute=True):
                abrir_incidencia(cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL)
                enviar.assert_not_called()  # dentro de la transacción: nada
        enviar.assert_called_once()  # tras el commit: sale el aviso
