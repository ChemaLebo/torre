"""Línea de tiempo (Chema 2026-09-25) en Mesa y portal: una fila por
paquete con las horas del pedido y de su caja (guía, manifiesto, recolección
del carrier, tránsito, entrega), el compromiso y el último evento de la
paquetería; el portal solo ve lo suyo."""
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import PerfilUsuario
from apps.envios.models import EventoGuia, Guia, LineaManifiesto, Manifiesto, Paquete
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda


class LineaTiempoTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente(slug="colima")
        self.tienda = crear_tienda(self.cliente)
        self.otro = crear_cliente(slug="nocturno", nombre="Mezcal Nocturno")
        ts = timezone.make_aware(datetime(2026, 9, 24, 19, 52))
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="RECOLECTADO", shopify_order_name="#4074",
                                   ts_recolectado=ts, ts_recolectado_carrier=ts)
        caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=4, carrier="imile", estado=Paquete.DESPACHADO)
        guia = Guia.objects.create(pedido=self.pedido, paquete=caja, carrier="imile", numero="6092426140197", proveedor="envia",
                                   estado=Guia.RECOLECTADO, fecha_compromiso=date(2026, 9, 29), dias_promesa=5, ts_recolectado_carrier=ts)
        EventoGuia.objects.create(guia=guia, estado="RECOLECTADO", crudo="Shipped", descripcion="Shipped", ts_carrier=ts)
        hoja = Manifiesto.objects.create(folio="MAN-2026-0007", carrier="imile", corral="SAL-OTRO", chofer="Luis")
        LineaManifiesto.objects.create(manifiesto=hoja, pedido=self.pedido, paquete=caja, guia=guia, numero_guia=guia.numero, caja=1)
        # Segunda caja del mismo pedido, aún en bodega: su propia fila.
        caja2 = Paquete.objects.create(pedido=self.pedido, numero=2, peso_kg=4, carrier="imile", estado=Paquete.EMPACADO)
        Guia.objects.create(pedido=self.pedido, paquete=caja2, carrier="imile", numero="6092426140198", proveedor="envia", dias_promesa=5)
        self.ajeno = crear_pedido(self.otro, crear_tienda(self.otro))

    def _mesa(self):
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_mesa_muestra_pasos_caja_manifiesto_y_eventos(self):
        self._mesa()
        html = self.client.get(reverse("mesa:linea_tiempo")).content.decode()
        for esperado in (self.pedido.folio, "#4074", "24/Sep 19:52", "MAN-2026-0007 ↗</a> · Luis", "29/Sep", "Shipped",
                         "6092426140197", "6092426140198", "1 de 2", "2 de 2", "5 días desde la salida", self.ajeno.folio):
            self.assertIn(esperado, html)
        self.assertNotIn('<details class="colapsable"', html)  # sin acordeones en la tabla: una fila por paquete (el menú sí usa details)
        filtrado = self.client.get(reverse("mesa:linea_tiempo"), {"q": "#4074"}).content.decode()
        self.assertIn(self.pedido.folio, filtrado)
        self.assertNotIn(self.ajeno.folio, filtrado)
        # Estatus del paquete: solo la caja recolectada, no la que sigue en bodega.
        por_paquete = self.client.get(reverse("mesa:linea_tiempo"), {"guia_estado": "RECOLECTADO"})
        self.assertEqual([f["caja"] for f in por_paquete.context["filas"]], [1])
        self.assertEqual(self.client.get(reverse("mesa:linea_tiempo"), {"guia_estado": "ENTREGADO"}).context["filas"], [])

    def test_la_segunda_ola_muestra_sus_propias_horas(self):
        """PED-00034 (2026-09-30): la caja de la reposición mostraba el picking y
        el empaque del 21 (los ts del pedido, que se estampan una sola vez)."""
        from apps.core.models import EventoAuditoria
        from apps.pedidos.linea_tiempo import construir
        from apps.pedidos.models import Pedido

        primera = timezone.make_aware(datetime(2026, 9, 21, 12, 17))
        Pedido.objects.filter(pk=self.pedido.pk).update(ts_picking=primera, ts_empacado=primera + timedelta(hours=1))
        Paquete.objects.filter(pedido=self.pedido).update(creado=primera - timedelta(hours=2))  # la primera ola se planeó el 21
        caja3 = Paquete.objects.create(pedido=self.pedido, numero=3, peso_kg=4, carrier="estafeta", estado=Paquete.EMPACADO)
        hoy = timezone.now()
        pick = EventoAuditoria.objects.create(entidad="pedido", entidad_id=str(self.pedido.pk), accion="cambio_estado",
                                              delta={"de": "PENDIENTE", "a": "EN_PICKING"})
        emp = EventoAuditoria.objects.create(entidad="pedido", entidad_id=str(self.pedido.pk), accion="caja_empacada",
                                             delta={"caja": 3, "peso_real_gr": 4000})
        EventoAuditoria.objects.filter(pk=pick.pk).update(ts=hoy + timedelta(minutes=5))
        EventoAuditoria.objects.filter(pk=emp.pk).update(ts=hoy + timedelta(minutes=40))
        filas = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=self.pedido.pk))}
        self.assertEqual((filas[1]["ts"]["picking"], filas[1]["ts"]["empacado"]), (primera, primera + timedelta(hours=1)))
        self.assertEqual(filas[3]["ts"]["picking"], hoy + timedelta(minutes=5))
        self.assertEqual(filas[3]["ts"]["empacado"], hoy + timedelta(minutes=40))

    def test_horas_completas_eventos_sin_fecha_y_relleno_del_paso_anterior(self):
        """Chema 2026-10-12 (PED-00255): iMile vía envia manda los eventos sin
        fecha → se usa la hora en que Torre los leyó; un paso que el carrier
        nunca reportó toma la hora del anterior y se marca ≈; una hora real
        nunca se pisa; una entrega local no inventa pasos de paquetería."""
        from apps.pedidos.linea_tiempo import construir
        from apps.pedidos.models import Pedido

        base = timezone.make_aware(datetime(2026, 9, 24, 12, 0))
        Pedido.objects.filter(pk=self.pedido.pk).update(ts_picking=base, ts_empacado=base + timedelta(hours=1))
        guia = Guia.objects.get(numero="6092426140197")
        visto_ruta = timezone.make_aware(datetime(2026, 9, 26, 8, 45))
        visto_entrega = timezone.make_aware(datetime(2026, 9, 26, 16, 45))
        ruta = EventoGuia.objects.create(guia=guia, estado="EN_RUTA", crudo="EN_RUTA", descripcion="Out for Delivery")
        entrega = EventoGuia.objects.create(guia=guia, estado="ENTREGADO", crudo="ENTREGADO", descripcion="Delivered")
        EventoGuia.objects.filter(pk=ruta.pk).update(ts_visto=visto_ruta)
        EventoGuia.objects.filter(pk=entrega.pk).update(ts_visto=visto_entrega)
        Guia.objects.filter(pk=guia.pk).update(estado=Guia.ENTREGADO)
        fila = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=self.pedido.pk))}[1]
        # Sin fecha del carrier: la hora de lectura. "En tránsito" nunca se reportó: toma la de recolección y se marca.
        self.assertEqual((fila["ts"]["en_ruta"], fila["ts"]["entregado"]), (visto_ruta, visto_entrega))
        self.assertEqual(fila["ts"]["en_transito"], fila["ts"]["recolectado_carrier"])
        self.assertEqual(fila["estimados"], {"en_transito"})
        self.assertIsNone(fila["ts_real"]["en_transito"])
        self._mesa()
        html = self.client.get(reverse("mesa:pedido_detalle", args=[self.pedido.pk])).content.decode()
        self.assertIn("≈ 24/Sep 19:52", html)  # en tránsito rellenado con la recolección
        self.assertIn("26/Sep 16:45", html)
        # La caja 2 sigue en bodega: nada después de "guía creada" se rellena.
        fila2 = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=self.pedido.pk))}[2]
        self.assertEqual(fila2["estimados"], set())
        self.assertIsNone(fila2["ts"]["salida"])
        # Entregada sin ningún evento: "entregado" toma el último movimiento de la guía y el resto se rellena.
        guia2 = Guia.objects.get(numero="6092426140198")
        mov = timezone.make_aware(datetime(2026, 9, 27, 10, 0))
        Guia.objects.filter(pk=guia2.pk).update(estado=Guia.ENTREGADO, ts_ultimo_movimiento=mov)
        fila2 = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=self.pedido.pk))}[2]
        self.assertEqual(fila2["ts"]["entregado"], mov)
        self.assertEqual(fila2["estimados"], {"salida", "recolectado_carrier", "en_transito", "en_ruta"})
        self.assertEqual(fila2["ts"]["en_ruta"], fila2["ts"]["guia"])
        # Local entregada: sin pasos de paquetería inventados.
        local = Paquete.objects.create(pedido=self.pedido, numero=3, peso_kg=4, carrier="local", estado=Paquete.DESPACHADO)
        Guia.objects.create(pedido=self.pedido, paquete=local, carrier="local", numero="LOCAL-1", proveedor="local", estado=Guia.ENTREGADO, ts_ultimo_movimiento=mov)
        fila3 = {f["caja"]: f for f in construir(Pedido.objects.filter(pk=self.pedido.pk))}[3]
        self.assertIsNone(fila3["ts"]["en_transito"])
        self.assertEqual(fila3["ts"]["entregado"], mov)
        self.assertNotIn("en_transito", fila3["estimados"])

    def test_portal_solo_ve_lo_suyo(self):
        karina = get_user_model().objects.create_user("karina", password="x12345678")
        PerfilUsuario.objects.create(usuario=karina, rol="portal", cliente=self.cliente)
        self.client.force_login(karina)
        html = self.client.get(reverse("portal:linea_tiempo")).content.decode()
        self.assertIn(self.pedido.folio, html)
        self.assertNotIn("MAN-2026-0007", html)  # el manifiesto es interno: el cliente no lo ve (Chema 2026-10-05)
        self.assertNotIn(self.ajeno.folio, html)
