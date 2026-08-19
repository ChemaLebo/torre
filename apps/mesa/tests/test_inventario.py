"""Inventario en Mesa: resumen por cliente, detalle de SKU con kardex, ajuste
con doble firma desde la vista y gestión de ubicaciones."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.inventario.models import Ajuste, LineaASN, OrdenEntrada, Saldo


def crear_usuario(username, rol, cliente=None, pin=""):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente, pin=pin)
    return user


class BaseInventarioMesa(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        cls.usuario_mesa = crear_usuario("mesa1", "mesa", pin="3333")
        cls.usuario_piso = crear_usuario("piso1", "piso", pin="1111")
        cls.usuario_portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.ubic_recepcion = Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        cls.ubic_picking = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        cls.sku = SKU.objects.create(
            cliente=cls.colima, codigo="COLIMITA-SIX", descripcion="Colimita six pack",
            requiere_lote=False, precio_declarado=Decimal("180.00"), punto_reorden=5,
        )
        cls.url = reverse("mesa:inventario")

    def entrar_mesa(self):
        self.client.force_login(self.usuario_mesa)

    def crear_stock(self, cantidad=20):
        """Stock vendible por la puerta oficial: ASN → recibir → ubicar."""
        from apps.inventario.services import recibir, ubicar

        orden = OrdenEntrada.objects.create(cliente=self.colima)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=cantidad)
        recibir(linea, cantidad, 0, self.usuario_piso)
        ubicar(self.sku, cantidad, self.ubic_picking, None, self.usuario_piso)
        return orden

    def vendible(self):
        total = Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).aggregate(
            t=Sum("cantidad")
        )["t"]
        return total or 0


class AccesoInventarioMesaTests(BaseInventarioMesa):
    def test_portal_no_entra(self):
        self.client.force_login(self.usuario_portal)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_piso_no_entra(self):
        """mesa:inventario es solo rol mesa: el piso no ajusta desde aquí."""
        self.client.force_login(self.usuario_piso)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"accion": "ajustar"}).status_code, 403)

    def test_anonimo_va_a_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)


class ResumenInventarioMesaTests(BaseInventarioMesa):
    def test_sin_cliente_muestra_selector(self):
        self.entrar_mesa()
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertContains(respuesta, "Mezcal Nocturno")
        self.assertContains(respuesta, "Elige un cliente")

    def test_resumen_por_cliente_con_fila_total(self):
        self.crear_stock(20)
        self.entrar_mesa()
        respuesta = self.client.get(self.url, {"cliente": "colima"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "COLIMITA-SIX")
        self.assertContains(respuesta, "Inventario de Cervecería Colima")
        self.assertContains(respuesta, "Total")
        # 20 vendibles, nada apartado: disponible 20, arriba del reorden (5).
        self.assertNotContains(respuesta, "por resurtir")

    def test_pill_por_resurtir_bajo_reorden(self):
        self.crear_stock(3)  # disponible 3 <= punto_reorden 5
        self.entrar_mesa()
        respuesta = self.client.get(self.url, {"cliente": "colima"})
        self.assertContains(respuesta, "por resurtir")

    def test_detalle_de_sku_con_saldos_kardex_y_form_de_ajuste(self):
        orden = self.crear_stock(20)
        self.entrar_mesa()
        respuesta = self.client.get(self.url, {"cliente": "colima", "sku": "COLIMITA-SIX"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Saldos por ubicación")
        self.assertContains(respuesta, "A-01-1")
        # Kardex: la recepción (referencia ASN) y el put-away quedan visibles.
        self.assertContains(respuesta, orden.folio)
        self.assertContains(respuesta, "Put-away")
        self.assertContains(respuesta, "Ajuste con doble firma")

    def test_sku_de_otro_cliente_es_404(self):
        self.entrar_mesa()
        respuesta = self.client.get(self.url, {"cliente": "nocturno", "sku": "COLIMITA-SIX"})
        self.assertEqual(respuesta.status_code, 404)


class AjusteDesdeMesaTests(BaseInventarioMesa):
    def datos_ajuste(self, **extra):
        datos = {
            "accion": "ajustar",
            "cliente": "colima",
            "sku": "COLIMITA-SIX",
            "delta": "-2",
            "motivo": "conteo",
            "incidencia_ref": "",
            "autorizo_1": "piso1",
            "pin_1": "1111",
            "autorizo_2": "mesa1",
            "pin_2": "3333",
        }
        datos.update(extra)
        return datos

    def test_ajuste_feliz_crea_ajuste_y_mueve_saldo(self):
        self.crear_stock(20)
        self.entrar_mesa()
        respuesta = self.client.post(self.url, self.datos_ajuste(), follow=True)

        ajuste = Ajuste.objects.get()
        self.assertEqual(ajuste.delta, -2)
        self.assertEqual(ajuste.motivo, "conteo")
        self.assertEqual(ajuste.autorizo_1, "piso1")
        self.assertEqual(ajuste.autorizo_2, "mesa1")
        self.assertEqual(self.vendible(), 18)
        self.assertContains(respuesta, f"Ajuste {ajuste.folio} aplicado")
        self.assertContains(respuesta, "Firmaron piso1 y mesa1")

    def test_ajuste_con_pin_malo_no_mueve_nada(self):
        self.crear_stock(20)
        self.entrar_mesa()
        respuesta = self.client.post(
            self.url, self.datos_ajuste(pin_2="0000"), follow=True,
        )
        self.assertContains(respuesta, "Firma inválida")
        self.assertEqual(Ajuste.objects.count(), 0)
        self.assertEqual(self.vendible(), 20)

    def test_delta_no_numerico_muestra_error(self):
        self.crear_stock(20)
        self.entrar_mesa()
        respuesta = self.client.post(
            self.url, self.datos_ajuste(delta="dos"), follow=True,
        )
        self.assertContains(respuesta, "número entero con signo")
        self.assertEqual(Ajuste.objects.count(), 0)
        self.assertEqual(self.vendible(), 20)


class UbicacionesMesaTests(BaseInventarioMesa):
    def test_lista_de_ubicaciones(self):
        self.entrar_mesa()
        respuesta = self.client.get(self.url, {"ver": "ubicaciones"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "REC-01")
        self.assertContains(respuesta, "A-01-1")
        self.assertContains(respuesta, "Nueva ubicación")

    def test_ubicacion_nueva_con_auditoria(self):
        self.entrar_mesa()
        respuesta = self.client.post(self.url, {
            "accion": "ubicacion_nueva", "codigo": "b-02-1", "tipo": "picking",
        }, follow=True)
        ubic = Ubicacion.objects.get(codigo="B-02-1")  # normalizada a mayúsculas
        self.assertEqual(ubic.tipo, Ubicacion.PICKING)
        self.assertTrue(ubic.activo)
        self.assertContains(respuesta, "B-02-1")
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="ubicacion", entidad_id="B-02-1", accion="alta",
            ).exists()
        )

    def test_ubicacion_duplicada_rechazada(self):
        self.entrar_mesa()
        respuesta = self.client.post(self.url, {
            "accion": "ubicacion_nueva", "codigo": "rec-01", "tipo": "recepcion",
        }, follow=True)
        self.assertContains(respuesta, "Ya existe la ubicación")
        self.assertEqual(Ubicacion.objects.filter(codigo__iexact="REC-01").count(), 1)

    def test_toggle_con_saldos_vivos_rechaza(self):
        self.crear_stock(20)  # deja 20 vendibles en A-01-1
        self.entrar_mesa()
        respuesta = self.client.post(self.url, {
            "accion": "ubicacion_toggle", "ubicacion_id": self.ubic_picking.pk,
        }, follow=True)
        self.assertContains(
            respuesta, "todavía tiene 20 piezas; muévelas antes de apagarla",
        )
        self.ubic_picking.refresh_from_db()
        self.assertTrue(self.ubic_picking.activo)

    def test_toggle_apaga_y_prende_con_auditoria(self):
        vacia = Ubicacion.objects.create(codigo="C-01-1", tipo=Ubicacion.RESERVA)
        self.entrar_mesa()
        self.client.post(self.url, {"accion": "ubicacion_toggle", "ubicacion_id": vacia.pk})
        vacia.refresh_from_db()
        self.assertFalse(vacia.activo)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="ubicacion", entidad_id="C-01-1", accion="desactivada",
            ).exists()
        )
        self.client.post(self.url, {"accion": "ubicacion_toggle", "ubicacion_id": vacia.pk})
        vacia.refresh_from_db()
        self.assertTrue(vacia.activo)

    def test_corral_nuevo_con_carriers(self):
        self.entrar_mesa()
        self.client.post(self.url, {
            "accion": "ubicacion_nueva", "codigo": "sal-99min", "tipo": "salida",
            "carriers": " noventa9Minutos , fedex ,",
        }, follow=True)
        ubic = Ubicacion.objects.get(codigo="SAL-99MIN")
        self.assertEqual(ubic.carriers, "noventa9Minutos,fedex")  # normalizados

    def test_carriers_en_no_corral_rechazados(self):
        self.entrar_mesa()
        respuesta = self.client.post(self.url, {
            "accion": "ubicacion_nueva", "codigo": "c-02-1", "tipo": "picking",
            "carriers": "fedex",
        }, follow=True)
        self.assertContains(respuesta, "solo aplican a corrales")
        self.assertFalse(Ubicacion.objects.filter(codigo="C-02-1").exists())

    def test_editar_carriers_de_corral_con_auditoria(self):
        corral = Ubicacion.objects.create(
            codigo="SAL-X", tipo=Ubicacion.SALIDA, carriers="fedex",
        )
        self.entrar_mesa()
        self.client.post(self.url, {
            "accion": "ubicacion_carriers", "ubicacion_id": corral.pk,
            "carriers": "noventa9Minutos",
        }, follow=True)
        corral.refresh_from_db()
        self.assertEqual(corral.carriers, "noventa9Minutos")
        evento = EventoAuditoria.objects.get(
            entidad="ubicacion", entidad_id="SAL-X", accion="carriers_actualizados",
        )
        self.assertEqual(evento.delta, {"antes": "fedex", "ahora": "noventa9Minutos"})

    def test_editar_carriers_de_no_corral_rechazado(self):
        self.entrar_mesa()
        respuesta = self.client.post(self.url, {
            "accion": "ubicacion_carriers", "ubicacion_id": self.ubic_picking.pk,
            "carriers": "fedex",
        }, follow=True)
        self.assertContains(respuesta, "solo aplican a corrales")
        self.ubic_picking.refresh_from_db()
        self.assertEqual(self.ubic_picking.carriers, "")
