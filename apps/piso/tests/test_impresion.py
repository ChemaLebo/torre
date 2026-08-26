"""Impresión de etiquetas en la térmica de bodega (lp vía CUPS, sin diálogos).

subprocess.run SIEMPRE va mockeado: las pruebas verifican el comando armado
(cola de TORRE_IMPRESORA_ETIQUETAS + media 100×150) y la traducción de fallas
a mensajes claros para el piso — nunca imprimen de verdad. El PDF sí se genera
completo (reportlab): eso valida Code128, QR y token del repartidor.
"""
import os
from decimal import Decimal
from unittest import mock

from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.models import Paquete
from apps.pedidos.models import Pedido
from apps.rastreo.models import AccesoEtiqueta

from ..etiquetas import MEDIA_LP, generar_pdf_etiqueta, imprimir_etiqueta
from .test_etiqueta import DIRECCION_BASE, EtiquetaTestCase

COLA = "Aiyin_BY480BT"


def _lp(returncode=0, stdout="request id Aiyin_BY480BT-1 (1 file(s))", stderr=""):
    """CompletedProcess simulado de `lp`."""
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _con_cola(valor=COLA):
    return mock.patch.dict(os.environ, {"TORRE_IMPRESORA_ETIQUETAS": valor})


class GenerarPdfEtiquetaTests(EtiquetaTestCase):
    def test_genera_un_pdf_de_verdad(self):
        guia = self.crear_guia()
        pdf = generar_pdf_etiqueta(guia)
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 2048)

    def test_generar_crea_el_token_del_repartidor(self):
        guia = self.crear_guia()
        self.assertFalse(AccesoEtiqueta.objects.filter(guia=guia).exists())
        generar_pdf_etiqueta(guia)
        self.assertTrue(AccesoEtiqueta.objects.filter(guia=guia).exists())

    def test_guia_legacy_sin_paquete_no_truena(self):
        guia = self.crear_guia()  # paquete=None → "Paquete 1 de 1", peso del pedido
        self.assertTrue(generar_pdf_etiqueta(guia).startswith(b"%PDF"))

    def test_guia_con_paquete_no_truena(self):
        pedido = self.crear_pedido(
            estado=Pedido.GUIA_GENERADA, reservar_stock=False, direccion=dict(DIRECCION_BASE),
        )
        paquete = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("7.50"), carrier="paquetexpress",
        )
        guia = self.crear_guia(pedido=pedido, paquete=paquete)
        self.assertTrue(generar_pdf_etiqueta(guia).startswith(b"%PDF"))

    def test_direccion_sin_address2_no_truena(self):
        guia = self.crear_guia(direccion=dict(DIRECCION_BASE))  # sin address2
        self.assertTrue(generar_pdf_etiqueta(guia).startswith(b"%PDF"))

    def test_direccion_minima_no_truena(self):
        guia = self.crear_guia(direccion={})
        self.assertTrue(generar_pdf_etiqueta(guia).startswith(b"%PDF"))


class ImprimirEtiquetaTests(EtiquetaTestCase):
    def test_sin_impresora_configurada_truena_claro(self):
        guia = self.crear_guia()
        with _con_cola(""), mock.patch("apps.piso.etiquetas.subprocess.run") as run:
            with self.assertRaisesMessage(ValueError, "No hay impresora configurada"):
                imprimir_etiqueta(guia)
        run.assert_not_called()

    def test_el_mensaje_de_error_nombra_la_variable_bien_escrita(self):
        guia = self.crear_guia()
        with _con_cola(""):
            with self.assertRaisesMessage(ValueError, "TORRE_IMPRESORA_ETIQUETAS"):
                imprimir_etiqueta(guia)

    def test_lp_recibe_cola_y_media_correctas(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run", return_value=_lp()
        ) as run:
            mensaje = imprimir_etiqueta(guia)
        comando = run.call_args.args[0]
        self.assertEqual(comando[0], "lp")
        self.assertEqual(comando[1:3], ["-d", COLA])
        self.assertEqual(comando[3:5], ["-o", MEDIA_LP])
        self.assertEqual(MEDIA_LP, "media=Custom.100x150mm")
        self.assertTrue(comando[5].endswith(".pdf"))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 15)
        self.assertIn(guia.pedido.folio, mensaje)
        self.assertIn(COLA, mensaje)

    def test_falla_de_lp_truena_con_el_nombre_de_la_cola(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run",
            return_value=_lp(returncode=1, stderr="lp: The printer or class does not exist."),
        ):
            with self.assertRaisesMessage(ValueError, f"La impresora '{COLA}' rechazó el trabajo"):
                imprimir_etiqueta(guia)

    def test_exito_registra_el_evento_impresa_en_bodega(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run", return_value=_lp()
        ):
            imprimir_etiqueta(guia)
        evento = EventoAuditoria.objects.filter(
            entidad="etiqueta", entidad_id=guia.numero, accion="impresa_en_bodega"
        ).latest("id")
        self.assertEqual(evento.delta["folio"], guia.pedido.folio)
        self.assertEqual(evento.delta["cola"], COLA)


class VistaImprimirTests(EtiquetaTestCase):
    def setUp(self):
        self.login_piso()

    def post_imprimir(self, guia, follow=False, **extra):
        datos = {"accion": "imprimir", **extra}
        return self.client.post(reverse("piso:etiqueta", args=[guia.pk]), datos, follow=follow)

    def test_post_imprimir_flashea_exito_y_regresa_a_la_etiqueta(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run", return_value=_lp()
        ):
            respuesta = self.post_imprimir(guia, follow=True)
        self.assertRedirects(respuesta, reverse("piso:etiqueta", args=[guia.pk]))
        contenido = respuesta.content.decode()
        self.assertIn("enviada a la impresora", contenido)
        self.assertIn(COLA, contenido)

    def test_post_con_volver_salida_regresa_a_salida(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run", return_value=_lp()
        ):
            respuesta = self.post_imprimir(guia, volver="salida")
        self.assertRedirects(respuesta, reverse("piso:salida"))

    def test_sin_impresora_flashea_el_error_en_la_etiqueta(self):
        guia = self.crear_guia()
        with _con_cola(""):
            respuesta = self.post_imprimir(guia, follow=True)
        self.assertRedirects(respuesta, reverse("piso:etiqueta", args=[guia.pk]))
        self.assertIn("No hay impresora configurada", respuesta.content.decode())

    def test_portal_no_puede_imprimir(self):
        guia = self.crear_guia()
        self.client.force_login(self.usuario_portal)
        with _con_cola(), mock.patch("apps.piso.etiquetas.subprocess.run") as run:
            respuesta = self.post_imprimir(guia)
        self.assertEqual(respuesta.status_code, 403)
        run.assert_not_called()

    def test_la_etiqueta_muestra_el_boton_imprimir_en_bodega(self):
        guia = self.crear_guia()
        respuesta = self.client.get(reverse("piso:etiqueta", args=[guia.pk]))
        self.assertContains(respuesta, "Imprimir en bodega")
        self.assertContains(respuesta, 'name="accion" value="imprimir"')

    def test_salida_muestra_ambos_botones_de_impresion_por_guia_activa(self):
        guia = self.crear_guia()  # pedido queda GUIA_GENERADA
        respuesta = self.client.get(reverse("piso:salida"))
        self.assertContains(respuesta, "🖨 carrier")
        self.assertContains(respuesta, "🖨 interna")
        self.assertContains(respuesta, 'value="imprimir"')
        self.assertContains(respuesta, 'value="imprimir_interna"')
        self.assertContains(respuesta, f'action="{reverse("piso:etiqueta", args=[guia.pk])}"')
        self.assertContains(respuesta, 'name="volver" value="salida"')
