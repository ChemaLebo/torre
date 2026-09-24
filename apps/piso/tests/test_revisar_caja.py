"""Navegación entre cajas del wizard de empaque; cambio de una foto y de la báscula.

Chips arriba (?caja=N): la caja pendiente se empaca ella, la empacada sin
cierre abre su paso de cierre (con su foto de contenido y su báscula a la
mano) y la cerrada se revisa con sus dos fotos y su peso.
reemplazar_foto_pedido mete la foto nueva, re-apunta la caja, borra la vieja
y deja el evento foto_reemplazada; corregir_peso_caja cambia la báscula sin
reabrir nada y recalcula el peso del pedido si ya quedó empacado.
"""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria, EvidenciaFoto
from apps.envios.adapters import MockAdapter
from apps.envios.models import Paquete, PaqueteLinea
from apps.pedidos.models import Pedido

from .base import PisoTestCase

TORRE_CARRIERS_CLASICOS = {
    **settings.TORRE,
    "CARRIERS_COTIZAR": ["estafeta", "paquetexpress", "fedex", "noventa9Minutos", "amPm"],
}


@override_settings(TORRE=TORRE_CARRIERS_CLASICOS)
class RevisarCajaTests(PisoTestCase):
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

    def _empacar(self, caja, peso, nombre):
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok (mock)"):
            return self.client.post(self.url, {
                "accion": "empacar_caja", "paquete_id": caja.pk,
                "peso_real_gr": peso, "foto_contenido": self.foto(nombre),
            }, follow=True)

    def _cerrar(self, caja, nombre):
        return self.client.post(self.url, {
            "accion": "cerrar_caja", "paquete_id": caja.pk, "foto_cierre": self.foto(nombre),
        }, follow=True)

    def _reemplazar(self, evidencia_id, caja, nombre="otra.jpg", foto=True):
        datos = {"accion": "reemplazar_foto", "evidencia_id": evidencia_id, "caja": caja}
        if foto:
            datos["foto"] = self.foto(nombre)
        return self.client.post(self.url, datos, follow=True)

    def _fotos(self, tipo):
        return EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk), tipo=tipo,
        )

    def test_chips_y_elegir_que_caja_empacar_primero(self):
        respuesta = self.client.get(self.url + "?caja=2")
        self.assertContains(respuesta, "Caja 2 de 2")
        self.assertContains(respuesta, f'name="paquete_id" value="{self.caja2.pk}"')
        self.assertContains(respuesta, "Caja 1 · pendiente")
        self.assertContains(respuesta, "?caja=1")
        # Sin ?caja, o con una que no existe, sigue la primera pendiente.
        self.assertContains(self.client.get(self.url), "Caja 1 de 2")
        self.assertContains(self.client.get(self.url + "?caja=9"), "Caja 1 de 2")

    def test_empacar_caja_estampa_su_foto_de_contenido(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self.caja1.refresh_from_db()
        self.assertIsNotNone(self.caja1.foto_contenido)
        self.assertEqual(self.caja1.foto_contenido.tipo, "contenido")
        self.assertEqual(self.caja1.foto_contenido.entidad_id, str(self.pedido.pk))

    def test_caja_empacada_se_revisa_y_su_foto_de_contenido_se_cambia(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self.caja1.refresh_from_db()
        vieja = self.caja1.foto_contenido

        respuesta = self.client.get(self.url + "?caja=1")
        self.assertContains(respuesta, "Revisar caja")
        self.assertContains(respuesta, "Caja 1 · empacada")
        self.assertContains(respuesta, reverse("core:evidencia", args=[vieja.pk]))
        self.assertContains(respuesta, f'name="evidencia_id" value="{vieja.pk}"')
        self.assertContains(respuesta, "Se toma en el paso de cierre")  # aún sin foto de cierre

        respuesta = self._reemplazar(vieja.pk, "1", "c1-bien.jpg")
        self.assertContains(respuesta, "Foto del contenido cambiada (caja 1)")
        self.assertContains(respuesta, "Revisar caja")  # regresa a la misma caja
        self.caja1.refresh_from_db()
        self.assertNotEqual(self.caja1.foto_contenido_id, vieja.pk)
        self.assertFalse(EvidenciaFoto.objects.filter(pk=vieja.pk).exists())
        self.assertEqual(self._fotos("contenido").count(), 1)
        evento = EventoAuditoria.objects.get(accion="foto_reemplazada")
        self.assertEqual((evento.entidad, evento.entidad_id), ("paquete", str(self.caja1.pk)))
        self.assertEqual(evento.delta["evidencia_anterior"]["id"], vieja.pk)
        self.assertEqual(evento.delta["evidencia_id"], self.caja1.foto_contenido_id)
        self.assertEqual(evento.delta["cajas"], [1])
        # Nada se reabrió ni se re-pesó: la caja sigue empacada y el pedido en picking.
        self.assertEqual(self.caja1.estado, Paquete.EMPACADO)
        self.assertEqual(self.caja1.peso_real_gr, 12100)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    def test_en_cierre_se_elige_la_caja_y_la_foto_de_cierre_se_cambia_sin_reabrir(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self._empacar(self.caja2, "8100", "c2.jpg")
        self.caja2.refresh_from_db()

        # ?caja=2 abre el cierre de la 2 aunque la 1 siga por cerrar, con su
        # foto de contenido a la mano por si salió mal.
        respuesta = self.client.get(self.url + "?caja=2")
        self.assertContains(respuesta, 'value="cerrar_caja"')
        self.assertContains(respuesta, f'name="paquete_id" value="{self.caja2.pk}"')
        self.assertContains(respuesta, f'name="evidencia_id" value="{self.caja2.foto_contenido_id}"')
        self.assertContains(respuesta, 'value="corregir_peso"')
        self.assertContains(respuesta, "Faltan después:")
        self.assertContains(respuesta, "caja 1")

        self._cerrar(self.caja1, "z1.jpg")
        self.caja1.refresh_from_db()
        vieja, ts_cierre = self.caja1.foto_cierre, self.caja1.ts_cierre
        self.assertIsNotNone(vieja)

        respuesta = self.client.get(self.url + "?caja=1")
        self.assertContains(respuesta, "Revisar caja")
        self.assertContains(respuesta, "Caja 1 · cerrada")
        self.assertContains(respuesta, f'name="evidencia_id" value="{vieja.pk}"')

        respuesta = self._reemplazar(vieja.pk, "1", "z1-bien.jpg")
        self.assertContains(respuesta, "Foto de la caja cerrada cambiada (caja 1)")
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.ts_cierre, ts_cierre)
        self.assertNotEqual(self.caja1.foto_cierre_id, vieja.pk)
        self.assertEqual(self.caja1.foto_cierre.tipo, "caja_cerrada")
        self.assertFalse(EvidenciaFoto.objects.filter(pk=vieja.pk).exists())
        self.assertEqual(self._fotos("caja_cerrada").count(), 1)
        # La caja 2 sigue por cerrar: el cambio de foto no cerró nada de más.
        self.assertFalse(self.pedido.cajas_cerradas_completas)
        self.assertEqual(self.pedido.paquetes.filter(ts_cierre__isnull=False).count(), 1)

    def test_desde_exito_se_revisan_las_cajas(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self._empacar(self.caja2, "8100", "c2.jpg")
        self._cerrar(self.caja1, "z1.jpg")
        respuesta = self._cerrar(self.caja2, "z2.jpg")
        self.assertContains(respuesta, "SIGUIENTE PEDIDO")
        self.assertContains(respuesta, "Caja 1 · cerrada")
        self.assertContains(respuesta, "Caja 2 · cerrada")
        self.assertContains(respuesta, "¿Una foto salió mal?")

        respuesta = self.client.get(self.url + "?caja=2")
        self.assertContains(respuesta, "Revisar caja")
        self.assertContains(respuesta, "Caja 2 de 2")
        self.caja2.refresh_from_db()
        self.assertContains(respuesta, f'name="evidencia_id" value="{self.caja2.foto_contenido_id}"')
        self.assertContains(respuesta, f'name="evidencia_id" value="{self.caja2.foto_cierre_id}"')
        self.assertContains(respuesta, "Volver al paso actual")

    def test_foto_congelada_por_incidencia_no_se_cambia(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self.caja1.refresh_from_db()
        vieja = self.caja1.foto_contenido
        EvidenciaFoto.objects.filter(pk=vieja.pk).update(congelada=True)

        respuesta = self.client.get(self.url + "?caja=1")
        self.assertContains(respuesta, "expediente de una incidencia")
        self.assertNotContains(respuesta, f'name="evidencia_id" value="{vieja.pk}"')

        respuesta = self._reemplazar(vieja.pk, "1")
        self.assertContains(respuesta, "expediente de una incidencia")
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.foto_contenido_id, vieja.pk)
        self.assertEqual(self._fotos("contenido").count(), 1)
        self.assertFalse(EventoAuditoria.objects.filter(accion="foto_reemplazada").exists())

    def test_foto_ajena_inexistente_o_sin_archivo_no_cambia_nada(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self.caja1.refresh_from_db()
        otro = self.crear_pedido(cantidad=1)
        ajena = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(otro.pk), tipo="contenido",
            archivo=self.foto("ajena.jpg"), tomada_por="alguien",
        )
        for evidencia_id in (ajena.pk, 999999, "x"):
            respuesta = self._reemplazar(evidencia_id, "1")
            self.assertContains(respuesta, "no es de empaque de")
        respuesta = self._reemplazar(self.caja1.foto_contenido_id, "1", foto=False)
        self.assertContains(respuesta, "Toma la foto nueva")
        self.assertTrue(EvidenciaFoto.objects.filter(pk=ajena.pk).exists())
        self.assertEqual(self._fotos("contenido").count(), 1)
        self.assertFalse(EventoAuditoria.objects.filter(accion="foto_reemplazada").exists())

    def test_cierre_legacy_ofrece_cambiar_sus_fotos_sueltas(self):
        # Pedido sin plan de cajas: sus fotos cuelgan del pedido, no de una caja.
        # Con 20 kg el plan que nace en la guía traería dos cajas y desde el
        # 2026-09-24 eso manda a reempacar (ReempaquePendiente): el legacy de
        # una caja implícita se prueba con un pedido que cabe en una sola.
        Paquete.objects.filter(pedido=self.pedido).delete()
        linea = self.pedido.lineas.get()
        linea.cantidad = 4
        linea.cantidad_pickeada = 4
        linea.save(update_fields=["cantidad", "cantidad_pickeada"])
        self.pedido.peso_esperado_gr = self.sku.peso_gr * 4
        self.pedido.save(update_fields=["peso_esperado_gr"])
        pedido = self.dejar_empacado(self.pedido)
        from apps.envios.services import generar_guia
        generar_guia(pedido)
        self.assertEqual(pedido.paquetes.count(), 1)
        contenido = self._fotos("contenido").get()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'value="cerrar_legacy"')
        self.assertContains(respuesta, f'name="evidencia_id" value="{contenido.pk}"')

        respuesta = self._reemplazar(contenido.pk, "", "mejor.jpg")
        self.assertContains(respuesta, "Foto del contenido cambiada.")
        self.assertFalse(EvidenciaFoto.objects.filter(pk=contenido.pk).exists())
        self.assertEqual(self._fotos("contenido").count(), 1)
        evento = EventoAuditoria.objects.get(accion="foto_reemplazada")
        self.assertEqual((evento.entidad, evento.entidad_id), ("pedido", str(pedido.pk)))
        self.assertEqual(evento.delta["cajas"], [])

    def _corregir(self, caja, peso):
        return self.client.post(self.url, {
            "accion": "corregir_peso", "paquete_id": caja.pk, "peso_real_gr": peso,
        }, follow=True)

    def test_corregir_la_bascula_de_una_caja_empacada_en_picking(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        respuesta = self.client.get(self.url + "?caja=1")
        self.assertContains(respuesta, 'value="corregir_peso"')
        self.assertContains(respuesta, "12100 g")

        respuesta = self._corregir(self.caja1, "11900")
        self.assertContains(respuesta, "Caja 1: báscula corregida a 11900 g")
        self.assertContains(respuesta, "11900 g")  # regresa a la revisión de la caja
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.peso_real_gr, 11900)
        self.assertEqual(self.caja1.estado, Paquete.EMPACADO)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertIsNone(self.pedido.peso_real_gr)  # aún no empacado entero
        evento = EventoAuditoria.objects.get(accion="peso_corregido")
        self.assertEqual((evento.entidad, evento.entidad_id), ("paquete", str(self.caja1.pk)))
        self.assertEqual((evento.delta["de_gr"], evento.delta["a_gr"]), (12100, 11900))

    def test_corregir_tras_la_guia_recalcula_el_pedido_sin_tocar_cierre_ni_guias(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self._empacar(self.caja2, "8100", "c2.jpg")
        self._cerrar(self.caja1, "z1.jpg")
        self.pedido.refresh_from_db()
        self.caja1.refresh_from_db()
        self.assertEqual(self.pedido.peso_real_gr, 12100 + 8100)
        ts_cierre, guias = self.caja1.ts_cierre, self.pedido.guias.count()

        respuesta = self._corregir(self.caja1, "12500")
        self.assertContains(respuesta, "báscula corregida a 12500 g")
        self.assertContains(respuesta, "viaja con el peso anterior")
        self.caja1.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual(self.caja1.peso_real_gr, 12500)
        self.assertEqual(self.pedido.peso_real_gr, 12500 + 8100)
        self.assertEqual(self.caja1.ts_cierre, ts_cierre)
        self.assertEqual(self.pedido.guias.count(), guias)
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)

    @override_settings(TORRE_PESO_MODO="bloquear")
    def test_corregir_fuera_de_rango_en_modo_bloquear_no_cambia_nada(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        respuesta = self._corregir(self.caja1, "20000")
        self.assertContains(respuesta, "no cuadra")
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.peso_real_gr, 12100)
        self.assertFalse(EventoAuditoria.objects.filter(accion="peso_corregido").exists())

    def test_corregir_rechaza_caja_sin_pesar_peso_invalido_y_mismo_peso(self):
        self._empacar(self.caja1, "12100", "c1.jpg")
        self.assertContains(self._corregir(self.caja2, "8000"), "todavía no se ha pesado")
        self.assertContains(self._corregir(self.caja1, "abc"), "Captura el peso")
        self.assertContains(self._corregir(self.caja1, "0"), "Captura el peso")
        self.assertContains(self._corregir(self.caja1, "12100"), "no hay nada que corregir")
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.peso_real_gr, 12100)
        self.assertFalse(EventoAuditoria.objects.filter(accion="peso_corregido").exists())
