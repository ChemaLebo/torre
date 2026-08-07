"""Carril único por caja: empacar_caja, despachar_a_corral y cerrar_caja.

Contrato C1 (fin de la paradoja de la evidencia): al empacar solo existe el
contenido; la guía + impresión se encadenan en despachar_a_corral (best-effort
en impresora) y la foto de caja cerrada CON etiqueta pegada llega en
cerrar_caja. El manifiesto usa pedido.cajas_cerradas_completas para excluir.
"""
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.core.models import EventoAuditoria, EvidenciaFoto
from apps.envios.adapters import MockAdapter
from apps.envios.models import Guia, Paquete
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido

from .test_servicios import BaseServicios, foto


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class BaseEmpaqueCaja(BaseServicios):
    def pedido_en_picking(self, cantidad=2, peso_esperado_gr=4800):
        pedido = self.pedido_directo(estado=Pedido.EN_PICKING, peso_esperado_gr=peso_esperado_gr)
        LineaPedido.objects.create(
            pedido=pedido, sku=self.sku, cantidad=cantidad,
            cantidad_pickeada=cantidad, reservada=True,
        )
        return pedido

    def plan_de_cajas(self, pedido, pesos_kg):
        return [
            Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal(str(peso)),
                carrier="paquetexpress", servicio="ground",
            )
            for n, peso in enumerate(pesos_kg, start=1)
        ]


class EmpacarCajaTests(BaseEmpaqueCaja):
    def setUp(self):
        self.pedido = self.pedido_en_picking()  # 2 × 2400 g = 4800 g esperados
        self.caja1, self.caja2 = self.plan_de_cajas(self.pedido, ["2.40", "2.40"])

    def test_caja_feliz_guarda_peso_foto_y_transiciona(self):
        services.empacar_caja(self.caja1, actor=None, peso_real_gr=2380, foto_contenido=foto())
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.estado, Paquete.EMPACADO)
        self.assertEqual(self.caja1.peso_real_gr, 2380)  # por fin se usa
        fotos = EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk), tipo="contenido",
        )
        self.assertEqual(fotos.count(), 1)
        evento = EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk), accion="caja_empacada",
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.delta["caja"], 1)
        self.assertEqual(evento.delta["peso_real_gr"], 2380)
        # Falta la caja 2: el pedido sigue en picking.
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)

    def test_peso_de_caja_fuera_de_plan_no_toca_nada(self):
        with self.assertRaises(ValueError) as ctx:
            services.empacar_caja(self.caja1, actor=None, peso_real_gr=2600, foto_contenido=foto())
        self.assertIn("caja 1", str(ctx.exception))
        self.caja1.refresh_from_db()
        self.pedido.refresh_from_db()
        self.assertEqual(self.caja1.estado, Paquete.PLANEADO)
        self.assertIsNone(self.caja1.peso_real_gr)
        self.assertEqual(self.pedido.estado, Pedido.EN_PICKING)
        self.assertEqual(EvidenciaFoto.objects.count(), 0)

    def test_sin_foto_de_contenido_truena(self):
        with self.assertRaises(ValueError) as ctx:
            services.empacar_caja(self.caja1, actor=None, peso_real_gr=2380, foto_contenido=None)
        self.assertIn("foto del contenido", str(ctx.exception))
        self.caja1.refresh_from_db()
        self.assertEqual(self.caja1.estado, Paquete.PLANEADO)

    def test_ultima_caja_dispara_empacar_total(self):
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar_caja(self.caja1, actor=None, peso_real_gr=2380, foto_contenido=foto())
            services.empacar_caja(self.caja2, actor=None, peso_real_gr=2450, foto_contenido=foto("c2.jpg"))
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.EMPACADO)
        # Peso del pedido = Σ pesos reales por caja.
        self.assertEqual(self.pedido.peso_real_gr, 2380 + 2450)
        confirmar.assert_called_once_with(self.sku, 2, self.pedido.folio)
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk), tipo="contenido",
            ).count(),
            2,
        )

    def test_caja_ya_empacada_no_se_empaca_dos_veces(self):
        services.empacar_caja(self.caja1, actor=None, peso_real_gr=2380, foto_contenido=foto())
        self.caja1.refresh_from_db()
        with self.assertRaises(ValueError):
            services.empacar_caja(self.caja1, actor=None, peso_real_gr=2380, foto_contenido=foto())

    def test_lineas_sin_pickear_truena(self):
        linea = self.pedido.lineas.get()
        linea.cantidad_pickeada = 1
        linea.save(update_fields=["cantidad_pickeada"])
        with self.assertRaises(ValueError):
            services.empacar_caja(self.caja1, actor=None, peso_real_gr=2380, foto_contenido=foto())

    def test_plan_con_margen_de_empaque_real_no_atora_el_pedido(self):
        # Regresión del crítico #1: el plan por caja trae MARGEN_EMPAQUE ×1.05
        # (peso_esperado_gr del pedido es NETO). Un packer FIEL al plan
        # (báscula = plan por caja) debe poder empacar: la suma (5040 g, +5%
        # del neto 4800 g) NO se revalida contra el neto — cada caja ya pasó
        # su propia báscula. Antes esto trunaba y el pedido moría EN_PICKING.
        pedido = self.pedido_en_picking()  # neto esperado: 4800 g
        caja1, caja2 = self.plan_de_cajas(pedido, ["2.52", "2.52"])  # 2.40 × 1.05
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar_caja(caja1, actor=None, peso_real_gr=2520, foto_contenido=foto())
            services.empacar_caja(caja2, actor=None, peso_real_gr=2520, foto_contenido=foto("c2.jpg"))
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)
        self.assertEqual(pedido.peso_real_gr, 5040)
        confirmar.assert_called_once()

    def test_caja_fuera_de_su_plan_si_revierte(self):
        # La validación por caja sigue viva: la báscula contra el plan de ESA
        # caja (±3%) es el único candado de peso — y sí truena.
        pedido = self.pedido_en_picking()
        caja1, _ = self.plan_de_cajas(pedido, ["2.50", "2.50"])
        with self.assertRaises(ValueError) as ctx:
            services.empacar_caja(caja1, actor=None, peso_real_gr=2650, foto_contenido=foto())
        self.assertIn("peso", str(ctx.exception).lower())
        caja1.refresh_from_db()
        self.assertEqual(caja1.estado, Paquete.PLANEADO)

    def test_doble_post_de_la_misma_caja_no_empaca_dos_veces(self):
        # Regresión del crítico #2 (carrera): el segundo POST llega con una
        # instancia VIEJA (estado PLANEADO en memoria) pero empacar_caja
        # re-lee con select_for_update y valida el estado FRESCO → ValueError,
        # cero doble confirmar_pick.
        pedido = self.pedido_en_picking()
        caja1, caja2 = self.plan_de_cajas(pedido, ["2.40", "2.40"])
        caja1_vieja = Paquete.objects.get(pk=caja1.pk)  # copia rezagada
        services.empacar_caja(caja1, actor=None, peso_real_gr=2400, foto_contenido=foto())
        self.assertEqual(caja1_vieja.estado, Paquete.PLANEADO)  # sigue vieja
        with self.assertRaises(ValueError) as ctx:
            services.empacar_caja(caja1_vieja, actor=None, peso_real_gr=2400, foto_contenido=foto())
        self.assertIn("no se empaca dos veces", str(ctx.exception))
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(pedido.pk), tipo="contenido",
            ).count(),
            1,
        )

    def test_doble_post_de_la_ultima_caja_no_confirma_pick_doble(self):
        # La última caja encadena empacar() (confirmar_pick, kardex): un doble
        # POST de ESA caja con instancia vieja jamás debe comer reservas dos veces.
        pedido = self.pedido_en_picking()
        (caja1,) = self.plan_de_cajas(pedido, ["4.80"])
        caja_vieja = Paquete.objects.get(pk=caja1.pk)
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar_caja(caja1, actor=None, peso_real_gr=4800, foto_contenido=foto())
            with self.assertRaises(ValueError):
                services.empacar_caja(caja_vieja, actor=None, peso_real_gr=4800, foto_contenido=foto())
        confirmar.assert_called_once()
        pedido.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.EMPACADO)

    def test_empacar_con_instancia_vieja_valida_estado_fresco(self):
        # empacar() también re-lee el pedido bajo lock: un pedido YA EMPACADO
        # en BD no se vuelve a empacar aunque la instancia en memoria diga
        # EN_PICKING (dos tablets con la misma pantalla abierta).
        pedido = self.pedido_en_picking()
        pedido_viejo = Pedido.objects.get(pk=pedido.pk)
        with patch("apps.inventario.services.confirmar_pick") as confirmar:
            services.empacar(pedido, actor=None, peso_real_gr=4800, fotos=[foto()])
            with self.assertRaises(ValueError) as ctx:
                services.empacar(pedido_viejo, actor=None, peso_real_gr=4800, fotos=[foto()])
        self.assertIn("no se puede empacar", str(ctx.exception))
        confirmar.assert_called_once()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DespacharACorralTests(BaseServicios):
    def setUp(self):
        MockAdapter.reiniciar()
        self.pedido = self.pedido_directo(estado=Pedido.EMPACADO, peso_esperado_gr=4800)
        LineaPedido.objects.create(
            pedido=self.pedido, sku=self.sku, cantidad=2, cantidad_pickeada=2, reservada=True,
        )

    def test_genera_guias_e_imprime_en_el_mismo_paso(self):
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta",
            return_value="Etiqueta enviada a la impresora (mock).",
        ) as imprimir:
            resultado = services.despachar_a_corral(self.pedido, actor=None)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertGreaterEqual(len(resultado["guias"]), 1)
        self.assertEqual(imprimir.call_count, len(resultado["guias"]))
        self.assertIn("Etiqueta enviada a la impresora (mock).", resultado["mensajes"])
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="pedido", entidad_id=str(self.pedido.pk),
                accion="despachado_a_corral",
            ).exists()
        )

    def test_impresora_fallando_no_revierte_la_guia(self):
        with patch(
            "apps.piso.etiquetas.imprimir_etiqueta",
            side_effect=ValueError("La impresora 'Aiyin' rechazó el trabajo."),
        ):
            resultado = services.despachar_a_corral(self.pedido, actor=None)
        self.pedido.refresh_from_db()
        # BEST-EFFORT: la falla se acumula en mensajes y la guía queda viva.
        self.assertEqual(self.pedido.estado, Pedido.GUIA_GENERADA)
        self.assertGreaterEqual(len(resultado["guias"]), 1)
        self.assertTrue(all(m.startswith("No se imprimió") for m in resultado["mensajes"]))
        self.assertEqual(
            Guia.objects.filter(pedido=self.pedido).count(), len(resultado["guias"]),
        )

    def test_es_idempotente_sobre_las_guias(self):
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok"):
            primera = services.despachar_a_corral(self.pedido, actor=None)
            segunda = services.despachar_a_corral(self.pedido, actor=None)
        self.assertEqual(
            [g.pk for g in primera["guias"]], [g.pk for g in segunda["guias"]],
        )

    def test_pedido_sin_empacar_truena(self):
        pendiente = self.pedido_directo(estado=Pedido.PENDIENTE)
        with self.assertRaises(ValueError):
            services.despachar_a_corral(pendiente, actor=None)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CerrarCajaTests(BaseEmpaqueCaja):
    def setUp(self):
        self.pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA, peso_esperado_gr=4800)
        self.caja = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("2.40"),
            carrier="paquetexpress", estado=Paquete.EMPACADO,
        )
        self.guia = Guia.objects.create(
            pedido=self.pedido, paquete=self.caja, carrier="paquetexpress",
            numero="MOCK-C1", estado=Guia.GUIA_CREADA,
        )

    def _fotos_cierre(self):
        return EvidenciaFoto.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk), tipo="caja_cerrada",
        )

    def test_cierre_feliz_adjunta_foto_y_evento_con_numero_de_caja(self):
        services.cerrar_caja(self.caja, actor=None, foto_caja_cerrada=foto("cierre.jpg"))
        self.assertEqual(self._fotos_cierre().count(), 1)
        evento = EventoAuditoria.objects.filter(
            entidad="paquete", entidad_id=str(self.caja.pk),
            accion="caja_cerrada_con_evidencia",
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.delta["caja"], 1)
        self.assertEqual(evento.delta["pedido"], self.pedido.folio)

    def test_sin_foto_truena(self):
        with self.assertRaises(ValueError) as ctx:
            services.cerrar_caja(self.caja, actor=None, foto_caja_cerrada=None)
        self.assertIn("etiqueta pegada", str(ctx.exception))
        self.assertEqual(self._fotos_cierre().count(), 0)

    def test_caja_sin_empacar_no_se_cierra(self):
        planeada = Paquete.objects.create(
            pedido=self.pedido, numero=2, peso_kg=Decimal("2.40"), carrier="paquetexpress",
        )
        with self.assertRaises(ValueError) as ctx:
            services.cerrar_caja(planeada, actor=None, foto_caja_cerrada=foto())
        self.assertIn("no está empacada", str(ctx.exception))

    def test_caja_sin_guia_no_se_cierra(self):
        # La foto de cierre muestra la etiqueta pegada: sin guía no hay etiqueta.
        otro = self.pedido_directo(estado=Pedido.EMPACADO, peso_esperado_gr=2400)
        caja = Paquete.objects.create(
            pedido=otro, numero=1, peso_kg=Decimal("2.40"),
            carrier="paquetexpress", estado=Paquete.EMPACADO,
        )
        with self.assertRaises(ValueError) as ctx:
            services.cerrar_caja(caja, actor=None, foto_caja_cerrada=foto())
        self.assertIn("no tiene guía", str(ctx.exception))

    def test_una_caja_no_se_cierra_dos_veces(self):
        services.cerrar_caja(self.caja, actor=None, foto_caja_cerrada=foto())
        with self.assertRaises(ValueError) as ctx:
            services.cerrar_caja(self.caja, actor=None, foto_caja_cerrada=foto())
        self.assertIn("ya tiene su foto de cierre", str(ctx.exception))
        self.assertEqual(self._fotos_cierre().count(), 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CajasCerradasCompletasTests(BaseEmpaqueCaja):
    """Helper del manifiesto: excluir pedidos sin todas sus fotos de cierre."""

    def _foto_cierre_directa(self, pedido):
        EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(pedido.pk), tipo="caja_cerrada",
            archivo=foto("cierre.jpg"),
        )

    def test_multi_caja_exige_una_foto_por_caja(self):
        pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA, peso_esperado_gr=4800)
        cajas = [
            Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal("2.40"),
                carrier="paquetexpress", estado=Paquete.EMPACADO,
            )
            for n in (1, 2)
        ]
        for caja in cajas:
            Guia.objects.create(
                pedido=pedido, paquete=caja, carrier="paquetexpress",
                numero=f"MOCK-C{caja.numero}", estado=Guia.GUIA_CREADA,
            )
        self.assertFalse(pedido.cajas_cerradas_completas)
        services.cerrar_caja(cajas[0], actor=None, foto_caja_cerrada=foto())
        self.assertFalse(pedido.cajas_cerradas_completas)  # falta la caja 2
        services.cerrar_caja(cajas[1], actor=None, foto_caja_cerrada=foto())
        self.assertTrue(pedido.cajas_cerradas_completas)

    def test_foto_duplicada_de_una_caja_no_cierra_la_otra(self):
        # Regresión del alto #3: el conteo es POR CAJA (evento
        # caja_cerrada_con_evidencia del paquete), no por número de fotos del
        # pedido — dos fotos de la caja 1 NO habilitan el manifiesto sin la
        # evidencia de la caja 2.
        pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA, peso_esperado_gr=4800)
        cajas = [
            Paquete.objects.create(
                pedido=pedido, numero=n, peso_kg=Decimal("2.40"),
                carrier="paquetexpress", estado=Paquete.EMPACADO,
            )
            for n in (1, 2)
        ]
        for caja in cajas:
            Guia.objects.create(
                pedido=pedido, paquete=caja, carrier="paquetexpress",
                numero=f"MOCK-C{caja.numero}", estado=Guia.GUIA_CREADA,
            )
        services.cerrar_caja(cajas[0], actor=None, foto_caja_cerrada=foto())
        # Foto duplicada colada directo (el bug viejo la contaba como cierre 2).
        self._foto_cierre_directa(pedido)
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(pedido.pk), tipo="caja_cerrada",
            ).count(),
            2,
        )
        self.assertFalse(pedido.cajas_cerradas_completas)  # falta la caja 2
        services.cerrar_caja(cajas[1], actor=None, foto_caja_cerrada=foto())
        self.assertTrue(pedido.cajas_cerradas_completas)

    def test_cerrar_caja_con_instancia_vieja_respeta_el_candado(self):
        # Carrera del cierre: segunda llamada con copia rezagada del paquete —
        # el candado (evento por caja) se revisa bajo select_for_update.
        pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA, peso_esperado_gr=4800)
        caja = Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("2.40"),
            carrier="paquetexpress", estado=Paquete.EMPACADO,
        )
        Guia.objects.create(
            pedido=pedido, paquete=caja, carrier="paquetexpress",
            numero="MOCK-C1", estado=Guia.GUIA_CREADA,
        )
        caja_vieja = Paquete.objects.get(pk=caja.pk)
        services.cerrar_caja(caja, actor=None, foto_caja_cerrada=foto())
        with self.assertRaises(ValueError) as ctx:
            services.cerrar_caja(caja_vieja, actor=None, foto_caja_cerrada=foto())
        self.assertIn("ya tiene su foto de cierre", str(ctx.exception))
        self.assertEqual(
            EvidenciaFoto.objects.filter(
                entidad="pedido", entidad_id=str(pedido.pk), tipo="caja_cerrada",
            ).count(),
            1,
        )

    def test_legacy_sin_paquetes_es_una_caja_implicita(self):
        pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA)
        self.assertFalse(pedido.cajas_cerradas_completas)
        self._foto_cierre_directa(pedido)
        self.assertTrue(pedido.cajas_cerradas_completas)

    def test_plan_sin_empacar_por_caja_tambien_exige_cierre(self):
        # Plan creado al generar la guía (cajas PLANEADO, flujo legacy con
        # empacar total): cuenta como 1 caja implícita — sin foto no sube.
        pedido = self.pedido_directo(estado=Pedido.GUIA_GENERADA)
        Paquete.objects.create(
            pedido=pedido, numero=1, peso_kg=Decimal("2.40"), carrier="paquetexpress",
        )
        self.assertFalse(pedido.cajas_cerradas_completas)
        self._foto_cierre_directa(pedido)
        self.assertTrue(pedido.cajas_cerradas_completas)
