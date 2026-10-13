"""Recepción por escaneo en el piso (Chema 2026-10-13).

Foto de llegada → ¿dónde estás escaneando? (tarima existente o nueva con nombre
editable) → una caja por escaneo (código de barras abre los lotes; QR de Torre
suma directo; el teclado en español del lector se tolera) → tarjetas de lote
con "Otro" y "Dañado" → confirmar tarima (cuadró / faltaron / sobraron) →
cuadre con anunciadas y alarma espejo → cierre. Ciego: el piso nunca ve lo
anunciado antes del cuadre.
"""
from datetime import date

from django.test import override_settings
from django.urls import reverse

from apps.catalogo.models import SKU, Ubicacion
from apps.inventario.models import EscaneoRecepcion, LineaASN, OrdenEntrada, Saldo

from .base import PisoTestCase


class RecepcionEscaneoBase(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.sku.requiere_lote = True
        self.sku.save()
        self.sku_b = SKU.objects.create(
            cliente=self.cliente, codigo="CC24LCAR", codigo_barras="7501000000028", descripcion="Colimita 24 lata",
            requiere_lote=True,
        )
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=2)
        self.linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=10, lote_codigo="L1", fecha_caducidad=date(2027, 4, 22))
        self.linea_b = LineaASN.objects.create(orden=self.orden, sku=self.sku_b, cantidad_anunciada=5, lote_codigo="B1")
        self.detalle = reverse("piso:recepcion_detalle", args=[self.orden.pk])
        self.tarima_url = reverse("piso:recepcion_tarima", args=[self.orden.pk])
        self.lote_url = reverse("piso:recepcion_lote", args=[self.orden.pk])
        self.cuadre_url = reverse("piso:recepcion_cuadre", args=[self.orden.pk])

    def con_foto(self):
        self.client.post(self.detalle, {"accion": "foto", "foto_llegada": self.foto()})

    def en_tarima(self, codigo=""):
        """Foto + tarima elegida (nueva con el nombre dado o el propuesto)."""
        self.con_foto()
        r = self.client.post(self.tarima_url, {"accion": "nueva", "codigo": codigo}, follow=True)
        return r

    def escanear(self, codigo):
        return self.client.post(self.detalle, {"accion": "escanear", "codigo": codigo})

    def contar(self, n, sku=None, linea=None, danada=False):
        sku, linea = sku or self.sku, linea or self.linea
        for _ in range(n):
            datos = {"sku_id": sku.pk, "linea_id": linea.pk}
            if danada:
                datos["danada"] = "1"
            self.client.post(self.lote_url, datos)

    def vivos(self):
        return EscaneoRecepcion.objects.filter(orden=self.orden).exclude(estado=EscaneoRecepcion.ANULADA)


class FotoYTarimaTests(RecepcionEscaneoBase):
    def test_sin_foto_no_hay_escaneo_y_la_pantalla_pide_la_foto(self):
        r = self.client.get(self.detalle)
        self.assertContains(r, "Foto de llegada")
        self.assertNotContains(r, "Contando en")
        r = self.escanear(self.sku.codigo_barras)
        self.assertRedirects(r, self.detalle)
        self.assertEqual(self.vivos().count(), 0)

    def test_con_foto_y_sin_tarima_manda_a_elegirla(self):
        self.con_foto()
        r = self.client.get(self.detalle)
        self.assertRedirects(r, self.tarima_url)
        r = self.client.get(self.tarima_url)
        self.assertContains(r, "¿Dónde estás escaneando?")
        self.assertContains(r, 'value="TAR-01"')  # nombre propuesto, editable
        self.assertContains(r, "Todavía no hay tarimas")

    def test_tarima_nueva_con_nombre_propio_unica_y_tarima_existente(self):
        self.con_foto()
        r = self.client.post(self.tarima_url, {"accion": "nueva", "codigo": "tar-roja"}, follow=True)
        self.assertRedirects(r, self.detalle)
        self.assertContains(r, "Contando en <span class=\"mono\">TAR-ROJA</span>")
        self.assertTrue(Ubicacion.objects.filter(codigo="TAR-ROJA", tipo=Ubicacion.RESERVA).exists())
        r = self.client.post(self.tarima_url, {"accion": "nueva", "codigo": "TAR-ROJA"}, follow=True)
        self.assertContains(r, "Ya existe TAR-ROJA")
        r = self.client.post(self.tarima_url, {"accion": "nueva", "codigo": "RES-9"}, follow=True)
        self.assertContains(r, "empieza con TAR-")
        otra = Ubicacion.objects.create(codigo="TAR-05", tipo=Ubicacion.RESERVA)
        r = self.client.get(self.tarima_url)
        self.assertContains(r, f'data-id="{otra.pk}"')
        self.assertContains(r, "Regresar")
        r = self.client.post(self.tarima_url, {"accion": "elegir", "tarima_id": otra.pk}, follow=True)
        self.assertContains(r, "Contando en <span class=\"mono\">TAR-05</span>")
        r = self.client.post(self.tarima_url, {"accion": "elegir", "tarima_id": self.ubic_picking.pk}, follow=True)
        self.assertContains(r, "Elige una tarima de la lista")


class EscaneoTests(RecepcionEscaneoBase):
    def test_codigo_de_barras_abre_los_lotes_y_el_piso_no_ve_lo_anunciado(self):
        self.en_tarima()
        r = self.escanear(self.sku.codigo_barras)
        self.assertRedirects(r, f"{self.lote_url}?sku={self.sku.pk}")
        r = self.client.get(f"{self.lote_url}?sku={self.sku.pk}")
        self.assertContains(r, "¿Qué lote?")
        self.assertContains(r, 'data-lote="L1"')
        self.assertContains(r, "22/04/2027")
        self.assertContains(r, "Otro")
        self.assertContains(r, "Dañado")
        self.assertContains(r, "Regresar (mal conteo)")
        self.assertContains(r, "¿Contar 1 caja?")
        self.assertNotContains(r, "nunciad")  # ciego: nada de anunciadas
        self.assertNotContains(r, "de 10")
        self.assertEqual(self.vivos().count(), 0)  # abrir la pantalla no cuenta

    def test_elegir_lote_cuenta_una_caja_y_regresa_al_escaner(self):
        self.en_tarima()
        r = self.client.post(self.lote_url, {"sku_id": self.sku.pk, "linea_id": self.linea.pk}, follow=True)
        self.assertRedirects(r, self.detalle)
        self.assertContains(r, "COLIMITA-SIX lote L1: +1 en TAR-01")
        escaneo = self.vivos().get()
        self.assertEqual((escaneo.linea, escaneo.tarima.codigo, escaneo.estado, escaneo.danada), (self.linea, "TAR-01", "contada", False))
        self.assertEqual(Saldo.objects.count(), 0)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.EN_RECEPCION)
        r = self.client.get(self.detalle)
        self.assertContains(r, "1 caja de esta orden en esta tarima")
        self.assertNotContains(r, "Anunciadas")
        self.assertNotContains(r, ">10<")

    def test_otro_lote_y_danada(self):
        self.en_tarima()
        self.client.post(self.lote_url, {"sku_id": self.sku.pk, "nl_codigo": "L9", "nl_cad": "2027-01-15", "danada": "1"})
        nueva = self.orden.lineas.get(sku=self.sku, lote_codigo="L9")
        self.assertEqual((nueva.cantidad_anunciada, nueva.fecha_caducidad), (0, date(2027, 1, 15)))
        self.assertTrue(self.vivos().get().danada)
        r = self.client.post(self.lote_url, {"sku_id": self.sku.pk, "nl_codigo": ""}, follow=True)
        self.assertContains(r, "Elige un lote o captura")
        r = self.client.post(self.lote_url, {"sku_id": self.sku.pk, "nl_codigo": "L10", "nl_cad": "ayer"}, follow=True)
        self.assertContains(r, "no es válida")

    def test_qr_de_torre_suma_directo_incluso_con_teclado_en_espanol(self):
        self.en_tarima()
        r = self.escanear("P|7501234567890|L1")
        self.assertRedirects(r, self.detalle)
        r = self.escanear("P]7501234567890]L1")
        self.assertRedirects(r, self.detalle)
        self.assertEqual(self.vivos().filter(linea=self.linea).count(), 2)
        r = self.escanear("U]TAR'01")
        r = self.client.get(self.detalle)
        self.assertContains(r, "etiqueta de posición, no de producto")

    def test_producto_sin_lote_suma_directo_y_el_que_no_esta_en_la_orden_se_agrega(self):
        self.en_tarima()
        sin_lote = SKU.objects.create(cliente=self.cliente, codigo="SINLOTE", codigo_barras="7501000000035", descripcion="Sin lote", requiere_lote=False)
        r = self.escanear("7501000000035")
        self.assertRedirects(r, self.detalle)
        linea = self.orden.lineas.get(sku=sin_lote)
        self.assertEqual(linea.cantidad_anunciada, 0)
        self.assertEqual(self.vivos().filter(linea=linea).count(), 1)
        extra = SKU.objects.create(cliente=self.cliente, codigo="EXTRA", codigo_barras="7501000000042", descripcion="Extra", requiere_lote=True)
        r = self.escanear("7501000000042")
        self.assertRedirects(r, f"{self.lote_url}?sku={extra.pk}")
        r = self.client.get(f"{self.lote_url}?sku={extra.pk}")
        self.assertContains(r, "no venía en la orden")
        self.assertNotContains(r, "data-lote=")  # solo "Otro"

    def test_otro_cliente_no_registrado_y_cerrada(self):
        self.en_tarima()
        from apps.core.models import Cliente
        otro = Cliente.objects.create(nombre="Infinitea", slug="infinitea")
        SKU.objects.create(cliente=otro, codigo="TE", codigo_barras="7509999999999", descripcion="Té")
        r = self.escanear("7509999999999")
        r = self.client.get(self.detalle)
        self.assertContains(r, "es un producto de Infinitea")
        r = self.escanear("0000000000000")
        r = self.client.get(self.detalle)
        self.assertContains(r, "no está registrado")
        self.assertEqual(self.vivos().count(), 0)


@override_settings(TORRE_MODO_IMPRESION="relay")
class ReetiquetadoTests(RecepcionEscaneoBase):
    """Terminar conteo bloquea el escaneo e imprime (en relay: la cola); el
    reetiquetado ajusta con faltaron/sobraron, marca cada tarima y Terminado
    lleva al cuadre."""

    def setUp(self):
        super().setUp()
        self.en_tarima()
        self.tarima = Ubicacion.objects.get(codigo="TAR-01")
        self.reetiquetado = reverse("piso:recepcion_reetiquetado", args=[self.orden.pk])

    def terminar(self):
        return self.client.post(self.detalle, {"accion": "terminar_conteo"}, follow=True)

    def test_terminar_conteo_bloquea_el_escaneo_y_manda_al_reetiquetado(self):
        r = self.client.get(self.detalle)
        self.assertContains(r, "Terminar conteo")
        self.assertContains(r, "disabled")  # sin cajas no se termina
        self.contar(3)
        self.contar(1, danada=True)
        r = self.terminar()
        self.assertRedirects(r, self.reetiquetado)
        self.assertContains(r, "Conteo terminado: 4 etiqueta(s) de caja más las de TAR-01 van a la impresora")
        self.orden.refresh_from_db()
        self.assertIsNotNone(self.orden.ts_conteo_fin)
        self.assertEqual(self.orden.fase_recepcion, OrdenEntrada.FASE_REETIQUETADO)
        r = self.escanear("7501234567890")
        self.assertRedirects(r, self.reetiquetado)
        r = self.client.get(self.detalle)
        self.assertContains(r, "Conteo terminado el")
        self.assertContains(r, "Ir al reetiquetado")
        self.assertNotContains(r, 'name="codigo"')
        r = self.client.get(f"{self.lote_url}?sku={self.sku.pk}")
        self.assertRedirects(r, self.reetiquetado)
        r = self.terminar()
        self.assertContains(r, "ya estaba terminado")

    def test_regresar_a_conteo_reabre_y_un_segundo_termino_imprime_solo_lo_nuevo(self):
        self.contar(2)
        self.terminar()
        r = self.client.post(self.reetiquetado, {"accion": "regresar_conteo"}, follow=True)
        self.assertRedirects(r, self.detalle)
        self.assertContains(r, "vuelve a conteo")
        self.orden.refresh_from_db()
        self.assertIsNone(self.orden.ts_conteo_fin)
        self.contar(1)
        r = self.terminar()
        self.assertContains(r, "Conteo terminado: 1 etiqueta(s) de caja")
        self.assertEqual(self.vivos().filter(impresa_ts__isnull=True).count(), 0)

    def test_reetiquetado_faltaron_sobraron_marcar_y_terminado(self):
        self.contar(4)
        self.contar(2, sku=self.sku_b, linea=self.linea_b)
        self.terminar()
        r = self.client.get(self.reetiquetado)
        self.assertContains(r, "TARIMA REETIQUETADA")
        self.assertContains(r, "por reetiquetar")
        self.assertContains(r, "Regresar a conteo")
        self.assertContains(r, "Terminado · ir al cuadre")
        self.assertContains(r, "disabled")
        # Faltaron 2 de L1: +2 cajas y 2 etiquetas al momento.
        r = self.client.post(self.reetiquetado, {"accion": "faltaron", "tarima_id": self.tarima.pk, "linea_id": self.linea.pk, "cantidad": 2}, follow=True)
        self.assertContains(r, "COLIMITA-SIX lote L1: +2 en TAR-01; 2 etiqueta(s) van a la impresora")
        self.assertEqual(self.vivos().filter(linea=self.linea).count(), 6)
        # Sobraron 1 de B1: −1.
        r = self.client.post(self.reetiquetado, {"accion": "sobraron", "tarima_id": self.tarima.pk, "linea_id": self.linea_b.pk, "cantidad": 1}, follow=True)
        self.assertContains(r, "CC24LCAR lote B1: −1 en TAR-01")
        self.assertEqual(self.vivos().filter(linea=self.linea_b).count(), 1)
        # Faltó un producto que no estaba en la tarima, con lote nuevo escrito a mano.
        extra = SKU.objects.create(cliente=self.cliente, codigo="EXTRA", codigo_barras="7501000000042", descripcion="Extra", requiere_lote=True)
        r = self.client.post(self.reetiquetado, {"accion": "faltaron", "tarima_id": self.tarima.pk, "sku_id": extra.pk, "lote": "X-77", "caducidad": "2027-02-02", "cantidad": 3}, follow=True)
        self.assertContains(r, "EXTRA lote X-77: +3 en TAR-01")
        linea_extra = self.orden.lineas.get(sku=extra)
        self.assertEqual((linea_extra.lote_codigo, linea_extra.cantidad_anunciada, str(linea_extra.fecha_caducidad)), ("X-77", 0, "2027-02-02"))
        # Terminado apagado hasta marcar la tarima.
        r = self.client.post(self.reetiquetado, {"accion": "terminado"}, follow=True)
        self.assertContains(r, "Marca TARIMA REETIQUETADA en todas las tarimas")
        r = self.client.post(self.reetiquetado, {"accion": "reetiquetada", "tarima_id": self.tarima.pk}, follow=True)
        self.assertContains(r, "TAR-01 reetiquetada: 10 caja(s) recibidas")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 6)
        self.assertEqual(Saldo.objects.filter(estado=Saldo.UBICADO_VENDIBLE).count(), 0)
        r = self.client.get(self.reetiquetado)
        self.assertContains(r, "Ya reetiquetada")
        self.assertContains(r, "Todas las tarimas están reetiquetadas")
        r = self.client.post(self.reetiquetado, {"accion": "terminado"})
        self.assertRedirects(r, self.cuadre_url)
        # Un ajuste después la deja pendiente otra vez.
        self.client.post(self.reetiquetado, {"accion": "sobraron", "tarima_id": self.tarima.pk, "linea_id": self.linea.pk, "cantidad": 1})
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 5)
        r = self.client.post(self.reetiquetado, {"accion": "faltaron", "tarima_id": self.tarima.pk, "linea_id": self.linea.pk, "cantidad": 1}, follow=True)
        self.assertContains(r, "por reetiquetar")
        r = self.client.post(self.reetiquetado, {"accion": "reimprimir", "tarima_id": self.tarima.pk}, follow=True)
        self.assertContains(r, "etiqueta(s) y la de la tarima van a la impresora")
        self.assertEqual(self.vivos().filter(impresa_ts__isnull=True).count(), 0)


class CuadreYCierreTests(RecepcionEscaneoBase):
    def setUp(self):
        super().setUp()
        self.en_tarima()
        self.tarima = Ubicacion.objects.get(codigo="TAR-01")
        self.reetiquetado = reverse("piso:recepcion_reetiquetado", args=[self.orden.pk])

    def reetiquetar(self):
        self.client.post(self.detalle, {"accion": "terminar_conteo"})
        self.client.post(self.reetiquetado, {"accion": "reetiquetada", "tarima_id": self.tarima.pk})

    def test_cuadre_muestra_anunciadas_espejo_y_bloquea_sin_confirmar(self):
        self.contar(7)
        self.contar(8, sku=self.sku_b, linea=self.linea_b)
        self.client.post(self.detalle, {"accion": "terminar_conteo"})
        r = self.client.get(self.cuadre_url)
        self.assertContains(r, "Anunciadas")
        self.assertContains(r, "¿Cajas cambiadas?")
        self.assertContains(r, "le faltan 3")
        self.assertContains(r, "Tarimas sin reetiquetar")
        self.assertContains(r, "disabled")
        r = self.client.post(self.cuadre_url, {}, follow=True)
        self.assertContains(r, "sin confirmar en TAR-01")
        self.orden.refresh_from_db()
        self.assertNotEqual(self.orden.estado, OrdenEntrada.CERRADA)

    def test_cerrar_vuelve_vendible_y_guarda_tarimas(self):
        self.contar(10)
        self.contar(5, sku=self.sku_b, linea=self.linea_b)
        self.reetiquetar()
        r = self.client.get(self.cuadre_url)
        self.assertContains(r, "cuadra")
        r = self.client.post(self.cuadre_url, {"tarimas_recibidas": "3"}, follow=True)
        self.assertRedirects(r, reverse("piso:recepciones"))
        self.assertContains(r, "cerrada: todo lo recibido quedó vendible")
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.tarimas_recibidas), (OrdenEntrada.CERRADA, 3))
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).ubicacion, self.tarima)
        self.assertEqual(Saldo.objects.filter(estado=Saldo.EN_PUTAWAY).count(), 0)
        r = self.client.get(self.detalle)
        self.assertContains(r, "Orden cerrada")
        r = self.escanear("7501234567890")
        r = self.client.get(self.detalle)
        self.assertContains(r, "ya está cerrada")

    def test_producto_anunciado_sin_lote_muestra_total_y_lotes_debajo(self):
        LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=8)  # COLIMITA-SIX también sin lote
        r = self.client.get(f"{self.lote_url}?sku={self.sku.pk}")
        self.assertContains(r, 'data-lote="L1"')
        self.assertNotContains(r, 'data-lote="sin lote"')  # requiere lote: la línea sin lote no es tarjeta
        self.client.post(self.lote_url, {"sku_id": self.sku.pk, "nl_codigo": "L7"})
        self.client.post(self.lote_url, {"sku_id": self.sku.pk, "nl_codigo": "L7"})
        self.contar(1)
        r = self.client.get(self.detalle)
        self.assertContains(r, "total del producto")
        self.assertContains(r, "↳ lote")
        r = self.client.get(self.cuadre_url)
        self.assertContains(r, "total del producto")
        fila = next(f for f in r.context["cuadre"]["lineas"] if f["agrupada"])
        self.assertEqual((fila["anunciadas"], fila["contadas"], fila["diferencia"]), (18, 3, -15))

    def test_lista_de_recepciones_muestra_contadas_sin_anunciadas(self):
        self.contar(2)
        r = self.client.get(reverse("piso:recepciones"))
        self.assertContains(r, "contadas <span class=\"num\">2</span>")
        self.assertNotContains(r, "/ 15")
