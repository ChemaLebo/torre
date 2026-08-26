"""Modo relay de imprimir_etiqueta (TORRE_MODO_IMPRESION=relay).

Con Torre en un VPS la térmica queda inalcanzable: la etiqueta se encola en
envios.TrabajoImpresion (PDF completo de reportlab guardado en MEDIA) y el
agente de bodega la imprime. En relay subprocess JAMÁS se toca; el modo local
(default) sigue intacto — sus pruebas viven en test_impresion.py.
"""
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.models import TrabajoImpresion

from ..etiquetas import imprimir_etiqueta
from .test_etiqueta import EtiquetaTestCase
from .test_impresion import _con_cola, _lp


@override_settings(TORRE_MODO_IMPRESION="relay")
class ImprimirEtiquetaRelayTests(EtiquetaTestCase):
    def test_relay_encola_y_jamas_llama_lp(self):
        guia = self.crear_guia()
        with mock.patch("apps.piso.etiquetas.subprocess.run") as run:
            mensaje = imprimir_etiqueta(guia)
        run.assert_not_called()
        trabajo = TrabajoImpresion.objects.get()
        self.assertEqual(trabajo.guia, guia)
        self.assertEqual(trabajo.estado, TrabajoImpresion.PENDIENTE)
        self.assertEqual(trabajo.intentos, 0)
        self.assertIn("en cola de impresión", mensaje)
        self.assertIn(guia.pedido.folio, mensaje)
        self.assertIn("bodega", mensaje)

    def test_el_pdf_queda_guardado_y_es_un_pdf_de_verdad(self):
        guia = self.crear_guia()
        imprimir_etiqueta(guia)
        trabajo = TrabajoImpresion.objects.get()
        self.assertTrue(trabajo.pdf.name.startswith("etiquetas/"))
        with trabajo.pdf.open("rb") as archivo:
            contenido = archivo.read()
        self.assertTrue(contenido.startswith(b"%PDF"))
        self.assertGreater(len(contenido), 2048)

    def test_relay_no_necesita_impresora_en_el_servidor(self):
        guia = self.crear_guia()
        with _con_cola(""):  # sin TORRE_IMPRESORA_ETIQUETAS configurada NO truena
            mensaje = imprimir_etiqueta(guia)
        self.assertIn("en cola de impresión", mensaje)

    def test_relay_registra_evento_encolada_impresion(self):
        guia = self.crear_guia()
        imprimir_etiqueta(guia)
        trabajo = TrabajoImpresion.objects.get()
        evento = EventoAuditoria.objects.filter(
            entidad="etiqueta", entidad_id=guia.numero, accion="encolada_impresion"
        ).latest("id")
        self.assertEqual(evento.delta["folio"], guia.pedido.folio)
        self.assertEqual(evento.delta["trabajo"], trabajo.pk)

    def test_interna_encola_la_dibujada_jamas_la_del_carrier(self):
        guia = self.crear_guia()
        with mock.patch("apps.piso.etiquetas._pdf_etiqueta") as carrier, \
             mock.patch(
                 "apps.piso.etiquetas.generar_pdf_etiqueta", return_value=b"%PDF-interna"
             ) as dibujada:
            mensaje = imprimir_etiqueta(guia, interna=True)
        carrier.assert_not_called()
        dibujada.assert_called_once_with(guia)
        trabajo = TrabajoImpresion.objects.get()
        self.assertIn("-interna", trabajo.pdf.name)
        self.assertIn("interna", mensaje)
        evento = EventoAuditoria.objects.filter(accion="encolada_impresion").latest("id")
        self.assertTrue(evento.delta["interna"])

    def test_default_sigue_siendo_la_del_carrier(self):
        """Decisión 2026-08-10 intacta: sin interna=True, la oficial del carrier."""
        guia = self.crear_guia()
        with mock.patch(
            "apps.piso.etiquetas._pdf_etiqueta", return_value=b"%PDF-carrier"
        ) as carrier, \
             mock.patch("apps.piso.etiquetas.generar_pdf_etiqueta") as dibujada:
            imprimir_etiqueta(guia)
        carrier.assert_called_once_with(guia)
        dibujada.assert_not_called()

    def test_boton_imprimir_interna_desde_salida(self):
        guia = self.crear_guia()
        self.login_piso()
        with mock.patch(
            "apps.piso.etiquetas.imprimir_etiqueta", return_value="interna en cola"
        ) as imprimir:
            respuesta = self.client.post(
                reverse("piso:etiqueta", args=[guia.pk]),
                {"accion": "imprimir_interna", "volver": "salida"},
            )
        imprimir.assert_called_once_with(guia, interna=True)
        self.assertRedirects(respuesta, reverse("piso:salida"), fetch_redirect_response=False)
        # En relay nada se imprimió aún: ese evento lo gana el trabajo al confirmarse.
        self.assertFalse(
            EventoAuditoria.objects.filter(accion="impresa_en_bodega").exists()
        )

    def test_cada_impresion_encola_su_propio_trabajo(self):
        guia = self.crear_guia()
        imprimir_etiqueta(guia)
        imprimir_etiqueta(guia)  # reimpresión desde Salida = trabajo nuevo
        self.assertEqual(TrabajoImpresion.objects.filter(guia=guia).count(), 2)

    def test_la_vista_del_piso_flashea_el_mensaje_de_cola(self):
        guia = self.crear_guia()
        self.login_piso()
        with mock.patch("apps.piso.etiquetas.subprocess.run") as run:
            respuesta = self.client.post(
                reverse("piso:etiqueta", args=[guia.pk]), {"accion": "imprimir"}, follow=True,
            )
        run.assert_not_called()
        self.assertIn("en cola de impresión", respuesta.content.decode())
        self.assertEqual(TrabajoImpresion.objects.count(), 1)


class ModoLocalIntactoTests(EtiquetaTestCase):
    """Sin TORRE_MODO_IMPRESION (default "local") todo sigue igual: lp directo."""

    def test_default_local_llama_lp_y_no_encola(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run", return_value=_lp()
        ) as run:
            mensaje = imprimir_etiqueta(guia)
        run.assert_called_once()
        self.assertEqual(TrabajoImpresion.objects.count(), 0)
        self.assertIn("enviada a la impresora", mensaje)

    @override_settings(TORRE_MODO_IMPRESION="local")
    def test_local_explicito_tambien_imprime_directo(self):
        guia = self.crear_guia()
        with _con_cola(), mock.patch(
            "apps.piso.etiquetas.subprocess.run", return_value=_lp()
        ) as run:
            imprimir_etiqueta(guia)
        run.assert_called_once()
        self.assertEqual(TrabajoImpresion.objects.count(), 0)
