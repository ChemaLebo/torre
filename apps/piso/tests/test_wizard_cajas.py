"""Wizard de empaque POR CAJA (C2): services reales, impresión mockeada.

Foto de contenido + peso por caja (empacar_caja), la última encadena empaque
total + guías + impresión (despachar_a_corral), luego "Etiquetas pegadas",
UNA foto de evidencia de la tanda (cerrar_tanda) y "Lista en salida" de
vuelta a Mi turno (Chema 2026-10-20).
"""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EvidenciaFoto
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos.models import Pedido

from .base import PisoTestCase


# La lista blanca de producción cambia con el negocio (2026-09-20: solo imile);
# estas pruebas ejercitan el mecanismo con los carriers que conoce la tabla mock.
TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


@override_settings(TORRE=TORRE_CARRIERS_CLASICOS)
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
        self.assertContains(respuesta, "1 de 2 en esta tanda")
        self.assertContains(respuesta, 'value="empacar_caja"')
        # Rango de ESA caja (12 kg ± 3%) renderizado para validar client-side
        # ANTES de subir nada.
        self.assertContains(respuesta, 'data-min="11640"')
        self.assertContains(respuesta, 'data-max="12360"')
        # Contenido de ESA caja (texto_para_piso).
        self.assertContains(respuesta, "6 ×")

    def test_sin_caja_del_catalogo_prellena_las_medidas_del_producto(self):
        self.sku.largo_cm, self.sku.ancho_cm, self.sku.alto_cm = 26, 20, 25
        self.sku.save()
        respuesta = self.client.get(self.url)
        # Caja 1 lleva 6 piezas: largo y ancho del producto, altos apilados.
        self.assertContains(respuesta, 'data-l="26" data-a="20" data-h="150" data-t="0"')
        self.assertContains(respuesta, 'id="dim-largo" min="1" value="26"')
        self.assertContains(respuesta, 'id="dim-alto" min="1" value="150"')
        # Sin medidas del producto: la opción no prellena y quedan las del plan.
        self.sku.largo_cm = 0
        self.sku.save()
        respuesta = self.client.get(self.url)
        self.assertNotContains(respuesta, 'data-t="0"')
        self.assertContains(respuesta, 'id="dim-largo" min="1" value="40"')

    def test_si_una_caja_falla_con_el_carrier_la_otra_se_imprime_y_se_reintenta_sin_det(self):
        from apps.envios.adapters import ErrorCarrier
        from apps.incidencias.models import Incidencia

        self.client.post(self.url, {
            "accion": "empacar_caja", "paquete_id": self.caja1.pk,
            "peso_real_gr": "12100", "foto_contenido": self.foto("c1.jpg"),
        })
        original = MockAdapter.generar

        def generar(self_, pedido, carrier, servicio, paquete=None):
            if paquete is not None and paquete.numero == 1:
                raise ErrorCarrier("envia.com regresó error: 1300")
            return original(self_, pedido, carrier, servicio, paquete=paquete)

        with patch.object(MockAdapter, "generar", autospec=True, side_effect=generar), \
             patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)") as imprimir:
            respuesta = self.client.post(self.url, {
                "accion": "empacar_caja", "paquete_id": self.caja2.pk,
                "peso_real_gr": "8100", "foto_contenido": self.foto("c2.jpg"),
            }, follow=True)
        self.assertEqual(Guia.objects.filter(pedido=self.pedido).count(), 1)  # solo la caja 2
        self.assertEqual(imprimir.call_count, 2)  # carrier + interna de la que sí salió
        self.assertContains(respuesta, "El carrier no respondió")
        self.assertContains(respuesta, "que sí tienen guía ya se mandaron a imprimir")
        # Sin DET automática (Chema 2026-10-20): el pedido sigue con su dueño en la pantalla
        # Etiquetas con "Reintentar guía"; "Etiquetas pegadas" espera la guía; Detener es manual.
        self.pedido.refresh_from_db()
        self.assertEqual((self.pedido.estado, self.pedido.detenido, self.pedido.asignado_a), (Pedido.EMPACADO, False, self.operador))
        self.assertFalse(Incidencia.objects.filter(pedido=self.pedido, tipo=Incidencia.TIPO_DET).exists())
        self.assertContains(respuesta, "Sin guía: caja 1")
        self.assertContains(respuesta, 'value="generar_guia"')
        self.assertContains(respuesta, "Reintentar guía · caja 1")
        self.assertContains(respuesta, "reintenta la guía primero")
        self.assertNotContains(self.client.get(reverse("piso:salida")), self.pedido.folio)
        self.assertContains(self.client.get(reverse("piso:home")), "sin guía: caja 1")

        # Reintentar guía desde Etiquetas: ahora el carrier responde y sale la que faltaba.
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            respuesta = self.client.post(self.url, {"accion": "generar_guia"}, follow=True)
        self.assertContains(respuesta, "listas para")
        self.assertEqual(Guia.objects.filter(pedido=self.pedido).count(), 2)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertNotContains(respuesta, 'value="generar_guia"')
        self.assertContains(respuesta, 'value="etiquetas_pegadas"')

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

    def _empacar_las_dos(self):
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            for caja, peso in ((self.caja1, "12100"), (self.caja2, "8100")):
                self.client.post(self.url, {
                    "accion": "empacar_caja", "paquete_id": caja.pk,
                    "peso_real_gr": peso, "foto_contenido": self.foto(),
                })

    def test_flujo_completo_caja_por_caja_hasta_lista_en_salida(self):
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
        self.assertContains(r1, "2 de 2 en esta tanda")  # auto-siguiente

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
        # Pantalla Etiquetas (Chema 2026-10-20): guías impresas, reimpresión por caja y "Etiquetas pegadas".
        self.assertContains(r2, "etiquetas enviadas a la impresora")
        self.assertContains(r2, 'value="etiquetas_pegadas"')
        self.assertContains(r2, 'value="imprimir_guia"')
        self.assertNotContains(r2, 'value="cerrar_caja"')
        self.assertNotContains(self.client.get(reverse("piso:salida")), self.pedido.folio)  # nada en Salida todavía

        # Etiquetas pegadas → pantalla Evidencia: UNA foto de la tanda.
        r3 = self.client.post(self.url, {"accion": "etiquetas_pegadas"}, follow=True)
        self.assertContains(r3, "Etiquetas pegadas en la caja 1, 2")
        self.assertContains(r3, 'value="cerrar_tanda"')
        self.assertContains(r3, 'name="foto_tanda"')
        self.caja1.refresh_from_db()
        self.assertIsNotNone(self.caja1.ts_etiquetas)

        # Evidencia → pantalla final "Déjalo en el corral" con "Lista en salida".
        r4 = self.client.post(self.url, {"accion": "cerrar_tanda", "foto_tanda": self.foto("tanda.jpg")}, follow=True)
        self.assertContains(r4, "Déjalo en el corral")
        self.assertContains(r4, "Lista en salida")
        self.assertContains(r4, 'value="lista_salida"')
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.cajas_cerradas_completas)
        self.assertFalse(self.pedido.empaque_completo)  # sin "Lista en salida" no entra a Salida
        fotos = EvidenciaFoto.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk))
        self.assertEqual(fotos.filter(tipo="contenido").count(), 2)
        self.assertEqual(fotos.filter(tipo="caja_cerrada").count(), 1)  # una foto para las dos cajas
        self.caja1.refresh_from_db()
        self.caja2.refresh_from_db()
        self.assertEqual(self.caja1.foto_cierre_id, self.caja2.foto_cierre_id)
        self.assertNotContains(self.client.get(reverse("piso:salida")), self.pedido.folio)

        # Lista en salida → de vuelta a Mi turno (nunca al siguiente pedido), ya en el corral.
        r5 = self.client.post(self.url, {"accion": "lista_salida"})
        self.assertRedirects(r5, reverse("piso:home"), fetch_redirect_response=False)
        self.pedido.refresh_from_db()
        self.caja1.refresh_from_db()
        self.assertTrue(self.pedido.empaque_completo)
        self.assertIsNotNone(self.caja1.ts_listo_salida)
        home = self.client.get(reverse("piso:home"))
        self.assertContains(home, "lista en salida")
        self.assertContains(home, "En salida")
        self.assertContains(self.client.get(reverse("piso:salida")), self.pedido.folio)
        otra_vez = self.client.get(self.url)
        self.assertContains(otra_vez, "en salida")
        self.assertContains(otra_vez, "Ir a Mi turno")
        self.assertContains(otra_vez, "Caja 1 · lista")
        self.assertNotContains(otra_vez, "SIGUIENTE PEDIDO")

    def test_con_una_caja_ya_fuera_la_que_queda_hace_su_propia_tanda(self):
        from apps.pedidos.services import cerrar_caja, marcar_recolectado

        self._empacar_las_dos()
        cerrar_caja(self.caja1, self.operador, self.foto("z1.jpg"))  # cierre de golpe: ya lista en salida
        self.pedido.refresh_from_db()
        marcar_recolectado(self.pedido, self.operador, paquetes=[self.caja1])
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.PARCIALMENTE_DESPACHADO)

        # La caja 2 sigue en bodega: pantalla Etiquetas solo con ella.
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'value="etiquetas_pegadas"')
        self.assertContains(respuesta, "1 caja en esta tanda")
        self.assertNotContains(respuesta, "no se puede empacar")
        self.client.post(self.url, {"accion": "etiquetas_pegadas"})
        self.client.post(self.url, {"accion": "cerrar_tanda", "foto_tanda": self.foto("z2.jpg")})
        respuesta = self.client.post(self.url, {"accion": "lista_salida"})
        self.assertRedirects(respuesta, reverse("piso:home"), fetch_redirect_response=False)
        self.caja2.refresh_from_db()
        self.assertIsNotNone(self.caja2.ts_cierre)
        self.assertIsNotNone(self.caja2.ts_listo_salida)
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.cajas_cerradas_completas)
        self.assertTrue(self.pedido.empaque_completo)

    def test_la_evidencia_de_la_tanda_no_se_toma_dos_veces(self):
        self._empacar_las_dos()
        self.client.post(self.url, {"accion": "etiquetas_pegadas"})
        self.client.post(self.url, {"accion": "cerrar_tanda", "foto_tanda": self.foto("z1.jpg")})
        repetida = self.client.post(self.url, {"accion": "cerrar_tanda", "foto_tanda": self.foto("z1-bis.jpg")}, follow=True)
        self.assertContains(repetida, "no tiene cajas esperando su foto")
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk), tipo="caja_cerrada",
            ).count(),
            1,
        )
        # Y sin guía en alguna caja, "Etiquetas pegadas" no se puede confirmar.
        Guia.objects.filter(paquete=self.caja2).update(estado=Guia.CANCELADA)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Sin guía: caja 2")
        self.assertContains(respuesta, "reintenta la guía primero")

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
        # BEST-EFFORT: las guías quedan vivas y la tanda sigue a Etiquetas pegadas.
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertContains(respuesta, 'value="etiquetas_pegadas"')
