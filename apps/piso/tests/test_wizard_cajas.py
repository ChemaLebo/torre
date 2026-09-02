"""Wizard de empaque POR CAJA (C2): services reales, impresión mockeada.

Foto de contenido + peso por caja (empacar_caja), la última encadena empaque
total + guías + impresión (despachar_a_corral), luego foto de CAJA CERRADA
por caja (cerrar_caja) y pantalla de éxito con SIGUIENTE PEDIDO ▶.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

from apps.core.models import EvidenciaFoto
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos.models import Pedido

from .base import PisoTestCase


class WizardCajasTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=10)  # 20 kg → plan de 2 cajas
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        linea = self.pedido.lineas.get()
        confirmar_linea_pick(linea, 10, self.operador)
        self.caja1 = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("12.00"), carrier="paquetexpress",
        )
        self.caja2 = Paquete.objects.create(
            pedido=self.pedido, numero=2, peso_kg=Decimal("8.00"), carrier="paquetexpress",
        )
        PaqueteLinea.objects.create(paquete=self.caja1, linea_pedido=linea, cantidad=6)
        PaqueteLinea.objects.create(paquete=self.caja2, linea_pedido=linea, cantidad=4)
        self.url = reverse("piso:empaque_pedido", args=[self.pedido.pk])

    def test_wizard_muestra_caja_1_de_2_con_su_rango_de_peso(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Caja 1 de 2")
        self.assertContains(respuesta, 'value="empacar_caja"')
        # Rango de ESA caja (12 kg ± 3%) renderizado para validar client-side
        # ANTES de subir nada.
        self.assertContains(respuesta, 'data-min="11640"')
        self.assertContains(respuesta, 'data-max="12360"')
        # Contenido de ESA caja (texto_para_piso).
        self.assertContains(respuesta, "6 ×")

    @override_settings(TORRE_PESO_MODO="bloquear")
    def test_peso_fuera_de_rango_no_toca_nada(self):
        respuesta = self.client.post(self.url, {
            "accion": "empacar_caja", "paquete_id": self.caja1.pk,
            "peso_real_gr": "20000", "foto_contenido": self.foto("c1.jpg"),
        }, follow=True)
        self.assertContains(respuesta, "no cuadra")
        self.caja1.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual(self.caja1.estado, Paquete.PLANEADO)
        self.assertIsNone(self.caja1.peso_real_gr)
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk),
            ).count(),
            0,
        )

    def test_flujo_completo_caja_por_caja_hasta_exito(self):
        # Caja 1: foto + peso contra SU plan; el pedido sigue en picking.
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            r1 = self.client.post(self.url, {
                "accion": "empacar_caja", "paquete_id": self.caja1.pk,
                "peso_real_gr": "12100", "foto_contenido": self.foto("c1.jpg"),
            }, follow=True)
        self.caja1.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual(self.caja1.estado, Paquete.EMPACADO)
        self.assertEqual(self.caja1.peso_real_gr, 12100)
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertContains(r1, "Caja 2 de 2")  # auto-siguiente

        # Caja 2 (última): encadena empaque total + guías + impresión.
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)",
        ) as imprimir:
            r2 = self.client.post(self.url, {
                "accion": "empacar_caja", "paquete_id": self.caja2.pk,
                "peso_real_gr": "8100", "foto_contenido": self.foto("c2.jpg"),
            }, follow=True)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertEqual(self.pedido.peso_real_gr, 12100 + 8100)
        guias = list(Guia.objects.filter(pedido=self.pedido))
        self.assertEqual(len(guias), 2)  # una guía por caja
        self.assertEqual(imprimir.call_count, 4)  # (carrier + interna) × 2 cajas
        # Paso de cierre: etiquetas impresas + foto de caja cerrada por caja.
        self.assertContains(r2, "Etiquetas imprimiéndose")
        self.assertContains(r2, 'value="cerrar_caja"')

        # Cierre caja 1 → sigue la 2 (auto-siguiente).
        r3 = self.client.post(self.url, {
            "accion": "cerrar_caja", "paquete_id": self.caja1.pk,
            "foto_cierre": self.foto("z1.jpg"),
        }, follow=True)
        self.assertFalse(self.pedido.cajas_cerradas_completas)
        self.assertContains(r3, f'value="{self.caja2.pk}"')

        # Cierre caja 2 → evidencia completa + pantalla de éxito.
        r4 = self.client.post(self.url, {
            "accion": "cerrar_caja", "paquete_id": self.caja2.pk,
            "foto_cierre": self.foto("z2.jpg"),
        }, follow=True)
        self.assertTrue(self.pedido.cajas_cerradas_completas)
        self.assertContains(r4, "SIGUIENTE PEDIDO")
        fotos = EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk),
        )
        self.assertEqual(fotos.filter(tipo="contenido").count(), 2)
        self.assertEqual(fotos.filter(tipo="caja_cerrada").count(), 2)

    def test_una_caja_no_se_cierra_dos_veces(self):
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            for caja, peso in ((self.caja1, "12100"), (self.caja2, "8100")):
                self.client.post(self.url, {
                    "accion": "empacar_caja", "paquete_id": caja.pk,
                    "peso_real_gr": peso, "foto_contenido": self.foto(),
                })
        self.client.post(self.url, {
            "accion": "cerrar_caja", "paquete_id": self.caja1.pk,
            "foto_cierre": self.foto("z1.jpg"),
        })
        repetida = self.client.post(self.url, {
            "accion": "cerrar_caja", "paquete_id": self.caja1.pk,
            "foto_cierre": self.foto("z1-bis.jpg"),
        }, follow=True)
        self.assertContains(repetida, "ya tiene su foto de cierre")
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk), tipo="caja_cerrada",
            ).count(),
            1,
        )

    def test_cierre_legacy_rechazado_con_cajas_empacadas(self):
        # Con plan de cajas empacado, el cierre es POR CAJA: el POST
        # cerrar_legacy (cierre único) no debe inflar la evidencia.
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            for caja, peso in ((self.caja1, "12100"), (self.caja2, "8100")):
                self.client.post(self.url, {
                    "accion": "empacar_caja", "paquete_id": caja.pk,
                    "peso_real_gr": peso, "foto_contenido": self.foto(),
                })
        respuesta = self.client.post(self.url, {
            "accion": "cerrar_legacy", "foto_cierre": self.foto("unica.jpg"),
        }, follow=True)
        self.assertContains(respuesta, "se empacó por caja")
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk), tipo="caja_cerrada",
            ).count(),
            0,
        )
        self.pedido.refresh_from_db()
        self.assertFalse(self.pedido.cajas_cerradas_completas)

    def test_post_fallido_conserva_el_peso_y_pide_la_foto_de_nuevo(self):
        # El navegador tira el file input en el PRG: el re-render debe traer
        # el peso capturado y el aviso de volver a tomar la foto.
        respuesta = self.client.post(self.url, {
            "accion": "empacar_caja", "paquete_id": self.caja1.pk,
            "peso_real_gr": "12100",  # sin foto → ValueError del servicio
        }, follow=True)
        self.assertContains(respuesta, 'value="12100"')
        self.assertContains(respuesta, "vuelve a tomarla")

    def test_error_de_impresora_avisa_pero_el_flujo_continua(self):
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta",
            side_effect=ValueError("No hay impresora configurada."),
        ):
            self.client.post(self.url, {
                "accion": "empacar_caja", "paquete_id": self.caja1.pk,
                "peso_real_gr": "12100", "foto_contenido": self.foto("c1.jpg"),
            })
            respuesta = self.client.post(self.url, {
                "accion": "empacar_caja", "paquete_id": self.caja2.pk,
                "peso_real_gr": "8100", "foto_contenido": self.foto("c2.jpg"),
            }, follow=True)
        self.assertContains(respuesta, "No se imprimió la etiqueta")
        self.pedido.refresh_from_db()
        # BEST-EFFORT: las guías quedan vivas y el cierre sigue disponible.
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertContains(respuesta, 'value="cerrar_caja"')
