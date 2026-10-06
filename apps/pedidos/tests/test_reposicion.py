"""Reposición de producto (Chema 2026-10-05): la línea es el line item de
Shopify y NUNCA se agrega otra; lo repuesto se suma a la línea original
(cantidad_repuesta, con su caja de origen), se reserva stock (sin stock:
faltante que espera), el pedido regresa a PENDIENTE sin dueño con un plan solo
de lo repuesto cuyos renglones apuntan a la caja original (repone_a), lo que
ya salió queda estampado como despachado, y al entregarse la reposición la
compensación aprobada se paga sola."""
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.catalogo.models import SKU, Ubicacion
from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario
from apps.envios.models import Guia, Paquete, PaqueteLinea
from apps.incidencias.models import Compensacion, Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.inventario.models import LineaASN, OrdenEntrada
from apps.inventario.services import recibir, ubicar
from apps.pedidos import services
from apps.pedidos.models import LineaPedido, Pedido


@override_settings(ENVIA_API_KEY="", MEDIA_ROOT=tempfile.mkdtemp(prefix="torre-repo-"))
class ReposicionTests(TestCase):
    """Bodega mínima: A con 10 piezas ubicadas, B sin existencias."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.a = SKU.objects.create(cliente=self.cliente, codigo="A-SIX", descripcion="Six A", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        self.b = SKU.objects.create(cliente=self.cliente, codigo="B-SIX", descripcion="Six B", peso_gr=2000,
                                    requiere_lote=False, precio_declarado=Decimal(180))
        Ubicacion.objects.create(codigo="REC-01", tipo=Ubicacion.RECEPCION)
        self.pic = Ubicacion.objects.create(codigo="A-01-1", tipo=Ubicacion.PICKING)
        self.mesa = get_user_model().objects.create_user(username="mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=self.mesa, rol="mesa")
        orden = OrdenEntrada.objects.create(cliente=self.cliente)
        linea = LineaASN.objects.create(orden=orden, sku=self.a, cantidad_anunciada=10)
        recibir(linea, 10, 0, self.mesa)
        ubicar(self.a, 10, self.pic, None, self.mesa)

    def pedido_entregado(self):
        """A × 2 y B × 1 entregados en una caja; sin cantidad_despachada (anterior al parcial)."""
        pedido = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana",
                                       cp="01780", estado=Pedido.ENTREGADO, asignado_a=self.mesa)
        la = LineaPedido.objects.create(pedido=pedido, sku=self.a, cantidad=2, reservada=True, cantidad_pickeada=2)
        lb = LineaPedido.objects.create(pedido=pedido, sku=self.b, cantidad=1, reservada=True, cantidad_pickeada=1)
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("6"), carrier="estafeta",
                                      estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=la, cantidad=2)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=lb, cantidad=1)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero="EST-1", proveedor="mock",
                            estado=Guia.ENTREGADO)
        return pedido, la, lb

    def test_repone_lo_elegido_y_regresa_el_pedido_a_picking(self):
        pedido, la, lb = self.pedido_entregado()
        with self.captureOnCommitCallbacks(execute=True):
            nuevas = services.reponer_lineas(pedido, [(la, 1), (lb, 1)], self.mesa)
        pedido.refresh_from_db()
        la.refresh_from_db()
        lb.refresh_from_db()
        caja_vieja = Paquete.objects.get(pedido=pedido, numero=1)
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertIsNone(pedido.asignado_a)
        # Ninguna línea nueva: lo repuesto se suma a la original, con su caja de origen.
        self.assertEqual(pedido.lineas.count(), 2)
        self.assertEqual([(n.sku.codigo, n.cantidad, n.caja.numero) for n in nuevas], [("A-SIX", 1, 1), ("B-SIX", 1, 1)])
        self.assertEqual((la.cantidad, la.cantidad_repuesta, la.cantidad_repuesta_reservada, la.por_surtir, la.con_stock), (2, 1, 1, 3, 3))
        self.assertEqual((lb.cantidad, lb.cantidad_repuesta, lb.cantidad_repuesta_reservada), (1, 1, 0))  # B sin stock: faltante
        self.assertTrue(lb.faltante)
        self.assertEqual(la.origen_reposicion, [{"caja": caja_vieja.pk, "numero": 1, "cantidad": 1, "incidencia": ""}])
        # Lo que ya salió queda como despachado: el plan no lo repite; A tiene 1 pendiente (la repuesta).
        self.assertEqual((la.cantidad_despachada, lb.cantidad_despachada, la.pendiente, lb.pendiente), (2, 1, 1, 0))
        self.assertEqual([l.pk for l in pedido.lineas_por_surtir], [la.pk])
        self.assertTrue(pedido.pendiente_de_completar)
        planeadas = list(Paquete.objects.filter(pedido=pedido, estado=Paquete.PLANEADO))
        self.assertEqual(len(planeadas), 1)
        # El renglón de la caja nueva apunta a la caja original que repone.
        self.assertEqual([(pl.linea_pedido_id, pl.cantidad, pl.repone_a_id) for pl in planeadas[0].lineas.all()], [(la.pk, 1, caja_vieja.pk)])
        self.assertTrue(planeadas[0].es_reposicion)
        self.assertEqual((planeadas[0].cajas_que_repone, caja_vieja.cajas_de_reposicion), ([1], [planeadas[0].numero]))
        self.assertEqual(Paquete.objects.filter(pedido=pedido, estado=Paquete.DESPACHADO).count(), 1)  # la caja vieja sigue
        evento = EventoAuditoria.objects.get(entidad="pedido", entidad_id=str(pedido.pk), accion="reposicion")
        self.assertEqual((evento.delta["sin_stock"], evento.delta["lineas"]), (["B-SIX"], [["A-SIX", 1, 1], ["B-SIX", 1, 1]]))

    def test_al_empacar_la_reposicion_no_se_reimprimen_las_cajas_que_ya_salieron(self):
        """PED-00045 (2026-09-28): al cerrar la caja 3 salieron también las
        etiquetas de las cajas 1 y 2, ya en la calle."""
        from unittest.mock import patch

        pedido, la, _ = self.pedido_entregado()
        with self.captureOnCommitCallbacks(execute=True):
            services.reponer_lineas(pedido, [(la, 1)], self.mesa)
        Guia.objects.filter(pedido=pedido, paquete__numero=1).update(estado=Guia.RECOLECTADO)
        nueva = Guia.objects.create(pedido=pedido, paquete=None, carrier="local", proveedor="local",
                                    numero=f"LOCAL-{pedido.folio}-3", estado=Guia.GUIA_CREADA)
        with patch("apps.piso.etiquetas.imprimir_etiqueta", return_value="ok") as imprimir:
            guias, _ = services.imprimir_guias_activas(pedido)
        self.assertEqual([g.pk for g in guias], [nueva.pk])
        self.assertEqual(imprimir.call_count, 2)  # carrier + interna, solo de la nueva

    def test_solo_desde_la_calle_o_entregado_y_nunca_mas_piezas_que_las_pedidas(self):
        pedido, la, _ = self.pedido_entregado()
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 3)], self.mesa)
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 0)], self.mesa)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EMPACADO)
        pedido.refresh_from_db()
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 1)], self.mesa)
        self.assertEqual(pedido.lineas.count(), 2)  # nada cambió

    def pedido_ola_dos_pendiente(self):
        """PED-00051 (Chema 2026-09-30): A × 2 salió en la caja 1 (despachada);
        B × 1 no tenía stock y espera como segunda ola con el pedido PENDIENTE."""
        pedido = Pedido.objects.create(cliente=self.cliente, tienda=None, origen="manual", comprador_nombre="Ana",
                                       cp="01780", estado=Pedido.PENDIENTE)
        la = LineaPedido.objects.create(pedido=pedido, sku=self.a, cantidad=2, reservada=True, cantidad_pickeada=2,
                                        cantidad_despachada=2)
        lb = LineaPedido.objects.create(pedido=pedido, sku=self.b, cantidad=1)
        caja = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("4"), carrier="estafeta",
                                      estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=la, cantidad=2)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier="estafeta", numero="EST-1", proveedor="mock",
                            estado=Guia.ENTREGADO)
        return pedido, la, lb

    def test_piezas_reponibles_es_por_caja_despachada(self):
        pedido, la, lb = self.pedido_ola_dos_pendiente()
        reponibles = services.piezas_reponibles(pedido)
        self.assertEqual({k: (v["piezas"], v["cajas"]) for k, v in reponibles.items()}, {la.pk: (2, [1])})
        caja = Paquete.objects.get(pedido=pedido, numero=1)
        self.assertEqual(reponibles[la.pk]["por_caja"], {caja.pk: {"numero": 1, "piezas": 2}})
        # Pedido anterior al fulfillment parcial (sin cantidad_despachada) entregado: todo salió.
        viejo, va, vb = self.pedido_entregado()
        self.assertEqual({k: v["piezas"] for k, v in services.piezas_reponibles(viejo).items()}, {va.pk: 2, vb.pk: 1})
        # En bodega sin ninguna caja fuera: nada que reponer.
        Pedido.objects.filter(pk=viejo.pk).update(estado=Pedido.EMPACADO)
        viejo.refresh_from_db()
        self.assertEqual(services.piezas_reponibles(viejo), {})

    def test_con_la_ola_dos_pendiente_se_repone_lo_de_la_caja_que_salio_y_se_suma_a_la_ola(self):
        pedido, la, lb = self.pedido_ola_dos_pendiente()
        with self.assertRaises(ValueError) as ctx:
            services.reponer_lineas(pedido, [(lb, 1)], self.mesa)  # B sigue en bodega: se corrige, no se repone
        self.assertIn("no ha salido de bodega", str(ctx.exception))
        with self.assertRaises(ValueError):
            services.reponer_lineas(pedido, [(la, 3)], self.mesa)  # más de lo que salió
        with self.captureOnCommitCallbacks(execute=True):
            nuevas = services.reponer_lineas(pedido, [(la, 1)], self.mesa)
        pedido.refresh_from_db()
        lb.refresh_from_db()
        la.refresh_from_db()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)  # sin transición: ya estaba
        self.assertEqual([(n.sku.codigo, n.cantidad, n.caja.numero) for n in nuevas], [("A-SIX", 1, 1)])
        self.assertEqual((la.cantidad_repuesta, la.cantidad_repuesta_reservada, la.pendiente), (1, 1, 1))
        self.assertEqual(lb.cantidad_despachada, 0)  # la ola 2 no se estampa como salida
        # Una sola ola: la reposición se surte ya; B sigue faltante (sin stock) en el mismo pedido.
        self.assertEqual([l.pk for l in pedido.lineas_por_surtir], [la.pk])
        self.assertEqual([l.pk for l in pedido.lineas_faltantes], [lb.pk])
        self.assertFalse(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="cambio_estado").exists())
        self.assertTrue(EventoAuditoria.objects.filter(entidad="pedido", entidad_id=str(pedido.pk), accion="reposicion").exists())

    def test_con_la_ola_dos_en_manos_del_piso_se_espera(self):
        pedido, la, _ = self.pedido_ola_dos_pendiente()
        for estado in (Pedido.EN_PICKING, Pedido.EMPACADO, Pedido.GUIA_GENERADA):
            Pedido.objects.filter(pk=pedido.pk).update(estado=estado)
            pedido.refresh_from_db()
            with self.assertRaises(ValueError) as ctx:
                services.reponer_lineas(pedido, [(la, 1)], self.mesa)
            self.assertIn("espera a que salga", str(ctx.exception))
        self.assertEqual(pedido.lineas.count(), 2)

    def test_al_entregar_la_reposicion_la_compensacion_se_paga_sola(self):
        from apps.envios.services import _transicionar_pedido

        pedido, la, _ = self.pedido_entregado()
        inc = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=pedido, texto="Rota")
        comp = Compensacion.objects.create(incidencia=inc, tipo=Compensacion.TIPO_REPOSICION, monto=Decimal(180),
                                           lineas=[{"linea_id": la.pk, "sku": "A-SIX", "cantidad": 1}],
                                           estado=Compensacion.APROBADA)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.EN_TRANSITO)
        pedido.refresh_from_db()
        self.assertTrue(_transicionar_pedido(pedido, "ENTREGADO", motivo="Entregado"))
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.PAGADA)
        self.assertEqual(comp.referencia_pago, f"{pedido.folio} entregado")
        self.assertIsNotNone(comp.fecha_pago)
        self.assertTrue(inc.mensajes.filter(texto__contains="se entregó").exists())
