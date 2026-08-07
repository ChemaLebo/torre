"""Tests del command seed_demo: contenido del demo completo e idempotencia.

El seed es el contrato de arranque de la demo: usuarios con password
documentado, dos tenants (aislamiento), pedidos en todos los estados,
incidencias con timeline/compensación/reclamación, kardex vivo y doble firma.
"""
import shutil
import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.mesa.management.commands.seed_demo import USUARIOS

MEDIA_DEMO = tempfile.mkdtemp(prefix="torre-media-seed-")


def correr_seed():
    call_command("seed_demo", stdout=StringIO())


@override_settings(MEDIA_ROOT=MEDIA_DEMO)
class SeedDemoTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_DEMO, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        correr_seed()

    # ── Usuarios y tenants ──

    def test_usuarios_con_password_documentado(self):
        User = get_user_model()
        for username, password, rol, cliente_slug, pin, _nombre, es_super in USUARIOS:
            user = User.objects.get(username=username)
            self.assertTrue(user.check_password(password), f"password de {username} no coincide")
            self.assertEqual(user.is_superuser, es_super)
            if rol is not None:
                self.assertEqual(user.perfil.rol, rol)
                self.assertEqual(user.perfil.pin, pin)
                if cliente_slug:
                    self.assertEqual(user.perfil.cliente.slug, cliente_slug)

    def test_multitienda_y_aislamiento_multitenant(self):
        from apps.catalogo.models import SKU
        from apps.core.models import Cliente
        from apps.integraciones.models import Tienda
        from apps.pedidos.models import Pedido

        colima = Cliente.objects.get(slug="colima")
        nocturno = Cliente.objects.get(slug="mezcal-nocturno")
        self.assertEqual(Tienda.objects.filter(cliente=colima).count(), 2)
        self.assertEqual(Tienda.objects.filter(cliente=nocturno).count(), 1)
        # Los datos del segundo tenant existen y NO se mezclan con Colima.
        self.assertTrue(SKU.objects.filter(cliente=nocturno).exists())
        self.assertFalse(SKU.objects.filter(cliente=nocturno, codigo__startswith="COLIMITA").exists())
        self.assertTrue(Pedido.objects.filter(cliente=nocturno).exists())
        for pedido in Pedido.objects.filter(cliente=nocturno):
            self.assertEqual(pedido.tienda.cliente, nocturno)
            for linea in pedido.lineas.all():
                self.assertEqual(linea.sku.cliente, nocturno)

    # ── Pedidos ──

    def test_pedidos_en_todos_los_estados(self):
        from apps.pedidos.models import Pedido

        self.assertGreaterEqual(Pedido.objects.count(), 14)
        estados = set(Pedido.objects.values_list("estado", flat=True))
        esperados = {
            Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA,
            Pedido.RECOLECTADO, Pedido.EN_TRANSITO, Pedido.ENTREGADO, Pedido.ENTREGA_PRESUNTA,
            Pedido.PARCIALMENTE_DESPACHADO, Pedido.CANCELACION_PENDIENTE,
            Pedido.CANCELADO, Pedido.RETORNADO,
        }
        self.assertTrue(esperados.issubset(estados), f"faltan estados: {esperados - estados}")

    def test_timestamps_escalonados_y_coherentes(self):
        from apps.pedidos.models import Pedido

        for pedido in Pedido.objects.filter(estado=Pedido.ENTREGADO, cliente__slug="colima"):
            self.assertIsNotNone(pedido.ts_recolectado)
            self.assertIsNotNone(pedido.ts_entregado)
            self.assertLess(pedido.creado, pedido.ts_recolectado)
            self.assertLessEqual(pedido.ts_recolectado, pedido.ts_entregado)

    def test_pedidos_empacados_tienen_evidencia_y_peso(self):
        from apps.core.models import EvidenciaFoto
        from apps.pedidos.models import Pedido

        empacados = Pedido.objects.exclude(ts_empacado=None)
        self.assertTrue(empacados.exists())
        for pedido in empacados:
            fotos = EvidenciaFoto.objects.filter(entidad="pedido", entidad_id=str(pedido.pk))
            self.assertGreaterEqual(fotos.count(), 2, f"{pedido.folio} sin las 2 fotos de empaque")
            self.assertIsNotNone(pedido.peso_real_gr)

    # ── Incidencias ──

    def test_incidencias_demo(self):
        from apps.incidencias.models import Compensacion, Incidencia, ReclamacionCarrier

        dan = Incidencia.objects.get(tipo="DAN")
        self.assertEqual(dan.estado, Incidencia.EN_CURSO)
        self.assertEqual(dan.dueno, "mesa1")
        self.assertGreaterEqual(dan.mensajes.count(), 4)
        self.assertTrue(dan.mensajes.filter(interno=True).exists())
        self.assertIsNotNone(dan.ts_primera_respuesta)
        self.assertTrue(dan.pedido.incidencia_activa)

        ret = Incidencia.objects.get(tipo="RET")
        self.assertEqual(ret.estado, Incidencia.RESUELTA)
        self.assertIsNotNone(ret.ts_resolucion)

        fal = Incidencia.objects.get(tipo="FAL")
        self.assertEqual(fal.estado, Incidencia.CERRADA)
        self.assertIsNotNone(fal.ts_cierre)
        # Al cerrar la única incidencia del pedido, el flag se libera.
        self.assertFalse(fal.pedido.incidencia_activa)

        comp = Compensacion.objects.get(incidencia=dan)
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.assertEqual(comp.aprobo, "karina")
        rec = ReclamacionCarrier.objects.get(incidencia=dan)
        self.assertEqual(rec.estado, ReclamacionCarrier.PRESENTADA)
        self.assertEqual(rec.carrier, "paquetexpress")
        self.assertIsNotNone(rec.fecha_presentacion)

    # ── Inventario: kardex, ASN, conteos, doble firma ──

    def test_kardex_vivo_y_auditoria(self):
        from apps.core.models import EventoAuditoria
        from apps.inventario.models import Movimiento

        tipos = set(Movimiento.objects.values_list("tipo", flat=True))
        esperados = {"recepcion", "putaway", "reserva", "pick", "salida", "retorno", "conteo", "ajuste"}
        self.assertTrue(esperados.issubset(tipos), f"kardex sin: {esperados - tipos}")
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="incidencia").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="sku").exists())

    def test_asn_cerrada_y_asn_anunciada(self):
        from apps.inventario.models import OrdenEntrada

        cerradas = OrdenEntrada.objects.filter(cliente__slug="colima", estado=OrdenEntrada.CERRADA)
        self.assertTrue(cerradas.exists())
        self.assertIsNotNone(cerradas.first().ts_vendible)
        anunciada = OrdenEntrada.objects.filter(cliente__slug="colima", estado=OrdenEntrada.ANUNCIADA)
        self.assertTrue(anunciada.exists())
        self.assertTrue(anunciada.first().lineas.exists())

    def test_ajuste_con_doble_firma(self):
        from apps.inventario.models import Ajuste

        ajuste = Ajuste.objects.get(motivo=Ajuste.MOTIVO_CONTEO)
        self.assertEqual(ajuste.delta, -2)
        self.assertNotEqual(ajuste.autorizo_1, ajuste.autorizo_2)
        self.assertIsNotNone(ajuste.conteo)
        self.assertEqual(ajuste.conteo.diferencia, -2)

    def test_conteos_sin_descuadre_mayor(self):
        from apps.incidencias.models import Incidencia
        from apps.inventario.models import Conteo

        self.assertGreaterEqual(Conteo.objects.count(), 6)
        # Las diferencias sembradas quedan bajo umbral: no se abren DES falsas.
        self.assertFalse(Incidencia.objects.filter(tipo="DES").exists())

    # ── Plantillas, reglas y sync ──

    def test_plantillas_y_reglas_de_envio(self):
        from apps.envios.models import ReglaEnvio
        from apps.mensajeria.models import PlantillaMensaje

        for clave in ("A", "B", "E"):
            self.assertTrue(PlantillaMensaje.objects.filter(clave=clave, cliente=None).exists())
            self.assertTrue(
                PlantillaMensaje.objects.filter(
                    clave=clave, cliente__slug="colima", aprobada_por_cliente=True
                ).exists()
            )
        reglas = ReglaEnvio.objects.filter(cliente__slug="colima").order_by("prioridad")
        self.assertEqual(
            [(r.carrier, r.servicio) for r in reglas],
            [("local", "entrega_local"), ("paquetexpress", "ground")],
        )

    def test_sync_poblado(self):
        from apps.integraciones.models import PushInventarioPendiente, SyncLog, WebhookEvento

        self.assertTrue(SyncLog.objects.filter(direccion="push", resultado="ok").exists())
        self.assertTrue(SyncLog.objects.filter(direccion="ingesta").exists())
        self.assertTrue(WebhookEvento.objects.filter(procesado=True).exists())
        # El ajuste corre después del push: deja la cola con actividad visible.
        self.assertTrue(PushInventarioPendiente.objects.exists())

    # ── Idempotencia ──

    def test_idempotente(self):
        from apps.catalogo.models import SKU, Lote, Ubicacion
        from apps.core.models import Cliente
        from apps.envios.models import Guia, ReglaEnvio
        from apps.incidencias.models import Compensacion, Incidencia, MensajeIncidencia, ReclamacionCarrier
        from apps.integraciones.models import Tienda, WebhookEvento
        from apps.inventario.models import Ajuste, Conteo, OrdenEntrada
        from apps.mensajeria.models import PlantillaMensaje
        from apps.pedidos.models import LineaPedido, Pedido

        modelos = [
            Cliente, Tienda, get_user_model(), SKU, Ubicacion, Lote,
            Pedido, LineaPedido, Guia, Incidencia, MensajeIncidencia,
            Compensacion, ReclamacionCarrier, Conteo, Ajuste, OrdenEntrada,
            PlantillaMensaje, ReglaEnvio, WebhookEvento,
        ]
        antes = {m.__name__: m.objects.count() for m in modelos}
        correr_seed()
        despues = {m.__name__: m.objects.count() for m in modelos}
        self.assertEqual(antes, despues, "el seed duplicó entidades al correr dos veces")
