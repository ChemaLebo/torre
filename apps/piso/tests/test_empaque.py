"""Empaque en piso, contrato del carril único: báscula ±settings.TORRE, foto de
CONTENIDO obligatoria (la de caja cerrada se toma después, con la etiqueta
pegada) y guía + impresión encadenadas en el MISMO POST (despachar_a_corral)."""
from unittest.mock import patch

from django.db.models import Sum
from django.urls import reverse

from apps.core.models import EvidenciaFoto
from apps.envios.models import Guia
from apps.inventario.models import Saldo
from apps.pedidos.models import Pedido

from .base import PisoTestCase


class EmpaqueKitTests(PisoTestCase):
    """Kit (7A·2): candado de contenido + declaración con reserva REAL del kardex."""

    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)  # stock real del té (self.sku)
        from apps.catalogo.models import SKU as ModeloSKU
        self.kit_sku = ModeloSKU.objects.create(
            cliente=self.cliente, codigo="TEABOX", descripcion="TeaBox",
            peso_gr=400, es_kit=True, requiere_lote=False,
        )
        from apps.pedidos.models import LineaPedido
        self.pedido = Pedido.objects.create(
            cliente=self.cliente, origen="manual", estado=Pedido.PENDIENTE,
            comprador_nombre="Ana Prueba", cp="44100",
            direccion={"address1": "Av. Prueba 123", "city": "Guadalajara"},
            peso_esperado_gr=400,
        )
        self.linea_kit = LineaPedido.objects.create(
            pedido=self.pedido, sku=self.kit_sku, cantidad=1, reservada=True,
        )
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        confirmar_linea_pick(self.linea_kit, 1, self.operador)
        self.url = reverse("piso:empaque_pedido", args=[self.pedido.pk])

    def _reservado(self):
        return (
            Saldo.objects.filter(sku=self.sku, estado="reservado")
            .aggregate(total=Sum("cantidad"))["total"] or 0
        )

    def test_candado_declaracion_y_reserva_real(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "falta declarar")

        bloqueo = self.client.post(self.url, {
            "peso_real_gr": "400", "foto_contenido": self.foto(),
        }, follow=True)
        self.assertContains(bloqueo, "Declara el contenido")

        declarar = self.client.post(self.url, {
            "accion": "contenido_kit", "linea_kit": self.linea_kit.pk,
            "sku_1": self.sku.pk, "cantidad_1": "2",
        }, follow=True)
        self.assertContains(declarar, "declarado")
        hija = self.linea_kit.componentes.get()
        self.assertEqual(hija.cantidad, 2)
        self.assertEqual(hija.cantidad_pickeada, 2)  # nace pickeada
        self.assertEqual(self._reservado(), 2)  # kardex REAL apartado
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.peso_esperado_gr, 400 + 2 * 2000)

    def test_quitar_regresa_el_stock_al_pool(self):
        self.client.post(self.url, {
            "accion": "contenido_kit", "linea_kit": self.linea_kit.pk,
            "sku_1": self.sku.pk, "cantidad_1": "2",
        })
        self.assertEqual(self._reservado(), 2)
        quitar = self.client.post(self.url, {
            "accion": "quitar_contenido_kit", "linea_kit": self.linea_kit.pk,
        }, follow=True)
        self.assertContains(quitar, "liberado")
        self.assertEqual(self.linea_kit.componentes.count(), 0)
        self.assertEqual(self._reservado(), 0)


class EmpaquePisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=3)  # peso esperado: 6000 g
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(self.pedido, self.operador)
        confirmar_linea_pick(self.pedido.lineas.get(), 3, self.operador)
        self.pedido.refresh_from_db()
        self.url = reverse("piso:empaque_pedido", args=[self.pedido.pk])

    def test_checklist_foraneo_exige_insumos_del_cliente(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "insumos oficiales")
        self.assertContains(respuesta, self.pedido.cliente.nombre)

    def test_checklist_local_avisa_naked_packing(self):
        local = self.crear_pedido(cantidad=1, es_local=True)
        from apps.pedidos.services import confirmar_linea_pick, iniciar_picking
        iniciar_picking(local, self.operador)
        confirmar_linea_pick(local.lineas.get(), 1, self.operador)
        respuesta = self.client.get(reverse("piso:empaque_pedido", args=[local.pk]))
        self.assertContains(respuesta, "NAKED PACKING")
        self.assertContains(respuesta, "JAMÁS cinta del 3PL")

    def test_sin_foto_de_contenido_no_hay_empacado(self):
        respuesta = self.client.post(self.url, {"peso_real_gr": "6000"}, follow=True)
        self.assertContains(respuesta, "Falta la foto del contenido")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    # Check de peso APAGADO (2026-08-28, ver empacar()): regresa con el
    # catálogo de cajas.
    # def test_peso_fuera_de_tolerancia_muestra_esperado_vs_real(self):
    #     respuesta = self.client.post(self.url, {
    #         "peso_real_gr": "7000",  # 16.7% arriba: fuera de ±3%
    #         "foto_contenido": self.foto("contenido.jpg"),
    #     }, follow=True)
    #     self.assertContains(respuesta, "El peso no cuadra")
    #     self.assertContains(respuesta, "6000")
    #     self.assertContains(respuesta, "7000")
    #     self.pedido.refresh_from_db()
    #     self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
    #     self.assertIsNone(self.pedido.peso_real_gr)

    def test_empacado_feliz_encadena_guia_e_impresion(self):
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta",
            return_value="Etiqueta enviada a la impresora (mock).",
        ) as imprimir:
            respuesta = self.client.post(self.url, {
                "peso_real_gr": "6100",  # 1.7%: dentro de tolerancia
                "foto_contenido": self.foto("contenido.jpg"),
            })
        # Wizard del carril único: el mismo POST encadena guía + impresión y
        # regresa al wizard, que ahora pide la foto de CIERRE por caja.
        self.assertRedirects(respuesta, self.url, fetch_redirect_response=False)

        self.pedido.refresh_from_db()
        # MISMO POST: empaque verificado + guía generada + impresión lanzada.
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertEqual(self.pedido.peso_real_gr, 6100)
        self.assertIsNotNone(self.pedido.ts_empacado)
        self.assertIsNotNone(self.pedido.ts_guia)

        guias = list(Guia.objects.filter(pedido=self.pedido))
        self.assertGreaterEqual(len(guias), 1)
        self.assertEqual(imprimir.call_count, len(guias))

        fotos = EvidenciaFoto.objects.filter(entidad="pedido", entidad_id=str(self.pedido.pk))
        self.assertEqual(fotos.count(), 1)
        self.assertEqual(set(fotos.values_list("tipo", flat=True)), {"contenido"})
        en_empaque = (
            Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_EMPAQUE)
            .aggregate(t=Sum("cantidad"))["t"] or 0
        )
        self.assertEqual(en_empaque, 3)

    def test_falla_de_impresora_no_revierte_la_guia(self):
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta",
            side_effect=ValueError("No hay impresora configurada."),
        ):
            respuesta = self.client.post(self.url, {
                "peso_real_gr": "6100",
                "foto_contenido": self.foto("contenido.jpg"),
            }, follow=True)
        self.assertContains(respuesta, "No se imprimió la etiqueta")
        self.pedido.refresh_from_db()
        # BEST-EFFORT: la impresora falló pero la guía queda viva.
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertTrue(Guia.objects.filter(pedido=self.pedido).exists())

    def test_error_del_carrier_deja_el_pedido_empacado(self):
        with patch(
            "apps.envios.services.generar_guia",
            side_effect=RuntimeError("timeout del carrier"),
        ):
            respuesta = self.client.post(self.url, {
                "peso_real_gr": "6100",
                "foto_contenido": self.foto("contenido.jpg"),
            }, follow=True)
        self.assertContains(respuesta, "El carrier no respondió")
        self.pedido.refresh_from_db()
        # El empaque NO se pierde: queda EMPACADO y recuperable desde Salida.
        self.assertEqual(self.pedido.estado, Pedido.EMPACADO)
        self.assertFalse(Guia.objects.filter(pedido=self.pedido).exists())

    def test_pedido_ya_empacado_muestra_paso_de_cierre(self):
        # EMPACADO sin guía (excepción legacy): el wizard muestra el paso de
        # cierre con el aviso de que la guía se genera en Salida.
        self.dejar_empacado(self.pedido)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "no tiene guía")
        self.assertContains(respuesta, "foto de cierre")

    def test_cierre_legacy_con_guia_guarda_evidencia_y_muestra_exito(self):
        # Flujo completo del wizard legacy: empacar → guía → foto de cierre →
        # pantalla de éxito con SIGUIENTE PEDIDO.
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta",
            return_value="Etiqueta enviada a la impresora (mock).",
        ):
            self.client.post(self.url, {
                "peso_real_gr": "6100",
                "foto_contenido": self.foto("contenido.jpg"),
            })
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)

        # GET: paso de cierre con las guías impresas.
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Etiquetas imprimiéndose")
        self.assertContains(respuesta, "cerrar_legacy")

        # POST cierre sin foto → error claro, nada se guarda.
        sin_foto = self.client.post(self.url, {"accion": "cerrar_legacy"}, follow=True)
        self.assertContains(sin_foto, "Toma la foto de la caja cerrada")
        self.assertFalse(self.pedido.cajas_cerradas_completas)

        # POST cierre con foto → evidencia guardada + pantalla de éxito.
        respuesta = self.client.post(self.url, {
            "accion": "cerrar_legacy", "foto_cierre": self.foto("cierre.jpg"),
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.pedido.refresh_from_db()
        self.assertTrue(self.pedido.cajas_cerradas_completas)
        self.assertTrue(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk), tipo="caja_cerrada",
            ).exists()
        )
        self.assertContains(respuesta, "SIGUIENTE PEDIDO")
        self.assertContains(respuesta, "listo")
