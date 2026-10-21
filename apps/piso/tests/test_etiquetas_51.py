"""Etiqueta interna 51×51 + etiquetas de manejo (Chema 2026-10-20).

La interna de un pedido foráneo es la 51×51 de la térmica de etiquetas, sin
dirección ni peso, y en el MISMO trabajo salen las FRÁGIL y de orientación
que pida la caja (banderas del SKU) en la cantidad configurada. Los pedidos
locales conservan la 10×15 de la térmica de guías. Salida imprime etiquetas
de manejo sueltas. subprocess.run siempre va mockeado.
"""
import os
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from apps.configuracion.models import EtiquetasManejo
from apps.core.models import EventoAuditoria
from apps.envios.models import Paquete, TrabajoImpresion
from apps.pedidos.models import Pedido

from ..etiquetas import (
    LEYENDA_INTERNA_51, MEDIA_ETIQUETAS, _peso_kg, generar_pdf_etiqueta_51, generar_pdf_manejo, imprimir_etiqueta,
    paginas_pdf,
)
from .test_etiqueta import DIRECCION_BASE, EtiquetaTestCase
from .test_impresion import _lp

COLA_51 = "Etiquetas51"


def _con_cola_51(valor=COLA_51):
    return mock.patch.dict(os.environ, {"TORRE_IMPRESORA_ETIQUETAS_51": valor, "TORRE_IMPRESORA_ETIQUETAS": "Aiyin"})


class PdfEtiqueta51Tests(EtiquetaTestCase):
    def _guia_con_caja(self, peso_real_gr=None):
        pedido = self.crear_pedido(estado=Pedido.GUIA_GENERADA, reservar_stock=False, direccion=dict(DIRECCION_BASE))
        caja = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("14.18"), carrier="estafeta", peso_real_gr=peso_real_gr,
        )
        return self.crear_guia(pedido=pedido, paquete=caja)

    def test_sin_banderas_solo_la_interna(self):
        pdf = generar_pdf_etiqueta_51(self.crear_guia())
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(paginas_pdf(pdf), 1)

    def test_con_banderas_salen_las_de_manejo_en_el_mismo_pdf(self):
        guia = self.crear_guia()
        self.sku.fragil = True
        self.sku.save(update_fields=["fragil"])
        self.assertEqual(paginas_pdf(generar_pdf_etiqueta_51(guia)), 1 + 4)  # default 4 FRÁGIL
        self.sku.este_lado_arriba = True
        self.sku.save(update_fields=["este_lado_arriba"])
        self.assertEqual(paginas_pdf(generar_pdf_etiqueta_51(guia)), 1 + 4 + 3)  # + default 3 de orientación

    def test_las_cantidades_salen_de_configuracion_y_cero_apaga(self):
        guia = self.crear_guia()
        self.sku.fragil = True
        self.sku.este_lado_arriba = True
        self.sku.save(update_fields=["fragil", "este_lado_arriba"])
        fila = EtiquetasManejo.objects.create(fragil=2, orientacion=1)
        self.assertEqual(paginas_pdf(generar_pdf_etiqueta_51(guia)), 1 + 2 + 1)
        fila.fragil = 0
        fila.save()
        self.assertEqual(paginas_pdf(generar_pdf_etiqueta_51(guia)), 1 + 0 + 1)

    def test_manejo_sueltas_y_sus_limites(self):
        self.assertEqual(paginas_pdf(generar_pdf_manejo("fragil", 3)), 3)
        self.assertEqual(paginas_pdf(generar_pdf_manejo("orientacion", "2")), 2)
        with self.assertRaisesMessage(ValueError, "de 1 a 50"):
            generar_pdf_manejo("fragil", 0)
        with self.assertRaisesMessage(ValueError, "desconocido"):
            generar_pdf_manejo("otra", 1)

    def test_el_peso_de_la_etiqueta_es_la_bascula(self):
        """PED-00716: la etiqueta decía el plan (14.18 kg) y la báscula marcó 18.9."""
        self.assertEqual(_peso_kg(self._guia_con_caja(peso_real_gr=18900)), Decimal("18.90"))
        self.assertEqual(_peso_kg(self._guia_con_caja()), Decimal("14.18"))  # sin pesar: el plan


class VistaEtiqueta51Tests(EtiquetaTestCase):
    def setUp(self):
        self.login_piso()

    def test_foraneo_usa_la_51_sin_direccion_ni_peso(self):
        pedido = self.crear_pedido(
            estado=Pedido.GUIA_GENERADA, reservar_stock=False, direccion=dict(DIRECCION_BASE, address2="Depto 4B"),
        )
        caja = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("7.50"), carrier="estafeta", peso_real_gr=8100)
        Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("5.00"), carrier="estafeta")
        guia = self.crear_guia(pedido=pedido, paquete=caja)
        respuesta = self.get_etiqueta(guia)
        self.assertTemplateUsed(respuesta, "piso/etiqueta_51.html")
        self.assertContains(respuesta, "PAQUETE 2 DE 2")
        self.assertContains(respuesta, pedido.folio)
        self.assertContains(respuesta, LEYENDA_INTERNA_51)
        self.assertContains(respuesta, "WOP.fulfillment")
        self.assertContains(respuesta, 'value="imprimir_interna"')
        self.assertNotContains(respuesta, "Av. Prueba 123")
        self.assertNotContains(respuesta, "Depto 4B")
        self.assertNotContains(respuesta, "8.10 kg")
        self.assertNotContains(respuesta, "7.50 kg")

    def test_local_conserva_la_10x15_con_la_bascula(self):
        pedido = self.crear_pedido(estado=Pedido.GUIA_GENERADA, reservar_stock=False, es_local=True, direccion=dict(DIRECCION_BASE))
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("14.18"), carrier="local", peso_real_gr=18900)
        respuesta = self.get_etiqueta(self.crear_guia(pedido=pedido, paquete=caja))
        self.assertTemplateUsed(respuesta, "piso/etiqueta.html")
        self.assertContains(respuesta, "Av. Prueba 123")
        self.assertContains(respuesta, "18.90 kg")
        self.assertNotContains(respuesta, "14.18 kg")

    def test_la_51_avisa_cuantas_de_manejo_salen(self):
        self.sku.fragil = True
        self.sku.save(update_fields=["fragil"])
        respuesta = self.get_etiqueta(self.crear_guia())
        self.assertContains(respuesta, "4 etiquetas FRÁGIL")
        self.assertNotContains(respuesta, "de orientación")


@override_settings(TORRE_MODO_IMPRESION="relay")
class ImpresionInterna51RelayTests(EtiquetaTestCase):
    def test_foraneo_encola_la_51_en_la_termica_de_etiquetas(self):
        guia = self.crear_guia()
        self.sku.fragil = True
        self.sku.save(update_fields=["fragil"])
        with mock.patch("apps.piso.etiquetas.subprocess.run") as run:
            mensaje = imprimir_etiqueta(guia, interna=True)
        run.assert_not_called()
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual(
            (trabajo.tipo, trabajo.impresora, trabajo.media, trabajo.guia, trabajo.paginas),
            (TrabajoImpresion.TIPO_INTERNA, TrabajoImpresion.IMPRESORA_ETIQUETAS, MEDIA_ETIQUETAS, guia, 5),
        )
        self.assertIn("-interna51", trabajo.pdf.name)
        self.assertIn("en cola de impresión", mensaje)
        evento = EventoAuditoria.objects.filter(entidad="etiqueta", entidad_id=guia.numero, accion="encolada_impresion").latest("id")
        self.assertEqual((evento.delta["interna"], evento.delta["formato"], evento.delta["trabajo"]), (True, "51", trabajo.pk))

    def test_local_sigue_en_la_termica_de_guias_a_10x15(self):
        guia = self.crear_guia(es_local=True)
        imprimir_etiqueta(guia, interna=True)
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual(
            (trabajo.tipo, trabajo.impresora, trabajo.media),
            (TrabajoImpresion.TIPO_GUIA, TrabajoImpresion.IMPRESORA_ENVIOS, TrabajoImpresion.MEDIA_GUIAS),
        )

    def test_la_del_carrier_no_cambia(self):
        imprimir_etiqueta(self.crear_guia())
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual((trabajo.tipo, trabajo.impresora), (TrabajoImpresion.TIPO_GUIA, TrabajoImpresion.IMPRESORA_ENVIOS))


class ImpresionInterna51LocalTests(EtiquetaTestCase):
    def test_lp_recibe_la_cola_de_etiquetas_y_media_51(self):
        guia = self.crear_guia()
        with _con_cola_51(), mock.patch("apps.piso.etiquetas.subprocess.run", return_value=_lp()) as run:
            mensaje = imprimir_etiqueta(guia, interna=True)
        comando = run.call_args.args[0]
        self.assertEqual(comando[:5], ["lp", "-d", COLA_51, "-o", f"media={MEDIA_ETIQUETAS}"])
        self.assertIn("impresora de etiquetas", mensaje)
        self.assertEqual(TrabajoImpresion.objects.count(), 0)
        self.assertTrue(EventoAuditoria.objects.filter(entidad="etiqueta", entidad_id=guia.numero, accion="impresa_en_bodega").exists())

    def test_sin_cola_51_el_error_nombra_la_variable(self):
        guia = self.crear_guia()
        with _con_cola_51(""), mock.patch("apps.piso.etiquetas.subprocess.run") as run:
            with self.assertRaisesMessage(ValueError, "TORRE_IMPRESORA_ETIQUETAS_51"):
                imprimir_etiqueta(guia, interna=True)
        run.assert_not_called()


@override_settings(TORRE_MODO_IMPRESION="relay")
class BotonesManejoEnSalidaTests(EtiquetaTestCase):
    def setUp(self):
        self.login_piso()

    def test_salida_ofrece_los_dos_botones(self):
        respuesta = self.client.get(reverse("piso:salida"))
        self.assertContains(respuesta, "Imprimir etiquetas FRÁGIL")
        self.assertContains(respuesta, "Imprimir etiquetas de orientación")
        self.assertContains(respuesta, 'value="imprimir_manejo"')

    def test_imprimir_fragil_encola_la_cantidad_pedida(self):
        respuesta = self.client.post(
            reverse("piso:salida"), {"accion": "imprimir_manejo", "tipo": "fragil", "cantidad": "3"}, follow=True,
        )
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual(
            (trabajo.tipo, trabajo.impresora, trabajo.media, trabajo.paginas, trabajo.referencia, trabajo.cliente),
            (TrabajoImpresion.TIPO_MANEJO, TrabajoImpresion.IMPRESORA_ETIQUETAS, MEDIA_ETIQUETAS, 3, "manejo fragil x3", None),
        )
        self.assertContains(respuesta, "3 etiqueta(s) FRÁGIL en cola de impresión")

    def test_cantidad_o_tipo_invalidos_avisan_sin_encolar(self):
        respuesta = self.client.post(reverse("piso:salida"), {"accion": "imprimir_manejo", "tipo": "orientacion", "cantidad": "99"}, follow=True)
        self.assertContains(respuesta, "de 1 a 50")
        respuesta = self.client.post(reverse("piso:salida"), {"accion": "imprimir_manejo", "tipo": "x", "cantidad": "1"}, follow=True)
        self.assertContains(respuesta, "desconocido")
        self.assertEqual(TrabajoImpresion.objects.count(), 0)
