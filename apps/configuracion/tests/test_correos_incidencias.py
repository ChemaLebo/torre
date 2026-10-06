"""Correos de incidencias (Chema 2026-10-06): lista fija de Torre (sin cliente,
solo admin) más lista por cliente (ficha de Mesa); cada incidencia que se abre
manda UN correo a la unión de ambas. En tests el correo va a mail.outbox."""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.configuracion.models import CorreoIncidencias
from apps.configuracion.services import agregar_correo_incidencias, correos_incidencias, quitar_correo_incidencias
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia


class ListaDeCorreosTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        self.otro = Cliente.objects.create(nombre="Otro", slug="otro")
        CorreoIncidencias.objects.create(correo="ops@torre.mx", nombre="Torre")
        CorreoIncidencias.objects.create(correo="karina@colima.mx", cliente=self.colima)
        CorreoIncidencias.objects.create(correo="otro@otro.mx", cliente=self.otro)
        CorreoIncidencias.objects.create(correo="apagado@torre.mx", activo=False)

    def test_fija_mas_la_del_cliente_sin_repetidos(self):
        self.assertEqual(correos_incidencias(self.colima), ["ops@torre.mx", "karina@colima.mx"])
        self.assertEqual(correos_incidencias(self.otro), ["ops@torre.mx", "otro@otro.mx"])
        self.assertEqual(correos_incidencias(None), ["ops@torre.mx"])
        CorreoIncidencias.objects.create(correo="OPS@torre.mx", cliente=self.colima)  # repetido con otra caja
        self.assertEqual(correos_incidencias(self.colima), ["ops@torre.mx", "karina@colima.mx"])

    def test_alta_valida_y_no_repite_y_baja_borra(self):
        with self.assertRaisesMessage(ValueError, "correo válido"):
            agregar_correo_incidencias(self.colima, "no-es-correo")
        with self.assertRaisesMessage(ValueError, "ya está en la lista"):
            agregar_correo_incidencias(self.colima, "Karina@colima.mx")
        fila = agregar_correo_incidencias(self.colima, " ventas@colima.mx ", "Ventas")
        self.assertEqual((fila.correo, fila.nombre, fila.cliente), ("ventas@colima.mx", "Ventas", self.colima))
        self.assertTrue(EventoAuditoria.objects.filter(entidad="cliente", entidad_id="colima", accion="correo_incidencias_alta").exists())
        quitar_correo_incidencias(fila)
        self.assertFalse(CorreoIncidencias.objects.filter(pk=fila.pk).exists())
        self.assertTrue(EventoAuditoria.objects.filter(accion="correo_incidencias_baja").exists())


class CorreoAlAbrirIncidenciaTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        CorreoIncidencias.objects.create(correo="ops@torre.mx")
        CorreoIncidencias.objects.create(correo="karina@colima.mx", cliente=self.colima)

    def test_abrir_manda_un_correo_a_la_union_con_el_detalle(self):
        with self.captureOnCommitCallbacks(execute=True):
            inc = abrir_incidencia(self.colima, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL, texto="Caja rota en la mesa.")
        self.assertEqual(len(mail.outbox), 1)
        correo = mail.outbox[0]
        self.assertEqual(sorted(correo.to), ["karina@colima.mx", "ops@torre.mx"])
        self.assertEqual(correo.subject, f"[Torre] {inc.folio} · DAN · Se abrió la incidencia {inc.folio}")
        self.assertIn("Caja rota en la mesa.", correo.body)
        self.assertIn(f"/mesa/incidencias/{inc.pk}/", correo.body)
        html = correo.alternatives[0][0]
        self.assertIn("Daño / rotura", html)
        self.assertIn("Ver en Mesa", html)
        evento = EventoAuditoria.objects.get(entidad="incidencia", entidad_id=inc.folio, accion="correo_enviado")
        self.assertEqual(evento.motivo, f"abierta:{inc.folio}")

    def test_interna_tambien_avisa_y_sin_destinatarios_queda_el_evento(self):
        with self.captureOnCommitCallbacks(execute=True):
            inc = abrir_incidencia(self.colima, Incidencia.TIPO_PAQ, Incidencia.ORIGEN_AUTO, texto="Nadie cotiza.", interna=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("interna (bodega)", mail.outbox[0].body)
        CorreoIncidencias.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            inc2 = abrir_incidencia(self.colima, Incidencia.TIPO_DIR, Incidencia.ORIGEN_MANUAL, texto="Sin número.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(EventoAuditoria.objects.filter(entidad_id=inc2.folio, accion="correo_sin_destinatarios").exists())
        self.assertNotEqual(inc.pk, inc2.pk)

    def test_un_reporte_agrupado_no_vuelve_a_mandar(self):
        from apps.incidencias.tests.utils import crear_pedido

        pedido = crear_pedido(self.colima)
        with self.captureOnCommitCallbacks(execute=True):
            abrir_incidencia(self.colima, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL, pedido=pedido, texto="Primera.")
            abrir_incidencia(self.colima, Incidencia.TIPO_DAN, Incidencia.ORIGEN_MANUAL, pedido=pedido, texto="Segunda, mismo caso.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"· {pedido.folio} ·", mail.outbox[0].subject)
        self.assertIn(pedido.folio, mail.outbox[0].body)


class FichaDeClienteTests(TestCase):
    def setUp(self):
        self.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        mesa = get_user_model().objects.create_user("mesa-correos", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.url = reverse("mesa:cliente_detalle", args=[self.colima.pk])
        CorreoIncidencias.objects.create(correo="ops@torre.mx")

    def test_agregar_y_quitar_desde_mesa(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Correos de incidencias")
        self.assertContains(respuesta, "1 fijo de Torre")
        self.assertNotContains(respuesta, "ops@torre.mx")  # la lista fija no se edita aquí
        respuesta = self.client.post(self.url, {"accion": "correo_incidencias_alta", "correo": "karina@colima.mx", "nombre": "Karina"}, follow=True)
        self.assertContains(respuesta, "karina@colima.mx recibirá las incidencias")
        fila = CorreoIncidencias.objects.get(correo="karina@colima.mx", cliente=self.colima)
        self.assertContains(respuesta, "Karina")
        respuesta = self.client.post(self.url, {"accion": "correo_incidencias_alta", "correo": "mal"}, follow=True)
        self.assertContains(respuesta, "correo válido")
        respuesta = self.client.post(self.url, {"accion": "correo_incidencias_baja", "correo_id": fila.pk}, follow=True)
        self.assertContains(respuesta, "ya no recibe")
        self.assertFalse(CorreoIncidencias.objects.filter(pk=fila.pk).exists())

    def test_admin_registra_el_modelo(self):
        from django.contrib import admin

        self.assertIn(CorreoIncidencias, admin.site._registry)
