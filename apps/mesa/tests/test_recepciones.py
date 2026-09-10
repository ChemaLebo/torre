"""Recepciones desde Mesa: tablero global de ASNs y captura del aviso que
llega por WhatsApp (alta en dos pasos: primero el cliente, luego el anuncio)."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import SKU
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.inventario.models import LineaASN, OrdenEntrada


def crear_usuario(username, rol, cliente=None):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


class BaseRecepcionesMesa(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        cls.usuario_mesa = crear_usuario("mesa1", "mesa")
        cls.usuario_portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.usuario_piso = crear_usuario("piso1", "piso")
        cls.sku_colima = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six pack",
        )
        cls.sku_nocturno = SKU.objects.create(
            cliente=cls.nocturno, codigo="MEZCAL-750", descripcion="Mezcal joven 750 ml",
        )
        cls.url = reverse("mesa:recepciones")

    def entrar_mesa(self):
        self.client.force_login(self.usuario_mesa)


class AccesoRecepcionesMesaTests(BaseRecepcionesMesa):
    def test_portal_no_entra(self):
        self.client.force_login(self.usuario_portal)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_piso_no_entra(self):
        self.client.force_login(self.usuario_piso)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonimo_va_a_login(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 302)


class ListaRecepcionesMesaTests(BaseRecepcionesMesa):
    def test_lista_asns_de_todos_los_clientes(self):
        abierta_colima = OrdenEntrada.objects.create(cliente=self.colima, tarimas=2)
        LineaASN.objects.create(orden=abierta_colima, sku=self.sku_colima, cantidad_anunciada=48)
        abierta_nocturno = OrdenEntrada.objects.create(cliente=self.nocturno)
        LineaASN.objects.create(orden=abierta_nocturno, sku=self.sku_nocturno, cantidad_anunciada=12)
        cerrada = OrdenEntrada.objects.create(cliente=self.colima, estado=OrdenEntrada.CERRADA)

        self.entrar_mesa()
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, abierta_colima.folio)
        self.assertContains(respuesta, abierta_nocturno.folio)
        self.assertContains(respuesta, cerrada.folio)  # en las últimas cerradas
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertContains(respuesta, "Mezcal Nocturno")

    def test_sin_cliente_muestra_selector(self):
        self.entrar_mesa()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Primero elige el cliente")
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertContains(respuesta, "Mezcal Nocturno")

    def test_con_cliente_muestra_el_form_de_anuncio(self):
        self.entrar_mesa()
        respuesta = self.client.get(self.url, {"cliente": "colima"})
        self.assertContains(respuesta, "Capturar ASN de Cervecería Colima")
        self.assertContains(respuesta, "COLIMITA-SIX")
        self.assertNotContains(respuesta, "MEZCAL-750")  # SKUs acotados al cliente


class AltaAsnMesaTests(BaseRecepcionesMesa):
    def test_alta_crea_asn_con_lineas_consolidadas_y_evento(self):
        self.entrar_mesa()
        fecha = timezone.localdate() + timedelta(days=2)
        respuesta = self.client.post(self.url, {
            "cliente": "colima",
            "fecha_compromiso": fecha.isoformat(),
            "tarimas": "3",
            "sku_1": str(self.sku_colima.pk),
            "cantidad_1": "24",
            "sku_2": str(self.sku_colima.pk),
            "cantidad_2": "24",
        })
        self.assertRedirects(respuesta, self.url)

        orden = OrdenEntrada.objects.get(cliente=self.colima)
        self.assertEqual(orden.estado, OrdenEntrada.ANUNCIADA)
        self.assertEqual(orden.fecha_compromiso, fecha)
        self.assertEqual(orden.tarimas, 3)
        linea = orden.lineas.get()  # renglones duplicados consolidados
        self.assertEqual(linea.sku_id, self.sku_colima.pk)
        self.assertEqual(linea.cantidad_anunciada, 48)

        evento = EventoAuditoria.objects.get(
            entidad="asn", entidad_id=orden.folio, accion="anunciada_mesa",
        )
        self.assertEqual(evento.delta["tarimas"], 3)
        self.assertEqual(evento.delta["lineas"], [{"sku": "COLIMITA-SIX", "cantidad": 48}])
        self.assertIn("Capturada por Mesa", evento.motivo)

    def test_alta_rechaza_sku_de_otro_cliente(self):
        self.entrar_mesa()
        fecha = timezone.localdate() + timedelta(days=2)
        respuesta = self.client.post(self.url, {
            "cliente": "colima",
            "fecha_compromiso": fecha.isoformat(),
            "sku_1": str(self.sku_nocturno.pk),
            "cantidad_1": "10",
        })
        self.assertEqual(respuesta.status_code, 200)  # re-render con error de form
        self.assertEqual(OrdenEntrada.objects.count(), 0)

    def test_alta_exige_fecha_de_hoy_en_adelante(self):
        self.entrar_mesa()
        ayer = timezone.localdate() - timedelta(days=1)
        respuesta = self.client.post(self.url, {
            "cliente": "colima",
            "fecha_compromiso": ayer.isoformat(),
            "sku_1": str(self.sku_colima.pk),
            "cantidad_1": "10",
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "de hoy en adelante")
        self.assertEqual(OrdenEntrada.objects.count(), 0)


class AltaAsnConLoteTests(BaseRecepcionesMesa):
    def test_alta_guarda_lote_y_caducidad_por_linea(self):
        self.entrar_mesa()
        fecha = timezone.localdate() + timedelta(days=2)
        respuesta = self.client.post(self.url, {
            "cliente": "colima", "fecha_compromiso": fecha.isoformat(),
            "sku_1": str(self.sku_colima.pk), "cantidad_1": "24", "lote_1": "L-2026-09", "caducidad_1": "2027-03-01",
            "sku_2": str(self.sku_colima.pk), "cantidad_2": "6", "lote_2": "L-2026-10",
        })
        self.assertRedirects(respuesta, self.url)
        orden = OrdenEntrada.objects.get(cliente=self.colima)
        lineas = {l.lote_codigo: l for l in orden.lineas.all()}
        self.assertEqual(set(lineas), {"L-2026-09", "L-2026-10"})
        self.assertEqual(lineas["L-2026-09"].fecha_caducidad.isoformat(), "2027-03-01")
        self.assertIsNone(lineas["L-2026-10"].fecha_caducidad)
        evento = EventoAuditoria.objects.get(entidad="asn", entidad_id=orden.folio, accion="anunciada_mesa")
        self.assertEqual(evento.delta["lineas"][0]["lote"], "L-2026-09")

    def test_formato_csv_con_productos_marcados(self):
        kit = SKU.objects.create(cliente=self.colima, codigo="KIT-3", descripcion="Kit", es_kit=True)
        self.entrar_mesa()
        respuesta = self.client.get(self.url + "?cliente=colima")
        self.assertContains(respuesta, "Descargar formato (CSV)")
        self.assertContains(respuesta, f'name="sku" value="{self.sku_colima.pk}"')
        self.assertNotContains(respuesta, f'name="sku" value="{kit.pk}"')
        self.assertContains(respuesta, 'id="asn-codigos"')
        respuesta = self.client.get(reverse("mesa:recepciones_plantilla"), {
            "cliente": "colima", "sku": [str(self.sku_colima.pk), str(kit.pk)],
        })
        lineas = respuesta.content.decode("utf-8-sig").splitlines()
        self.assertEqual(lineas, [
            "codigo,descripcion,cantidad,lote,caducidad",
            f"{self.sku_colima.codigo},{self.sku_colima.descripcion},,,AAAA-MM-DD",
        ])

    def test_form_muestra_columnas_de_lote_y_datalist(self):
        from apps.catalogo.models import Lote

        Lote.objects.create(sku=self.sku_colima, codigo="L-PREVIO", fecha_caducidad=date(2027, 6, 1))
        self.entrar_mesa()
        respuesta = self.client.get(self.url + "?cliente=colima")
        self.assertContains(respuesta, 'name="lote_1"')
        self.assertContains(respuesta, 'id="lotes-recientes"')
        # La sugerencia trae la caducidad para autollenarla al elegir el lote.
        self.assertContains(respuesta, 'value="L-PREVIO" data-caducidad="2027-06-01"')
