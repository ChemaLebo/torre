"""Servicio crear_pedido_manual: alta desde Mesa para clientes sin Shopify.

Mismo contrato operativo que la ingesta: reserva por línea (mockeada, como en
test_servicios), incidencia FAL sin stock, es_local por CP, corte vigente y
plantilla A solo si hay teléfono (camino real de mensajeria, sin mock).
"""
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase

from apps.catalogo.models import SKU
from apps.core.models import Cliente, EventoAuditoria
from apps.mensajeria.models import NotificacionEnviada
from apps.pedidos import services
from apps.pedidos.models import Pedido


def direccion_manual(cp="01780"):
    """Mismo shape que el shipping_address de Shopify (contrato del servicio)."""
    return {
        "name": "Karina Ordaz",
        "address1": "Av. Torres de Ixtapantongo 380",
        "address2": "Olivar de los Padres",
        "city": "Ciudad de México",
        "province": "CDMX",
        "zip": cp,
        "country": "México",
        "phone": "+525512345678",
    }


class BasePedidoManual(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.sku = SKU.objects.create(
            cliente=cls.cliente,
            codigo="COL-SIX",
            descripcion="Colimita six pack",
            peso_gr=2400,
            unidad="six",
            precio_declarado=Decimal("189.00"),
        )
        cls.sku_caja = SKU.objects.create(
            cliente=cls.cliente,
            codigo="COL-C12",
            descripcion="Colimita caja 12",
            peso_gr=4800,
            unidad="caja",
            precio_declarado=Decimal("350.00"),
        )
        cls.otro_cliente = Cliente.objects.create(nombre="Mezcal Nocturno", slug="mezcal-nocturno")
        cls.sku_ajeno = SKU.objects.create(
            cliente=cls.otro_cliente,
            codigo="MN-750",
            descripcion="Mezcal 750 ml",
            peso_gr=1300,
            precio_declarado=Decimal("620.00"),
        )

    def _crear(self, reservar_ok=True, **kwargs):
        datos = {
            "comprador_nombre": "Karina Ordaz",
            "comprador_tel": "+525512345678",
            "comprador_email": "karina@example.com",
            "direccion": direccion_manual(),
            "cp": "01780",
            "lineas": [(self.sku, 2)],
            "actor": None,
        }
        datos.update(kwargs)
        # captureOnCommitCallbacks: la plantilla A sale en transaction.on_commit
        # (jamás dentro del atomic del alta) — aquí se ejecuta el commit
        # simulado para que el camino real de mensajeria sí corra.
        with patch("apps.inventario.services.reservar", return_value=reservar_ok) as reservar, \
             patch("apps.incidencias.services.abrir_incidencia") as abrir, \
             self.captureOnCommitCallbacks(execute=True):
            pedido = services.crear_pedido_manual(self.cliente, **datos)
        return pedido, reservar, abrir


class CrearPedidoManualTests(BasePedidoManual):
    def test_alta_feliz_crea_pedido_con_reserva(self):
        pedido, reservar, abrir = self._crear()
        self.assertTrue(pedido.folio.startswith("PED-"))
        self.assertEqual(pedido.origen, "manual")
        self.assertIsNone(pedido.tienda)
        self.assertEqual(pedido.shopify_order_id, "")
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertEqual(pedido.cliente, self.cliente)
        self.assertEqual(pedido.direccion["address1"], "Av. Torres de Ixtapantongo 380")
        linea = pedido.lineas.get()
        self.assertEqual(linea.sku, self.sku)
        self.assertEqual(linea.cantidad, 2)
        self.assertTrue(linea.reservada)
        reservar.assert_called_once_with(self.sku, 2, pedido.folio)
        abrir.assert_not_called()
        self.assertFalse(pedido.incidencia_activa)

    def test_peso_esperado_y_corte_vigente(self):
        pedido, *_ = self._crear(lineas=[(self.sku, 2), (self.sku_caja, 1)])
        self.assertEqual(pedido.peso_esperado_gr, 2 * 2400 + 4800)
        hora, minuto = str(settings.TORRE["CORTE_CONTRACTUAL"]).split(":")
        self.assertIsNotNone(pedido.corte_vigente_al_ingreso)
        self.assertEqual(pedido.corte_vigente_al_ingreso.hour, int(hora))
        self.assertEqual(pedido.corte_vigente_al_ingreso.minute, int(minuto))

    def test_es_local_por_cp(self):
        local, *_ = self._crear(cp="01780", direccion=direccion_manual("01780"))
        foraneo, *_ = self._crear(cp="28017", direccion=direccion_manual("28017"))
        self.assertTrue(local.es_local)
        self.assertFalse(foraneo.es_local)

    def test_valor_declarado_default_suma_del_catalogo(self):
        pedido, *_ = self._crear(lineas=[(self.sku, 2), (self.sku_caja, 1)])
        # 2 × 189.00 + 1 × 350.00
        self.assertEqual(pedido.valor_declarado, Decimal("728.00"))

    def test_valor_declarado_explicito_se_respeta(self):
        pedido, *_ = self._crear(valor_declarado=Decimal("1500.00"))
        self.assertEqual(pedido.valor_declarado, Decimal("1500.00"))

    def test_evento_alta_manual_con_lineas(self):
        pedido, *_ = self._crear()
        evento = EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=pedido.folio, accion="alta_manual",
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.cliente, self.cliente)
        self.assertEqual(evento.delta["lineas"], [{"sku": "COL-SIX", "cantidad": 2}])

    def test_plantilla_a_solo_con_telefono(self):
        con_tel, *_ = self._crear()
        self.assertTrue(
            NotificacionEnviada.objects.filter(
                plantilla_clave="A", referencia=con_tel.folio,
            ).exists()
        )
        sin_tel, *_ = self._crear(comprador_tel="")
        self.assertFalse(
            NotificacionEnviada.objects.filter(
                plantilla_clave="A", referencia=sin_tel.folio,
            ).exists()
        )

    def test_sin_stock_linea_sin_reservar_e_incidencia_fal(self):
        pedido, reservar, abrir = self._crear(reservar_ok=False)
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertTrue(pedido.incidencia_activa)
        linea = pedido.lineas.get()
        self.assertFalse(linea.reservada)
        abrir.assert_called_once()
        self.assertEqual(abrir.call_args[0][1], "FAL")

    def test_sku_de_otro_cliente_truena(self):
        with self.assertRaises(ValueError) as ctx:
            self._crear(lineas=[(self.sku_ajeno, 1)])
        self.assertIn("MN-750", str(ctx.exception))
        self.assertEqual(Pedido.objects.count(), 0)

    def test_cantidad_cero_truena(self):
        with self.assertRaises(ValueError):
            self._crear(lineas=[(self.sku, 0)])
        self.assertEqual(Pedido.objects.count(), 0)

    def test_sin_lineas_truena(self):
        with self.assertRaises(ValueError):
            self._crear(lineas=[])
        self.assertEqual(Pedido.objects.count(), 0)

    def test_planificacion_fallida_no_truena_el_alta(self):
        with patch(
            "apps.envios.cotizador.planificar_envio",
            side_effect=ValueError("Ningún carrier cotizó. Revisar con Mesa de Control."),
        ) as planificar:
            pedido, *_ = self._crear()
        planificar.assert_called_once()
        self.assertEqual(pedido.estado, Pedido.PENDIENTE)
        self.assertTrue(Pedido.objects.filter(pk=pedido.pk).exists())

    def test_planificacion_sale_en_on_commit_jamas_dentro_del_atomic(self):
        """Cotizar un lane frío pega a la API real: primero el commit (suelta
        los locks de Saldo), luego el plan."""
        datos = {
            "comprador_nombre": "Karina Ordaz",
            "direccion": direccion_manual(),
            "cp": "01780",
            "lineas": [(self.sku, 1)],
            "actor": None,
        }
        with patch("apps.inventario.services.reservar", return_value=True), \
             patch("apps.envios.cotizador.planificar_envio") as planificar:
            with self.captureOnCommitCallbacks(execute=True):
                services.crear_pedido_manual(self.cliente, **datos)
                planificar.assert_not_called()  # aún dentro del atomic
        planificar.assert_called_once()
