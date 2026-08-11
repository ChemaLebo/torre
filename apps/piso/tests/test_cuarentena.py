"""Pantalla de cuarentena del piso: lista de saldos y dictamen con dos firmas."""
from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo

from .base import PisoTestCase


class CuarentenaPisoBase(PisoTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.jefe = get_user_model().objects.create_user("jefe", password="pin-jefe")
        PerfilUsuario.objects.create(usuario=cls.jefe, rol=PerfilUsuario.ROL_PISO, pin="2222")
        cls.url = reverse("piso:cuarentena")

    def crear_cuarentena(self, danadas=4):
        """Cuarentena por la puerta oficial: ASN recibida con dañadas."""
        from apps.inventario.services import recibir

        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.sku, cantidad_anunciada=danadas)
        recibir(linea, 0, danadas, self.operador)
        return Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA)

    def suma_cuarentena(self):
        total = Saldo.objects.filter(sku=self.sku, estado=Saldo.CUARENTENA).aggregate(
            t=Sum("cantidad")
        )["t"]
        return total or 0

    def post_dictamen(self, saldo, destino, cantidad=None, pin_2="2222", **extra):
        datos = {
            "accion": "dictamen",
            "saldo_id": saldo.pk,
            "cantidad": cantidad if cantidad is not None else saldo.cantidad,
            "destino": destino,
            "motivo_texto": "Revisión de piso",
            "autorizo_1": "piso1",
            "pin_1": "1111",
            "autorizo_2": "jefe",
            "pin_2": pin_2,
        }
        datos.update(extra)
        return self.client.post(self.url, datos, follow=True)


class AccesoCuarentenaTests(CuarentenaPisoBase):
    def test_portal_no_entra(self):
        self.client.force_login(self.usuario_portal)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"accion": "dictamen"}).status_code, 403)

    def test_anonimo_va_a_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)


class ListaCuarentenaTests(CuarentenaPisoBase):
    def test_lista_saldos_en_cuarentena_con_banner(self):
        self.crear_cuarentena(4)
        self.login_piso()
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Nada sale de cuarentena sin dictamen con dos firmas.")
        self.assertContains(respuesta, self.sku.codigo)
        self.assertContains(respuesta, "Cervecería Colima")
        self.assertContains(respuesta, "REC-01")

    def test_cuarentena_vacia(self):
        self.login_piso()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Cuarentena vacía")


class DictamenPisoTests(CuarentenaPisoBase):
    def test_dictamen_revendible_feliz(self):
        saldo = self.crear_cuarentena(4)
        self.login_piso()
        respuesta = self.post_dictamen(saldo, "revendible")
        self.assertContains(
            respuesta, "4 piezas regresan a put-away — ubícalas para que vuelvan a estar vendibles."
        )
        self.assertEqual(self.suma_cuarentena(), 0)
        putaway = Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).aggregate(
            t=Sum("cantidad")
        )["t"]
        self.assertEqual(putaway, 4)

    def test_dictamen_merma_feliz(self):
        saldo = self.crear_cuarentena(4)
        self.login_piso()
        respuesta = self.post_dictamen(saldo, "merma", cantidad=3)
        self.assertContains(respuesta, "3 piezas dadas de baja como merma. Quedó en el kardex.")
        self.assertEqual(self.suma_cuarentena(), 1)
        mov = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.MERMA)
        self.assertEqual(mov.delta, -3)
        self.assertEqual(mov.referencia, "DICTAMEN")

    def test_firmas_malas_muestran_error_y_no_mueven_stock(self):
        saldo = self.crear_cuarentena(4)
        self.login_piso()
        respuesta = self.post_dictamen(saldo, "merma", pin_2="0000")
        self.assertContains(respuesta, "Firma inválida")
        self.assertEqual(self.suma_cuarentena(), 4)
        self.assertFalse(Movimiento.objects.filter(tipo=Movimiento.MERMA).exists())

    def test_misma_persona_no_firma_dos_veces(self):
        saldo = self.crear_cuarentena(4)
        self.login_piso()
        respuesta = self.post_dictamen(
            saldo, "merma", autorizo_2="piso1", pin_2="1111",
        )
        self.assertContains(respuesta, "dos personas distintas")
        self.assertEqual(self.suma_cuarentena(), 4)

    def test_cantidad_mayor_al_saldo_muestra_error(self):
        saldo = self.crear_cuarentena(2)
        self.login_piso()
        respuesta = self.post_dictamen(saldo, "merma", cantidad=9)
        self.assertContains(respuesta, "Esa fila solo tiene 2 piezas")
        self.assertEqual(self.suma_cuarentena(), 2)


class DictamenPorFilaPisoTests(CuarentenaPisoBase):
    """La vista pasa la fila exacta (ubicación + lote): dictaminar una fila
    jamás descuenta otra fila del mismo SKU."""

    def crear_dos_filas(self):
        """Por las puertas oficiales: dañadas de ASN (REC-01) + retorno (RET-01)."""
        from apps.catalogo.models import Ubicacion
        from apps.inventario.services import retornar

        fila_rec = self.crear_cuarentena(2)
        Ubicacion.objects.create(codigo="RET-01", tipo=Ubicacion.RETORNO)
        retornar(self.sku, 3, "INC-2026-0001", self.operador)
        fila_ret = Saldo.objects.get(
            sku=self.sku, estado=Saldo.CUARENTENA, ubicacion__codigo="RET-01",
        )
        return fila_rec, fila_ret

    def test_dictamen_del_retorno_no_toca_las_danadas_de_recepcion(self):
        fila_rec, fila_ret = self.crear_dos_filas()
        self.login_piso()
        respuesta = self.post_dictamen(fila_ret, "merma", cantidad=3)
        self.assertContains(respuesta, "3 piezas dadas de baja como merma")
        fila_rec.refresh_from_db()
        self.assertEqual(fila_rec.cantidad, 2)  # intacta
        self.assertFalse(Saldo.objects.filter(pk=fila_ret.pk).exists())

    def test_cantidad_mayor_a_la_fila_muestra_error_aunque_el_total_alcance(self):
        fila_rec, fila_ret = self.crear_dos_filas()
        self.login_piso()
        respuesta = self.post_dictamen(fila_ret, "merma", cantidad=4)  # total SKU: 5
        self.assertContains(respuesta, "Esa fila solo tiene 3 piezas")
        self.assertEqual(self.suma_cuarentena(), 5)


class PutawayPendientePisoTests(CuarentenaPisoBase):
    """F7: el put-away suelto (dictamen revendible, retornos) tiene puerta de
    salida en piso:cuarentena — de ahí vuelve a vendible."""

    def test_revendible_aparece_en_putaway_pendiente_y_se_ubica_hasta_vendible(self):
        from apps.inventario.services import resumen_sku

        saldo = self.crear_cuarentena(4)
        self.login_piso()
        self.post_dictamen(saldo, "revendible")

        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Put-away pendiente")
        self.assertContains(respuesta, 'value="ubicar_putaway"')
        putaway = Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY)

        respuesta = self.client.post(self.url, {
            "accion": "ubicar_putaway",
            "saldo_id": putaway.pk,
            "cantidad": "4",
            "ubicacion": "a-01-1",  # escaneo tolerante a mayúsculas/minúsculas
        }, follow=True)
        self.assertContains(respuesta, "ya cuentan como vendibles")
        resumen = resumen_sku(self.sku)
        self.assertEqual(resumen["vendible"], 4)
        self.assertEqual(resumen["en_recepcion"], 0)
        # Desaparece de la lista de pendientes.
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Nada pendiente de ubicar.")

    def test_ubicacion_inexistente_muestra_error_y_no_mueve_stock(self):
        saldo = self.crear_cuarentena(2)
        self.login_piso()
        self.post_dictamen(saldo, "revendible")
        putaway = Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY)
        respuesta = self.client.post(self.url, {
            "accion": "ubicar_putaway", "saldo_id": putaway.pk,
            "cantidad": "2", "ubicacion": "Z-99-9",
        }, follow=True)
        self.assertContains(respuesta, "No existe la ubicación")
        self.assertEqual(
            Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).count(), 1
        )

    def test_fila_sin_lote_de_sku_con_lote_pide_lote_al_ubicar(self):
        from apps.catalogo.models import Lote

        self.sku.requiere_lote = True
        self.sku.save(update_fields=["requiere_lote"])
        saldo = self.crear_cuarentena(2)
        self.login_piso()
        self.post_dictamen(saldo, "revendible")
        putaway = Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY)

        # El template pide el lote para esta fila (la fila no lo trae).
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Lote (el producto lo pide)")

        respuesta = self.client.post(self.url, {
            "accion": "ubicar_putaway", "saldo_id": putaway.pk,
            "cantidad": "2", "ubicacion": "A-01-1",
            "lote": "L-2026-08", "fecha_caducidad": "2026-12-31",
        }, follow=True)
        self.assertContains(respuesta, "ya cuentan como vendibles")
        lote = Lote.objects.get(sku=self.sku, codigo="L-2026-08")
        vendible = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual(vendible.lote_id, lote.pk)
        self.assertEqual(vendible.cantidad, 2)
