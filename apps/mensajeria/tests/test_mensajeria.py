"""Tests de mensajería: idempotencia de salida, render de plantillas y regla B.

Los modelos de otras apps (pedidos, integraciones, envios) se resuelven vía
apps.get_model y los objetos se crean filtrando a campos existentes, para no
acoplarse a defaults ajenos más allá del esquema del contrato.
"""
from datetime import time
from decimal import Decimal
from types import SimpleNamespace

from django.apps import apps as django_apps
from django.test import TestCase, override_settings

from apps.core.models import Cliente
from apps.mensajeria import services
from apps.mensajeria.models import CUERPOS_DEFAULT, NotificacionEnviada, PlantillaMensaje


def _crear(modelo, **datos):
    """Crea una instancia pasando solo los campos que el modelo realmente tiene."""
    campos = {f.name for f in modelo._meta.get_fields()}
    return modelo.objects.create(**{k: v for k, v in datos.items() if k in campos})


def crear_tienda(cliente, **extra):
    Tienda = django_apps.get_model("integraciones", "Tienda")
    datos = dict(
        cliente=cliente, plataforma="shopify", dominio="colima-mx.myshopify.com",
        token="", location_id="", activo=True,
    )
    datos.update(extra)
    return _crear(Tienda, **datos)


def crear_pedido(cliente, tienda, **extra):
    Pedido = django_apps.get_model("pedidos", "Pedido")
    datos = dict(
        tienda=tienda, cliente=cliente, shopify_order_id="1001", folio="PED-00001",
        origen="webhook", comprador_nombre="Ana Martínez", comprador_tel="+523121234567",
        comprador_email="ana@example.com", direccion={}, cp="28017", es_local=True,
        valor_declarado=Decimal("500.00"), nota_regalo="", estado="PENDIENTE",
        corte_vigente_al_ingreso=time(14, 0), peso_esperado_gr=1000,
    )
    datos.update(extra)
    return _crear(Pedido, **datos)


def crear_guia(pedido, numero="MOCK-1234", **extra):
    Guia = django_apps.get_model("envios", "Guia")
    datos = dict(
        pedido=pedido, carrier="paquetexpress", servicio="terrestre", numero=numero,
        costo_cotizado=Decimal("0.00"), costo_preferencial=Decimal("0.00"),
        etiqueta_url="", estado="GUIA_CREADA", ultimo_evento="", raw={},
    )
    datos.update(extra)
    return _crear(Guia, **datos)


@override_settings(WHATSAPP_TOKEN="", WHATSAPP_PHONE_ID="")
class RenderPlantillaTests(TestCase):
    def test_render_sustituye_variables(self):
        plantilla = PlantillaMensaje(
            clave="B", nombre="Prueba",
            cuerpo="Hola {nombre}, tu pedido {folio} de {marca} lleva la guía {guia}: {link_rastreo}",
        )
        cuerpo = plantilla.render({
            "nombre": "Ana", "folio": "PED-00007", "marca": "Cervecería Colima",
            "guia": "PQX-555", "link_rastreo": "https://rastreo.example/PQX-555",
        })
        self.assertIn("Ana", cuerpo)
        self.assertIn("PED-00007", cuerpo)
        self.assertIn("Cervecería Colima", cuerpo)
        self.assertIn("PQX-555", cuerpo)
        self.assertIn("https://rastreo.example/PQX-555", cuerpo)
        self.assertNotIn("{", cuerpo)

    def test_render_variable_faltante_no_explota(self):
        plantilla = PlantillaMensaje(clave="A", nombre="Prueba", cuerpo="Hola {nombre}, guía {guia}.")
        cuerpo = plantilla.render({"nombre": "Ana"})
        self.assertEqual(cuerpo, "Hola Ana, guía .")

    def test_cuerpos_default_tienen_las_variables_clave(self):
        # A: honesta, con corte y fecha real
        for variable in ("{nombre}", "{folio}", "{marca}", "{corte}", "{fecha}"):
            self.assertIn(variable, CUERPOS_DEFAULT["A"])
        # B: guía y link de rastreo
        for variable in ("{guia}", "{link_rastreo}"):
            self.assertIn(variable, CUERPOS_DEFAULT["B"])
        # E: nueva fecha, y JAMÁS culpa a la paquetería
        self.assertIn("{fecha}", CUERPOS_DEFAULT["E"])
        for prohibida in ("paqueter", "carrier", "paquetexpress", "mensajer"):
            self.assertNotIn(prohibida, CUERPOS_DEFAULT["E"].lower())

    def test_obtener_plantilla_prefiere_la_del_cliente(self):
        cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        PlantillaMensaje.objects.create(clave="A", cliente=None, nombre="Default", cuerpo="default {folio}")
        propia = PlantillaMensaje.objects.create(
            clave="A", cliente=cliente, nombre="Colima", cuerpo="colima {folio}", aprobada_por_cliente=True,
        )
        self.assertEqual(services.obtener_plantilla("A", cliente).pk, propia.pk)
        self.assertEqual(services.obtener_plantilla("A", None).nombre, "Default")

    def test_obtener_plantilla_sin_filas_regresa_cuerpo_canonico(self):
        plantilla = services.obtener_plantilla("E", None)
        self.assertIsNone(plantilla.pk)
        self.assertEqual(plantilla.cuerpo, CUERPOS_DEFAULT["E"])


@override_settings(WHATSAPP_TOKEN="", WHATSAPP_PHONE_ID="")
class EnvioIdempotenteTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima",
            contacto_nombre="Karina", contacto_whatsapp="+523120000000",
        )
        self.tienda = crear_tienda(self.cliente)

    def test_confirmacion_dos_veces_un_solo_registro(self):
        pedido = crear_pedido(self.cliente, self.tienda)
        primera = services.enviar_confirmacion(pedido)
        segunda = services.enviar_confirmacion(pedido)
        self.assertIsNotNone(primera)
        self.assertEqual(primera.pk, segunda.pk)  # no-op silencioso: regresa la existente
        self.assertEqual(NotificacionEnviada.objects.count(), 1)
        self.assertEqual(primera.canal, "consola")
        self.assertIn("PED-00001", primera.cuerpo)

    def test_confirmacion_sin_telefono_no_crea_registro(self):
        pedido = crear_pedido(self.cliente, self.tienda, comprador_tel="")
        self.assertIsNone(services.enviar_confirmacion(pedido))
        self.assertEqual(NotificacionEnviada.objects.count(), 0)

    def test_notificar_cliente_incidencia_idempotente(self):
        incidencia = SimpleNamespace(
            folio="INC-2026-0001", tipo="DAN", cliente=self.cliente, pedido=None,
        )
        primera = services.notificar_cliente_incidencia(incidencia)
        segunda = services.notificar_cliente_incidencia(incidencia)
        self.assertEqual(primera.pk, segunda.pk)
        self.assertEqual(NotificacionEnviada.objects.count(), 1)
        self.assertIn("INC-2026-0001", primera.cuerpo)
        self.assertEqual(primera.destinatario, "+523120000000")  # el cliente SIEMPRE se entera

    def test_digest_diario_idempotente(self):
        primeras = services.digest_diario()
        segundas = services.digest_diario()
        self.assertEqual(len(primeras), 1)
        self.assertEqual([n.pk for n in primeras], [n.pk for n in segundas])
        digest = primeras[0]
        self.assertIn("Cervecería Colima", digest.cuerpo)
        self.assertEqual(digest.plantilla_clave, "DIGEST")


@override_settings(WHATSAPP_TOKEN="", WHATSAPP_PHONE_ID="")
class PlantillaBTests(TestCase):
    """La regla dura: B solo al RECOLECTADO, y siempre con la guía."""

    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima", contacto_whatsapp="+523120000000",
        )
        self.tienda = crear_tienda(self.cliente)

    def test_b_contiene_la_guia_y_el_link(self):
        pedido = crear_pedido(self.cliente, self.tienda, estado="RECOLECTADO")
        crear_guia(pedido, numero="MOCK-1234")
        notificacion = services.enviar_en_camino(pedido)
        self.assertIsNotNone(notificacion)
        self.assertIn("MOCK-1234", notificacion.cuerpo)   # la guía sí se menciona
        self.assertIn("/r/", notificacion.cuerpo)          # el link es la página brandeada

    def test_b_jamas_antes_de_recolectado(self):
        pedido = crear_pedido(self.cliente, self.tienda, estado="EMPACADO")
        with self.assertRaises(ValueError):
            services.enviar_en_camino(pedido)
        self.assertEqual(NotificacionEnviada.objects.count(), 0)

    def test_retraso_con_fecha_nueva_si_sale_y_repetido_no(self):
        from datetime import date
        pedido = crear_pedido(self.cliente, self.tienda, estado="EN_TRANSITO")
        primera = services.enviar_retraso(pedido, date(2026, 7, 24))
        repetida = services.enviar_retraso(pedido, date(2026, 7, 24))
        distinta = services.enviar_retraso(pedido, date(2026, 7, 27))
        self.assertEqual(primera.pk, repetida.pk)
        self.assertNotEqual(primera.pk, distinta.pk)
        self.assertIn("viernes 24 de julio", primera.cuerpo)
        self.assertEqual(NotificacionEnviada.objects.count(), 2)


@override_settings(WHATSAPP_TOKEN="", WHATSAPP_PHONE_ID="")
class RecepcionCerradaTests(TestCase):
    """Confirmación honesta del ASN cerrado: idempotente y con cifras reales."""

    def setUp(self):
        self.cliente = Cliente.objects.create(
            nombre="Cervecería Colima", slug="colima",
            contacto_nombre="Karina", contacto_whatsapp="+523120000000",
        )
        SKU = django_apps.get_model("catalogo", "SKU")
        LineaASN = django_apps.get_model("inventario", "LineaASN")
        OrdenEntrada = django_apps.get_model("inventario", "OrdenEntrada")
        self.sku = SKU.objects.create(
            cliente=self.cliente, codigo="COLIMITA-SIX", descripcion="Colimita six pack",
        )
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)
        # Anunciadas 125: llegaron 120 buenas + 2 dañadas → faltaron 3.
        LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=125,
            cantidad_recibida=120, cantidad_danada=2,
        )

    def test_idempotente_y_cuerpo_con_cifras(self):
        primera = services.enviar_recepcion_cerrada(self.orden)
        segunda = services.enviar_recepcion_cerrada(self.orden)
        self.assertIsNotNone(primera)
        self.assertEqual(primera.pk, segunda.pk)  # no-op silencioso
        self.assertEqual(NotificacionEnviada.objects.count(), 1)
        self.assertEqual(primera.destinatario, "+523120000000")
        self.assertEqual(primera.plantilla_clave, "REC")
        self.assertEqual(primera.referencia, self.orden.folio)
        self.assertIn(self.orden.folio, primera.cuerpo)
        self.assertIn("120 piezas en buen estado", primera.cuerpo)
        self.assertIn("2 de COLIMITA-SIX dañadas", primera.cuerpo)
        self.assertIn("faltaron 3 de COLIMITA-SIX", primera.cuerpo)

    def test_sin_discrepancias_lo_dice_claro(self):
        LineaASN = django_apps.get_model("inventario", "LineaASN")
        LineaASN.objects.filter(orden=self.orden).update(
            cantidad_recibida=125, cantidad_danada=0,
        )
        notificacion = services.enviar_recepcion_cerrada(self.orden)
        self.assertIn("sin diferencias", notificacion.cuerpo)
        self.assertNotIn("faltaron", notificacion.cuerpo)

    def test_sin_telefono_no_truena_y_sale_por_consola(self):
        self.cliente.contacto_whatsapp = ""
        self.cliente.save(update_fields=["contacto_whatsapp"])
        notificacion = services.enviar_recepcion_cerrada(self.orden)
        self.assertIsNotNone(notificacion)  # el aviso no se pierde
        self.assertEqual(notificacion.canal, "consola")
        self.assertEqual(notificacion.destinatario, "cliente:colima")
        from apps.core.models import EventoAuditoria
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="notificacion", entidad_id=self.orden.folio, accion="envio_omitido",
            ).exists()
        )
