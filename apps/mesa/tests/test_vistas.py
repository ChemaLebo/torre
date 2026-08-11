"""Tests de las vistas de Mesa de Control: acceso por rol y gestión de incidencias.

La Mesa ve todos los clientes (no filtra por tenant), pero SOLO entra quien
tiene rol mesa (o superuser). Toda acción de gestión pasa por los servicios
de dominio: aquí se prueba que los POST del detalle disparan lo correcto.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario


def crear_usuario(username, rol, cliente=None, pin=""):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente, pin=pin)
    return user


def crear_pedido(cliente, **extra):
    from apps.pedidos.models import Pedido

    defaults = {
        "cliente": cliente,
        "origen": "manual",
        "comprador_nombre": "Comprador de Prueba",
        "direccion": {},
        "cp": "28017",
        "es_local": True,
        "valor_declarado": Decimal("500.00"),
        "estado": Pedido.PENDIENTE,
        "corte_vigente_al_ingreso": time(14, 0),
    }
    defaults.update(extra)
    return Pedido.objects.create(**defaults)


class BaseMesaTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.usuario_mesa = crear_usuario("mesa1", "mesa", pin="3333")
        cls.usuario_portal = crear_usuario("karina", "portal", cliente=cls.colima)


class AccesoPorRolTests(BaseMesaTest):
    def test_mesa_entra_al_dashboard(self):
        self.client.force_login(self.usuario_mesa)
        respuesta = self.client.get(reverse("mesa:dashboard"))
        self.assertEqual(respuesta.status_code, 200)

    def test_portal_no_entra_a_mesa(self):
        self.client.force_login(self.usuario_portal)
        for nombre in ("mesa:dashboard", "mesa:incidencias", "mesa:pedidos", "mesa:sync", "mesa:clientes"):
            respuesta = self.client.get(reverse(nombre))
            self.assertEqual(respuesta.status_code, 403, f"{nombre} dejó pasar a un rol portal")

    def test_anonimo_va_a_login(self):
        respuesta = self.client.get(reverse("mesa:dashboard"))
        self.assertEqual(respuesta.status_code, 302)

    def test_todas_las_vistas_cargan_para_mesa(self):
        self.client.force_login(self.usuario_mesa)
        crear_pedido(self.colima)
        for nombre in ("mesa:dashboard", "mesa:incidencias", "mesa:pedidos", "mesa:sync", "mesa:clientes"):
            respuesta = self.client.get(reverse(nombre))
            self.assertEqual(respuesta.status_code, 200, f"{nombre} no cargó")
        detalle = self.client.get(reverse("mesa:cliente_detalle", args=[self.colima.pk]))
        self.assertEqual(detalle.status_code, 200)
        self.assertContains(detalle, "Cervecería Colima")


class GestionIncidenciaTests(BaseMesaTest):
    def setUp(self):
        from apps.incidencias.services import abrir_incidencia

        self.incidencia = abrir_incidencia(
            self.colima, "DAN", "cliente", texto="Se reporta una caja dañada."
        )
        self.url = reverse("mesa:incidencia_detalle", args=[self.incidencia.pk])
        self.client.force_login(self.usuario_mesa)

    def test_detalle_carga_con_folio_y_reloj(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.incidencia.folio)

    def test_responder_estampa_primera_respuesta(self):
        self.assertIsNone(self.incidencia.ts_primera_respuesta)
        respuesta = self.client.post(self.url, {"accion": "responder", "texto": "Ya lo estamos viendo."})
        self.assertRedirects(respuesta, self.url)
        self.incidencia.refresh_from_db()
        self.assertIsNotNone(self.incidencia.ts_primera_respuesta)
        mensaje = self.incidencia.mensajes.latest("ts")
        self.assertEqual(mensaje.rol_autor, "mesa")
        self.assertFalse(mensaje.interno)

    def test_nota_interna_no_para_el_reloj(self):
        self.client.post(self.url, {"accion": "nota_interna", "texto": "Contexto solo para la Mesa."})
        self.incidencia.refresh_from_db()
        self.assertIsNone(self.incidencia.ts_primera_respuesta)
        self.assertTrue(self.incidencia.mensajes.filter(interno=True).exists())

    def test_tomar_pone_en_curso_con_dueno(self):
        self.client.post(self.url, {"accion": "tomar"})
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.estado, "EN_CURSO")
        self.assertEqual(self.incidencia.dueno, "mesa1")

    def test_resolver_y_cerrar(self):
        self.client.post(self.url, {"accion": "resolver", "texto": "Reposición enviada."})
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.estado, "RESUELTA")
        self.client.post(self.url, {"accion": "cerrar"})
        self.incidencia.refresh_from_db()
        self.assertEqual(self.incidencia.estado, "CERRADA")

    def test_flujo_compensacion(self):
        from apps.incidencias.models import Compensacion

        self.client.post(self.url, {"accion": "compensacion_crear", "tipo": "reposicion", "monto": "450.00"})
        comp = Compensacion.objects.get(incidencia=self.incidencia)
        self.assertEqual(comp.estado, Compensacion.COTIZADA)
        self.assertEqual(comp.monto, Decimal("450.00"))

        self.client.post(self.url, {
            "accion": "compensacion_avanzar", "compensacion_id": comp.pk, "nuevo_estado": "APROBADA",
        })
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.assertEqual(comp.aprobo, "mesa1")

        # PAGADA sin referencia de pago: el servicio truena y el estado no cambia.
        self.client.post(self.url, {
            "accion": "compensacion_avanzar", "compensacion_id": comp.pk, "nuevo_estado": "PAGADA",
        })
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.APROBADA)

        self.client.post(self.url, {
            "accion": "compensacion_avanzar", "compensacion_id": comp.pk,
            "nuevo_estado": "PAGADA", "referencia_pago": "SPEI-778899",
        })
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.PAGADA)
        self.assertEqual(comp.referencia_pago, "SPEI-778899")
        self.assertIsNotNone(comp.fecha_pago)

    def test_flujo_reclamacion_carrier(self):
        from apps.incidencias.models import ReclamacionCarrier

        self.client.post(self.url, {
            "accion": "reclamacion_crear", "carrier": "paquetexpress", "monto_reclamado": "780.00",
        })
        rec = ReclamacionCarrier.objects.get(incidencia=self.incidencia)
        self.assertEqual(rec.estado, ReclamacionCarrier.PREPARADA)

        self.client.post(self.url, {
            "accion": "reclamacion_avanzar", "reclamacion_id": rec.pk, "nuevo_estado": "PRESENTADA",
        })
        rec.refresh_from_db()
        self.assertEqual(rec.estado, ReclamacionCarrier.PRESENTADA)
        self.assertIsNotNone(rec.fecha_presentacion)

        self.client.post(self.url, {
            "accion": "reclamacion_avanzar", "reclamacion_id": rec.pk,
            "nuevo_estado": "ACEPTADA", "monto_recuperado": "156.00",
        })
        rec.refresh_from_db()
        self.assertEqual(rec.estado, ReclamacionCarrier.ACEPTADA)
        self.assertEqual(rec.monto_recuperado, Decimal("156.00"))

    def test_portal_no_gestiona_incidencias_de_mesa(self):
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.post(self.url, {"accion": "responder", "texto": "intruso"})
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(self.incidencia.mensajes.filter(texto="intruso").count(), 0)


class PedidosGlobalesTests(BaseMesaTest):
    def test_filtra_por_cliente_estado_y_busqueda(self):
        from apps.pedidos.models import Pedido

        otro = Cliente.objects.create(nombre="Mezcal Nocturno", slug="mezcal-nocturno")
        p1 = crear_pedido(self.colima, comprador_nombre="Lupita Ramírez")
        p2 = crear_pedido(otro, comprador_nombre="Tomás Bravo", estado=Pedido.EN_PICKING)

        self.client.force_login(self.usuario_mesa)
        url = reverse("mesa:pedidos")

        todos = self.client.get(url)
        self.assertContains(todos, p1.folio)
        self.assertContains(todos, p2.folio)

        solo_colima = self.client.get(url, {"cliente": self.colima.pk})
        self.assertContains(solo_colima, p1.folio)
        self.assertNotContains(solo_colima, p2.folio)

        por_estado = self.client.get(url, {"estado": Pedido.EN_PICKING})
        self.assertNotContains(por_estado, p1.folio)
        self.assertContains(por_estado, p2.folio)

        buscado = self.client.get(url, {"q": "Lupita"})
        self.assertContains(buscado, p1.folio)
        self.assertNotContains(buscado, p2.folio)


class SyncTests(BaseMesaTest):
    def test_el_boton_de_push_manual_ya_no_existe(self):
        """La cola la drena el cron (push_inventario cada 5 min): el botón
        corría el drenado síncrono en el request y amarraba un worker."""
        from apps.catalogo.models import SKU
        from apps.integraciones.models import PushInventarioPendiente, Tienda
        from apps.integraciones.services import encolar_push_inventario

        Tienda.objects.create(cliente=self.colima, dominio="colima-mx.myshopify.com", token="")
        sku = SKU.objects.create(cliente=self.colima, codigo="COLIMITA-SIX", descripcion="Colimita six")
        encolar_push_inventario(sku)

        self.client.force_login(self.usuario_mesa)
        pantalla = self.client.get(reverse("mesa:sync"))
        self.assertNotContains(pantalla, "correr_push")
        # El POST viejo ya no drena nada.
        self.client.post(reverse("mesa:sync"), {"accion": "correr_push"})
        self.assertEqual(PushInventarioPendiente.objects.count(), 1)
