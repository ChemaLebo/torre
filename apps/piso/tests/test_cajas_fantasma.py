"""Cajas fantasma y cajas ya en salida (Chema 2026-10-21, PED-00341).

Una caja fantasma es un registro que sigue PLANEADO/EMPACADO aunque su guía
ya salió o se entregó (empaque entero anterior al 2026-10-15). El wizard, la
tanda, la impresión y Salida solo trabajan lo que sigue en la mesa; al reabrir
el pedido por reposición la fantasma se cierra sola como despachada; y una
caja ya confirmada en salida no vuelve a aparecer en la pantalla de Etiquetas.
"""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import EventoAuditoria
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.pedidos import services
from apps.pedidos.models import Pedido

from .base import PisoTestCase

TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


@override_settings(TORRE=TORRE_CARRIERS_CLASICOS)
class CajasFantasmaTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        MockAdapter.reiniciar()
        self.crear_stock(cantidad=50)

    def _entregado_con_caja_fantasma(self):
        """Pedido entregado del camino viejo: su caja 1 se quedó PLANEADO con la guía entregada."""
        pedido = self.dejar_empacado(self.crear_pedido(cantidad=2))
        linea = pedido.lineas.get()
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta")
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=2)
        services.generar_guia(pedido)
        pedido.refresh_from_db()
        with patch("apps.mensajeria.services.enviar_en_camino"):
            services.marcar_recolectado(pedido, self.operador)
        # Desde el 2026-10-15 la salida del pedido entero cierra la caja; el dato viejo la dejó planeada.
        Paquete.objects.filter(pk=caja.pk).update(estado=Paquete.PLANEADO)
        guia = Guia.objects.get(paquete=caja)
        Guia.objects.filter(pk=guia.pk).update(estado=Guia.ENTREGADO, ts_recolectado_carrier=timezone.now())
        pedido.refresh_from_db()
        pedido.transicionar(Pedido.ENTREGADO)
        caja.refresh_from_db()
        guia.refresh_from_db()
        linea.refresh_from_db()
        self.assertEqual((pedido.estado, caja.estado, linea.cantidad_despachada), (Pedido.ENTREGADO, Paquete.PLANEADO, 2))
        self.assertTrue(caja.ya_salio)
        return pedido, caja, guia

    def _flujo_tanda(self, url, nombre):
        self.client.post(url, {"accion": "etiquetas_pegadas"})
        self.client.post(url, {"accion": "cerrar_tanda", "foto_tanda": self.foto(nombre)})
        return self.client.post(url, {"accion": "lista_salida"})

    def test_al_reponer_la_fantasma_se_cierra_y_el_piso_solo_trabaja_la_caja_nueva(self):
        pedido, caja1, guia1 = self._entregado_con_caja_fantasma()
        linea = pedido.lineas.get()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(linea, 1, caja1)], self.operador)
        pedido.refresh_from_db()
        caja1.refresh_from_db()
        self.assertEqual((pedido.estado, caja1.estado), (Pedido.PENDIENTE, Paquete.DESPACHADO))
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="caja_fantasma_cerrada")
        self.assertEqual((evento.delta["caja"], evento.delta["guia"]), (1, guia1.numero))
        caja2 = pedido.paquetes.get(numero=2)
        self.assertEqual(caja2.estado, Paquete.PLANEADO)

        # Picking pide solo la pieza repuesta; el wizard solo la caja 2.
        services.iniciar_picking(pedido, self.operador)
        linea.refresh_from_db()
        self.assertEqual(linea.por_pickear, 1)
        services.confirmar_linea_pick(linea, 1, self.operador)
        url = reverse("piso:empaque_pedido", args=[pedido.pk])
        respuesta = self.client.get(url)
        self.assertEqual((respuesta.context["paso"], respuesta.context["caja"].numero), ("caja", 2))
        self.assertContains(respuesta, "Caja 1 · despachada")
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)") as imprimir:
            respuesta = self.client.post(url, {
                "accion": "empacar_caja", "paquete_id": caja2.pk,
                "peso_real_gr": str(int(caja2.peso_kg * 1000)), "foto_contenido": self.foto("c2.jpg"),
            }, follow=True)
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.GUIA_GENERADA)
        self.assertEqual(Guia.objects.filter(pedido=pedido).count(), 2)  # la entregada de la caja 1 + la nueva de la 2
        self.assertEqual(imprimir.call_count, 2)  # solo la caja 2: carrier + interna
        self.assertEqual(respuesta.context["paso"], "etiquetas")
        self.assertEqual([c.numero for c in respuesta.context["tanda"]], [2])
        self.assertContains(respuesta, "1 caja en esta tanda")
        self.assertNotContains(respuesta, f'name="paquete_id" value="{caja1.pk}"')
        self._flujo_tanda(url, "tanda.jpg")
        caja1.refresh_from_db()
        caja2.refresh_from_db()
        self.assertIsNone(caja1.ts_etiquetas)  # la fantasma no se tocó
        self.assertIsNotNone(caja2.ts_listo_salida)
        salida = self.client.get(reverse("piso:salida"))
        self.assertContains(salida, pedido.folio)
        self.assertContains(salida, "ya salió: caja 1")
        self.assertNotContains(salida, guia1.numero)

    def test_una_fantasma_sin_cerrar_tampoco_se_pide_ni_se_imprime_ni_sale(self):
        """Dato viejo sin pasar por la reposición: el candado del wizard basta."""
        pedido, caja1, guia1 = self._entregado_con_caja_fantasma()
        linea = pedido.lineas.get()
        from apps.inventario.services import reservar

        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EN_PICKING, asignado_a=self.operador)
        self.assertTrue(reservar(self.sku, 1, pedido.folio))  # la pieza repuesta sí está apartada en el kardex
        linea.cantidad_repuesta, linea.cantidad_repuesta_reservada, linea.cantidad_pickeada = 1, 1, 3
        linea.save(update_fields=["cantidad_repuesta", "cantidad_repuesta_reservada", "cantidad_pickeada"])
        caja2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("2"), carrier="estafeta")
        PaqueteLinea.objects.create(paquete=caja2, linea_pedido=linea, cantidad=1, repone_a=caja1)
        pedido.refresh_from_db()
        from apps.piso.views import _que_falta_empaque
        self.assertEqual(_que_falta_empaque(pedido), "por empacar")  # la fantasma no cuenta como caja hecha ni pendiente
        url = reverse("piso:empaque_pedido", args=[pedido.pk])
        respuesta = self.client.get(url)
        self.assertEqual((respuesta.context["paso"], respuesta.context["caja"].numero), ("caja", 2))
        self.assertContains(respuesta, "Caja 1 · despachada")
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)") as imprimir:
            respuesta = self.client.post(url, {
                "accion": "empacar_caja", "paquete_id": caja2.pk, "peso_real_gr": "2000", "foto_contenido": self.foto("c2.jpg"),
            }, follow=True)
        pedido.refresh_from_db()
        caja1.refresh_from_db()

        self.assertEqual((pedido.estado, caja1.estado), (Pedido.GUIA_GENERADA, Paquete.PLANEADO))  # la fantasma cuenta como hecha
        self.assertEqual(Guia.objects.filter(pedido=pedido, paquete=caja2).count(), 1)
        self.assertEqual(imprimir.call_count, 2)
        self.assertEqual([c.numero for c in respuesta.context["tanda"]], [2])
        self._flujo_tanda(url, "tanda.jpg")
        pedido.refresh_from_db()
        self.assertTrue(pedido.empaque_completo)
        salida = self.client.get(reverse("piso:salida"))
        self.assertContains(salida, pedido.folio)
        self.assertNotContains(salida, guia1.numero)
        registrar = reverse("piso:salida_registrar") + "?corral=SAL-OTRO&carrier=estafeta"
        respuesta = self.client.post(registrar, {"accion": "escanear", "codigo": guia1.numero}, follow=True)
        self.assertContains(respuesta, "ya salió en otro manifiesto")

    def test_las_cajas_ya_en_salida_no_vuelven_a_la_pantalla_de_etiquetas(self):
        """Replaneo con las primeras cajas ya en el corral: la tanda nueva es solo la caja 3."""
        pedido = self.crear_pedido(cantidad=10)
        services.iniciar_picking(pedido, self.operador)
        linea = pedido.lineas.get()
        services.confirmar_linea_pick(linea, 10, self.operador)
        c1 = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("12"), carrier="paquetexpress")
        c2 = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("8"), carrier="paquetexpress")
        PaqueteLinea.objects.create(paquete=c1, linea_pedido=linea, cantidad=6)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=4)
        url = reverse("piso:empaque_pedido", args=[pedido.pk])
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            for caja, peso in ((c1, "12100"), (c2, "8100")):
                self.client.post(url, {"accion": "empacar_caja", "paquete_id": caja.pk, "peso_real_gr": peso, "foto_contenido": self.foto()})
        self._flujo_tanda(url, "primera.jpg")
        c1.refresh_from_db()
        self.assertIsNotNone(c1.ts_listo_salida)
        # Mesa replanea: nace la caja 3, el piso la empaca y compra su guía (simulado: ya EMPACADO con guía).
        c3 = Paquete.objects.create(pedido=pedido, numero=3, peso_kg=Decimal("2"), carrier="paquetexpress", estado=Paquete.EMPACADO, peso_real_gr=2000)
        Guia.objects.create(pedido=pedido, paquete=c3, carrier="paquetexpress", numero="MOCK-C3", estado=Guia.GUIA_CREADA)
        Pedido.objects.filter(pk=pedido.pk).update(asignado_a=self.operador)
        respuesta = self.client.get(url)
        self.assertEqual(respuesta.context["paso"], "etiquetas")
        self.assertEqual([c.numero for c in respuesta.context["tanda"]], [3])
        self.assertContains(respuesta, "1 caja en esta tanda")
        self.assertContains(respuesta, f'name="paquete_id" value="{c3.pk}"')
        self.assertNotContains(respuesta, f'name="paquete_id" value="{c1.pk}"')
        self.assertContains(respuesta, "Caja 1 · lista")
        antes = c1.ts_etiquetas
        self._flujo_tanda(url, "segunda.jpg")
        c1.refresh_from_db()
        c3.refresh_from_db()
        self.assertEqual(c1.ts_etiquetas, antes)  # la primera tanda no se volvió a estampar
        self.assertNotEqual(c1.foto_cierre_id, c3.foto_cierre_id)
        self.assertIsNotNone(c3.ts_listo_salida)
