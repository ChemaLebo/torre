"""Intentos de entrega visibles (Chema 2026-10-14): a partir de los eventos
del carrier normalizados a INTENTO_FALLIDO, uno por día; se ven en Mesa →
Pedido → Cajas, en la línea de tiempo, en la página de rastreo del comprador
y en el encabezado de la incidencia. Un carrier que no reporta intentos no
muestra nada."""
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import PerfilUsuario
from apps.envios.models import EventoGuia, Guia, Paquete
from apps.envios.services import guias_del_pedido, intentos_de_entrega
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda
from apps.incidencias.models import Incidencia
from apps.incidencias.services import abrir_incidencia
from apps.pedidos.linea_tiempo import construir
from apps.pedidos.models import Pedido


class IntentosDeEntregaTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente(slug="colima")
        self.tienda = crear_tienda(self.cliente)
        self.pedido = crear_pedido(self.cliente, self.tienda, estado="EN_TRANSITO")
        self.caja = Paquete.objects.create(pedido=self.pedido, numero=1, peso_kg=4, carrier="noventa9Minutos", estado=Paquete.DESPACHADO)
        self.guia = Guia.objects.create(pedido=self.pedido, paquete=self.caja, carrier="noventa9Minutos", numero="1865484451",
                                        proveedor="envia", estado=Guia.INTENTO_FALLIDO)
        self.dia1 = timezone.make_aware(datetime(2026, 10, 6, 14, 10))
        self.dia2 = timezone.make_aware(datetime(2026, 10, 8, 11, 32))
        EventoGuia.objects.create(guia=self.guia, estado="EN_RUTA", crudo="OUT_FOR_DELIVERY", descripcion="En ruta", ts_carrier=self.dia1 - timedelta(hours=3))
        EventoGuia.objects.create(guia=self.guia, estado="INTENTO_FALLIDO", crudo="DELIVERY_ATTEMPT", descripcion="Intento fallido: nadie en el domicilio", ts_carrier=self.dia1)
        EventoGuia.objects.create(guia=self.guia, estado="INTENTO_FALLIDO", crudo="NOT_DELIVERED", descripcion="No entregado", ts_carrier=self.dia1 + timedelta(minutes=20))  # mismo día: un solo intento
        EventoGuia.objects.create(guia=self.guia, estado="", crudo="STORED", descripcion="Reingreso a la estación MX14", ts_carrier=self.dia1 + timedelta(hours=5))
        EventoGuia.objects.create(guia=self.guia, estado="INTENTO_FALLIDO", crudo="DELIVERY_ATTEMPT", descripcion="Intento fallido: dirección incompleta", ts_carrier=self.dia2)

    def _mesa(self):
        mesa = get_user_model().objects.create_user("mesa1", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)

    def test_un_intento_por_dia_y_nada_si_el_carrier_no_reporta(self):
        intentos = intentos_de_entrega(self.guia)
        self.assertEqual([(ts, texto) for ts, texto in intentos], [(self.dia1, "Intento fallido: nadie en el domicilio"), (self.dia2, "Intento fallido: dirección incompleta")])
        sin = Guia.objects.create(pedido=self.pedido, carrier="estafeta", numero="EST-9", estado=Guia.EN_TRANSITO)
        EventoGuia.objects.create(guia=sin, estado="EN_TRANSITO", crudo="IN_TRANSIT", descripcion="En tránsito", ts_carrier=self.dia1)
        self.assertEqual(intentos_de_entrega(sin), [])
        self.assertEqual(intentos_de_entrega(None), [])
        self.assertEqual(len(guias_del_pedido(self.pedido)[0]["intentos"]), 2)

    def test_se_ven_en_linea_de_tiempo_cajas_rastreo_e_incidencia(self):
        from apps.rastreo.views import _paquetes

        fila = construir(Pedido.objects.filter(pk=self.pedido.pk))[0]
        self.assertEqual([ts for ts, _ in fila["intentos"]], [self.dia1, self.dia2])
        pasos = _paquetes(self.pedido)[0]["pasos"]
        nombres = [p["nombre"] for p in pasos]
        self.assertEqual(nombres.count("Intento de entrega"), 2)
        self.assertLess(nombres.index("En reparto"), nombres.index("Intento de entrega"))
        self.assertLess(nombres.index("Intento de entrega"), nombres.index("Entregado"))
        intentos = [p for p in pasos if p["clave"] == "intento"]
        self.assertTrue(all(p["hecho"] for p in intentos))
        self.assertTrue(intentos[-1]["actual"])  # el último intento es lo último que pasó
        self._mesa()
        html = self.client.get(reverse("mesa:pedido_detalle", args=[self.pedido.pk])).content.decode()
        self.assertIn("2 intentos de entrega · 06/Oct, 08/Oct", html)
        html = self.client.get(reverse("mesa:linea_tiempo")).content.decode()
        self.assertIn("2 intentos de entrega · 06/Oct, 08/Oct", html)
        inc = abrir_incidencia(self.cliente, Incidencia.TIPO_RF, Incidencia.ORIGEN_AUTO, pedido=self.pedido, texto="Intento fallido")
        html = self.client.get(reverse("mesa:incidencia_detalle", args=[inc.pk])).content.decode()
        self.assertIn("2 intentos de entrega · 06/Oct, 08/Oct", html)
