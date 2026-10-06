"""Compensaciones que ejecutan (Chema 2026-09-28): reposición (regresa el
pedido a picking con los line items elegidos), reembolso (refund en Shopify)
y cupón (registro). Aquí se prueba el servicio; la reposición real vive en
pedidos.tests.test_reposicion y el refund en integraciones.tests.test_reembolso."""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.catalogo.models import SKU
from apps.incidencias.models import Compensacion, Incidencia, MensajeIncidencia
from apps.incidencias.services import (
    abrir_incidencia, aprobar_compensacion, crear_compensacion, ejecutar_reembolso, opciones_compensacion,
    resumen_ejecucion, seleccion_desde_post,
)
from apps.integraciones.shopify import ShopifyError
from apps.pedidos.models import LineaPedido

from .utils import crear_cliente, crear_pedido


class CompensacionesTests(TestCase):
    def setUp(self):
        self.cliente = crear_cliente()
        self.pedido = crear_pedido(self.cliente)
        self.pedido.estado = "ENTREGADO"  # se repone lo que ya salió (anterior al parcial: todo el pedido)
        self.pedido.save(update_fields=["estado"])
        self.six = SKU.objects.create(cliente=self.cliente, codigo="SIX", descripcion="Six", precio_declarado=Decimal("300"))
        self.caja = SKU.objects.create(cliente=self.cliente, codigo="C12", descripcion="Caja 12", precio_declarado=Decimal("600"))
        self.l1 = LineaPedido.objects.create(pedido=self.pedido, sku=self.six, cantidad=2, precio_unitario=Decimal("250"))
        self.l2 = LineaPedido.objects.create(pedido=self.pedido, sku=self.caja, cantidad=1)
        self.inc = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, pedido=self.pedido, texto="Rota")

    def test_opciones_segun_el_tipo_de_incidencia(self):
        self.assertEqual([t for t, _ in opciones_compensacion(self.inc)], ["reposicion", "reembolso", "cupon"])
        # Cualquier tipo con pedido repone o reembolsa (Chema 2026-09-28).
        retraso = abrir_incidencia(self.cliente, Incidencia.TIPO_RET, Incidencia.ORIGEN_AUTO, pedido=self.pedido, texto="x")
        self.assertEqual([t for t, _ in opciones_compensacion(retraso)], ["reposicion", "reembolso", "cupon"])
        suelta = abrir_incidencia(self.cliente, Incidencia.TIPO_DAN, Incidencia.ORIGEN_CLIENTE, texto="sin pedido")
        self.assertEqual([t for t, _ in opciones_compensacion(suelta)], ["cupon"])

    def test_seleccion_desde_el_formulario(self):
        post = {f"linea_{self.l1.pk}": "1", f"cantidad_{self.l1.pk}": "1", f"cantidad_{self.l2.pk}": "1"}
        self.assertEqual(seleccion_desde_post(post, self.inc), [(self.l1, 1, None)])
        self.assertEqual(seleccion_desde_post({f"linea_{self.l2.pk}": "1"}, self.inc), [(self.l2, 1, None)])  # sin cantidad: todas
        # Reposición: la caja de la que salieron las piezas viaja en caja_<pk>.
        self.assertEqual(seleccion_desde_post({f"linea_{self.l1.pk}": "1", f"caja_{self.l1.pk}": "7"}, self.inc), [(self.l1, 2, 7)])
        with self.assertRaises(ValueError):
            seleccion_desde_post({f"linea_{self.l1.pk}": "1", f"cantidad_{self.l1.pk}": "dos"}, self.inc)

    def test_reposicion_cotiza_con_el_catalogo_y_al_aprobar_repone(self):
        comp = crear_compensacion(self.inc, "reposicion", None, "cliente", lineas=[(self.l1, 1), (self.l2, 1)])
        self.assertEqual((comp.estado, comp.monto, comp.creada_por), (Compensacion.COTIZADA, Decimal("900"), "cliente"))
        self.assertEqual(comp.lineas, [{"linea_id": self.l1.pk, "sku": "SIX", "cantidad": 1, "caja_id": None, "caja": None},
                                       {"linea_id": self.l2.pk, "sku": "C12", "cantidad": 1, "caja_id": None, "caja": None}])
        self.assertEqual(comp.resumen_lineas, "1× SIX, 1× C12")
        ultimo = self.inc.mensajes.order_by("-pk").first()
        self.assertEqual((ultimo.rol_autor, ultimo.texto), (MensajeIncidencia.ROL_CLIENTE, "Propuso reposición física: 1× SIX, 1× C12."))
        with patch("apps.pedidos.services.reponer_lineas", return_value=[MagicMock(cantidad=1), MagicMock(cantidad=1)]) as reponer:
            aprobar_compensacion(comp, None)
        reponer.assert_called_once()
        args, kwargs = reponer.call_args
        self.assertEqual((args[0], args[1], kwargs["incidencia"]), (self.pedido, [(self.l1, 1, None), (self.l2, 1, None)], self.inc))
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.assertIn("regresó a picking con 2 pieza(s)", comp.nota)
        self.assertTrue(resumen_ejecucion(comp).startswith("Reposición aprobada"))

    def test_reposicion_que_no_se_puede_hacer_no_queda_aprobada(self):
        comp = crear_compensacion(self.inc, "reposicion", None, "mesa", lineas=[(self.l1, 1)])
        with patch("apps.pedidos.services.reponer_lineas", side_effect=ValueError("aún en bodega")):
            with self.assertRaises(ValueError):
                aprobar_compensacion(comp, None)
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.COTIZADA)

    def test_validaciones(self):
        suelta = abrir_incidencia(self.cliente, Incidencia.TIPO_DES, Incidencia.ORIGEN_AUTO, texto="sin pedido")
        casos = [
            (suelta, "reposicion", {"lineas": [(self.l1, 1)]}),           # sin pedido no aplica
            (self.inc, "reposicion", {}),                                  # sin líneas
            (self.inc, "reposicion", {"lineas": [(self.l1, 3)]}),          # más piezas que las pedidas
            (self.inc, "cupon", {}),                                       # cupón sin monto
            (self.inc, "reembolso", {}),                                   # nada que reembolsar
            (self.inc, "cupon", {"monto": "abc"}),                         # monto inválido
        ]
        for incidencia, tipo, extra in casos:
            with self.assertRaises(ValueError, msg=(tipo, extra)):
                crear_compensacion(incidencia, tipo, None, "mesa", **extra)
        self.assertFalse(Compensacion.objects.exists())

    def test_reposicion_solo_de_lo_que_salio_en_una_caja(self):
        """Candado por caja (Chema 2026-09-30): PED-00051 con la ola 1 en la
        calle y la ola 2 PENDIENTE por falta de stock: se repone lo de la
        caja que salió, con tope en sus piezas; lo de bodega se corrige."""
        from apps.incidencias.services import lineas_para_compensar

        self.pedido.estado = "PENDIENTE"
        self.pedido.save(update_fields=["estado"])
        LineaPedido.objects.filter(pk=self.l1.pk).update(cantidad_despachada=1)
        lineas = lineas_para_compensar(self.inc)
        self.assertEqual([(l.sku.codigo, l.salieron) for l in lineas], [("SIX", 1), ("C12", 0)])
        with self.assertRaises(ValueError) as ctx:
            crear_compensacion(self.inc, "reposicion", None, "mesa", lineas=[(self.l2, 1)])
        self.assertIn("no ha salido de bodega", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            crear_compensacion(self.inc, "reposicion", None, "mesa", lineas=[(self.l1, 2)])
        self.assertIn("salieron 1 pieza(s)", str(ctx.exception))
        comp = crear_compensacion(self.inc, "reposicion", None, "mesa", lineas=[(self.l1, 1)])
        self.assertEqual(comp.lineas, [{"linea_id": self.l1.pk, "sku": "SIX", "cantidad": 1, "caja_id": None, "caja": None}])

    def test_reembolso_lo_ejecuta_shopify_y_queda_pagado(self):
        comp = crear_compensacion(self.inc, "reembolso", None, "mesa", lineas=[(self.l1, 2)], reembolsar_envio=True)
        self.assertEqual(comp.monto, Decimal("500"))  # estimado con el precio real de venta
        with patch("apps.integraciones.services.reembolsar_en_shopify",
                   return_value=("gid://shopify/Refund/9", Decimal("589.00"))) as refund:
            aprobar_compensacion(comp, None)
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["lineas"], [("SIX", 2)])
        self.assertTrue(refund.call_args.kwargs["reembolsar_envio"])
        self.assertIsNone(refund.call_args.kwargs["monto"])
        comp.refresh_from_db()
        self.assertEqual((comp.estado, comp.referencia_pago, comp.monto), (Compensacion.PAGADA, "gid://shopify/Refund/9", Decimal("589.00")))
        self.assertIsNotNone(comp.fecha_pago)
        self.assertEqual(resumen_ejecucion(comp), "Reembolso de $589.00 hecho en Shopify (gid://shopify/Refund/9).")

    def test_reembolso_rechazado_queda_aprobado_con_nota_y_se_reintenta(self):
        comp = crear_compensacion(self.inc, "reembolso", None, "mesa", lineas=[(self.l1, 1)])
        with patch("apps.integraciones.services.reembolsar_en_shopify", side_effect=ShopifyError("sin fondos")):
            aprobar_compensacion(comp, None)
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.APROBADA)
        self.assertIn("Shopify rechazó el reembolso: sin fondos", comp.nota)
        self.assertIn("no se ejecutó", resumen_ejecucion(comp))
        with patch("apps.integraciones.services.reembolsar_en_shopify", return_value=("gid://shopify/Refund/1", Decimal("250"))):
            ejecutar_reembolso(comp, None)
        comp.refresh_from_db()
        self.assertEqual(comp.estado, Compensacion.PAGADA)

    def test_reembolso_de_monto_libre_y_sin_tienda(self):
        comp = crear_compensacion(self.inc, "reembolso", None, "cliente", monto="150")
        self.assertEqual((comp.monto, comp.lineas), (Decimal("150"), []))
        with patch("apps.integraciones.services.reembolsar_en_shopify", return_value=("gid://shopify/Refund/2", Decimal("150"))) as refund:
            aprobar_compensacion(comp, None, "cliente")
        self.assertEqual((refund.call_args.kwargs["lineas"], refund.call_args.kwargs["monto"]), ([], Decimal("150")))
        self.assertEqual(self.inc.mensajes.order_by("-pk").first().rol_autor, MensajeIncidencia.ROL_CLIENTE)
        self.pedido.tienda = None
        self.pedido.save(update_fields=["tienda"])
        manual = crear_compensacion(self.inc, "reembolso", None, "mesa", monto="80", aprobar=True)
        self.assertEqual(manual.estado, Compensacion.APROBADA)
        self.assertIn("Sin tienda de Shopify", manual.nota)
        manual.referencia_pago = "SPEI 123"
        manual.save(update_fields=["referencia_pago"])
        manual.transicionar(Compensacion.PAGADA)
        self.assertEqual(manual.estado, Compensacion.PAGADA)
