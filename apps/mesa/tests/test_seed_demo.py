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


# DEBUG=True: el seed es herramienta de demo y sus tiendas sin token usan el
# modo mock del sync, que en producción (DEBUG=0) ahora es fail-closed.
@override_settings(MEDIA_ROOT=MEDIA_DEMO, DEBUG=True)
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
                if pin:
                    # El PIN vive hasheado: se verifica, no se compara en claro.
                    self.assertTrue(user.perfil.check_pin(pin), f"PIN de {username} no coincide")
                    self.assertNotEqual(user.perfil.pin, pin, "el PIN quedó en claro en la BD")
                else:
                    self.assertEqual(user.perfil.pin, "")
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
            Pedido.PARCIALMENTE_DESPACHADO, Pedido.CANCELADO, Pedido.RETORNADO,
        }
        self.assertTrue(esperados.issubset(estados), f"faltan estados: {esperados - estados}")

    def test_cancelado_a_medio_picking_deja_reingreso_para_el_piso(self):
        from apps.inventario.models import OrdenEntrada
        from apps.pedidos.models import Pedido

        reingreso = OrdenEntrada.objects.get(tipo=OrdenEntrada.TIPO_REINGRESO)
        self.assertEqual(reingreso.pedido.estado, Pedido.CANCELADO)
        self.assertEqual(reingreso.pedido.reingreso_estado, Pedido.REINGRESADO)
        self.assertEqual(reingreso.lineas.get().cantidad_recibida, 1)

    def test_reporte_del_dia_tiene_casos_de_hoy_y_de_ayer(self):
        from django.utils import timezone

        from apps.pedidos import reportes
        from apps.pedidos.models import Pedido

        hoy = timezone.localdate()
        de_hoy = list(reportes.pedidos_con_actividad(hoy))
        estados_hoy = {p.estado for p in de_hoy}
        for estado in (Pedido.PENDIENTE, Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA,
                       Pedido.RECOLECTADO, Pedido.CANCELADO, Pedido.ENTREGADO):
            self.assertIn(estado, estados_hoy)
        entregado_hoy = Pedido.objects.get(shopify_order_id="5018")
        self.assertIn(entregado_hoy, de_hoy)
        self.assertLess(entregado_hoy.creado.date(), hoy)
        # Lo fechado días atrás NO cae en hoy: actualizado sigue al último paso.
        self.assertNotIn(Pedido.objects.get(shopify_order_id="5001"), de_hoy)
        [r] = reportes.armar_reporte([entregado_hoy], con_operador=True)
        etapas = {e["clave"]: e for e in r["etapas"]}
        self.assertEqual(len(etapas["empaque"]["fotos"]), 2)
        self.assertEqual(len(etapas["entrega"]["fotos"]), 1)
        self.assertEqual(etapas["entrega"]["operador"], "jefe")

    def test_canales_de_venta_sembrados(self):
        from apps.pedidos.models import Pedido

        canales = {p.shopify_order_id: p.canal for p in Pedido.objects.filter(shopify_order_id__in=["5009", "5011", "5001"])}
        self.assertEqual(canales, {"5009": Pedido.CANAL_B2B, "5011": Pedido.CANAL_TIKTOK, "5001": Pedido.CANAL_WEB})

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

        # El escenario Lote A (sin existencias) agrega una FAL ABIERTA: esta
        # aserción apunta a la FAL histórica cerrada del demo.
        fal = Incidencia.objects.get(tipo="FAL", estado=Incidencia.CERRADA)
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
        # Lotes en la ASN: dos renglones anunciados con lote y caducidad, uno sin.
        lineas = anunciada.first().lineas
        self.assertEqual(lineas.filter(lote_codigo="LC-2609", fecha_caducidad__isnull=False).count(), 1)
        self.assertEqual(lineas.filter(lote_codigo="").count(), 1)

    def test_lote_extra_caduca_antes_y_tiene_stock(self):
        from apps.catalogo.models import Lote
        from apps.inventario.models import Saldo

        extra = Lote.objects.get(sku__codigo="COLIMITA-SIX", codigo="LC-2512")
        base = Lote.objects.get(sku__codigo="COLIMITA-SIX", codigo="LC-2601")
        self.assertLess(extra.fecha_caducidad, base.fecha_caducidad)
        self.assertTrue(
            Saldo.objects.filter(lote=extra, ubicacion__codigo="A-02-1", cantidad__gt=0).exists()
        )

    def test_reingresos_por_decidir_sembrados(self):
        from apps.incidencias.models import Incidencia
        from apps.inventario.models import Saldo
        from apps.pedidos.models import Pedido
        from apps.pedidos.services import reingresos_por_decidir

        por_decidir = list(reingresos_por_decidir())
        retornado = Pedido.objects.get(estado=Pedido.RETORNADO)
        tardia = Incidencia.objects.get(tipo="CAN").pedido
        self.assertIn(retornado, por_decidir)
        self.assertIn(tardia, por_decidir)
        self.assertEqual(tardia.estado, Pedido.EN_TRANSITO)
        # Nada entró a cuarentena por adelantado: la decisión es de Mesa.
        self.assertFalse(Saldo.objects.filter(estado=Saldo.CUARENTENA, sku__codigo="PARAMO-C12").exists())

    def test_en_empaque_huerfano_se_limpia_con_el_command(self):
        from apps.inventario.management.commands.limpiar_en_empaque import (
            excedentes_en_empaque,
        )

        excedentes = {sku.codigo: exceso for sku, _real, _resp, exceso in excedentes_en_empaque()}
        self.assertEqual(excedentes, {"CAYACO-SIX": 2})
        call_command("limpiar_en_empaque", "--aplicar", stdout=StringIO())
        self.assertEqual(excedentes_en_empaque(), [])
        # Re-correr el seed no vuelve a sembrar el fantasma ya limpiado.
        correr_seed()
        self.assertEqual(excedentes_en_empaque(), [])

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
        # Las diferencias de conteo sembradas quedan bajo umbral: no abren DES.
        # La única DES es la de la recepción con faltante, ligada a su orden.
        des = Incidencia.objects.get(tipo="DES")
        self.assertIsNotNone(des.orden)
        self.assertEqual(des.orden.lineas.get().cantidad_recibida, 21)

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


class SeedEscenariosLoteATests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_escenarios_lote_a_sembrados(self):
        from apps.catalogo.models import SKU
        from apps.core.models import Cliente
        from apps.pedidos.models import LineaPedido, Pedido

        kit = SKU.objects.get(codigo="COL-MYSTERY3")
        self.assertTrue(kit.es_kit)
        self.assertEqual(kit.productos_por_kit, 3)
        self.assertTrue(kit.codigo_barras)
        lineas_kit = LineaPedido.objects.filter(sku=kit)
        self.assertGreaterEqual(lineas_kit.count(), 2)  # 88001 + stepper 88004
        for linea_kit in lineas_kit:
            self.assertTrue(linea_kit.reservada)  # virtual: pasa el gate de picking
        self.assertTrue(
            LineaPedido.objects.filter(sku__codigo="COL-AGOTADO", reservada=False).exists()
        )
        replan = Pedido.objects.get(shopify_order_id="88003")
        self.assertEqual(replan.paquetes.get().carrier, "fedex")  # fuera del reparto → replan al generar
        self.assertEqual(
            Cliente.objects.get(slug="mezcal-nocturno").integracion_envios, "99minutos",
        )

    def test_salida_por_caja_parcial_y_pedido_de_dos_cajas_en_camino(self):
        from apps.envios.models import Guia, Paquete
        from apps.pedidos.models import Pedido

        parcial = Pedido.objects.get(shopify_order_id="5014")
        self.assertEqual(parcial.estado, Pedido.PARCIALMENTE_DESPACHADO)
        self.assertIsNotNone(parcial.ts_recolectado)
        c1, c2 = parcial.paquetes.order_by("numero")
        self.assertEqual((c1.estado, c2.estado), (Paquete.DESPACHADO, Paquete.EMPACADO))
        self.assertIsNotNone(c1.ts_cierre)
        self.assertIsNone(c2.ts_cierre)  # la caja 2 espera su foto de cierre en Salida
        self.assertEqual(parcial.guias.count(), 2)

        dos = Pedido.objects.get(shopify_order_id="5020")
        self.assertEqual(dos.estado, Pedido.EN_TRANSITO)
        self.assertEqual(set(dos.guias.values_list("estado", flat=True)), {Guia.EN_TRANSITO, Guia.ENTREGADO})
        self.assertTrue(all(c.estado == Paquete.DESPACHADO for c in dos.paquetes.all()))

    def test_lineas_con_precio_de_venta_para_el_reporte_de_ventas(self):
        from apps.pedidos.models import LineaPedido

        con_precio = LineaPedido.objects.filter(precio_unitario__isnull=False, parte_de_kit__isnull=True)
        self.assertGreater(con_precio.count(), 20)
        self.assertFalse(con_precio.filter(pedido__origen="manual").exists())

    def test_colima_reparte_por_porcentajes(self):
        from apps.core.models import Cliente
        from apps.envios.models import ReglaEnvio
        from apps.envios.reparto import reporte_mes
        from apps.pedidos.models import Pedido

        colima = Cliente.objects.get(slug="colima")
        self.assertEqual(colima.integracion_envios, "reparto")
        self.assertEqual(colima.reparto_pesos, {"noventa9Minutos": 75, "estafeta": 25})
        repartidos = Pedido.objects.filter(cliente=colima).exclude(reparto_carrier="")
        self.assertGreaterEqual(repartidos.count(), 8)
        self.assertEqual(colima.reparto_cursor, repartidos.count())
        self.assertTrue(set(repartidos.values_list("reparto_carrier", flat=True)) <= {"noventa9Minutos", "estafeta"})
        for pedido in repartidos.prefetch_related("guias"):
            for guia in pedido.guias.all():
                self.assertEqual(guia.carrier, pedido.reparto_carrier, pedido.folio)
        # Mérida va forzado a paquetexpress por regla: sin carta.
        merida = Pedido.objects.filter(cliente=colima, cp__startswith="97")
        self.assertTrue(merida.exists())
        self.assertFalse(merida.exclude(reparto_carrier="").exists())
        self.assertEqual(ReglaEnvio.objects.get(cliente=colima, prioridad=20).condicion, {"cp_prefijo": "97"})
        [bloque] = reporte_mes(*self._mes_completo())
        self.assertGreaterEqual(bloque["cartas"], 8)
        self.assertGreaterEqual(bloque["forzados_regla"], 1)

    def _mes_completo(self):
        from datetime import timedelta

        from django.utils import timezone

        ahora = timezone.now()
        return ahora - timedelta(days=30), ahora + timedelta(days=1)
