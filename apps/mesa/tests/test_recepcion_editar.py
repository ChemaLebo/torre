"""Editor de recepción en Mesa (Chema 2026-10-06; por escaneo desde
2026-10-13): la tabla Realidad por producto · lote · tarima y sus
correcciones (contadas/dañadas por tarima, lote, mover, dañadas, agregar
producto, cerrar, reiniciar), todas con motivo y en el historial; el portal
ve dónde quedó cada caja, solo lectura."""
from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.catalogo.models import Lote, Ubicacion
from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.inventario.models import EscaneoRecepcion, LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import confirmar_tarima, contar_caja, realidad_recepcion
from apps.piso.tests.base import PisoTestCase


class EditorRecepcionTests(PisoTestCase):
    """Reproduce una recepción real: 144 cajas anunciadas, 106 contadas y
    recibidas en dos tarimas, 38 contadas en una tercera sin confirmar."""

    def setUp(self):
        self.sku.requiere_lote = True
        self.sku.save()
        self.res = Ubicacion.objects.create(codigo="RES-1-D-F-1", tipo=Ubicacion.RESERVA)
        self.t1 = Ubicacion.objects.create(codigo="TAR-01", tipo=Ubicacion.RESERVA)
        self.t2 = Ubicacion.objects.create(codigo="TAR-02", tipo=Ubicacion.RESERVA)
        self.t3 = Ubicacion.objects.create(codigo="TAR-03", tipo=Ubicacion.RESERVA)
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=3)
        self.linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=144, lote_codigo="CEX27ABC", fecha_caducidad=date(2027, 1, 31))
        for tarima, n in ((self.t1, 68), (self.t2, 38)):
            for _ in range(n):
                contar_caja(self.orden, self.sku, "CEX27ABC", None, tarima, self.operador)
            confirmar_tarima(self.orden, tarima, self.operador)
        for _ in range(38):
            contar_caja(self.orden, self.sku, "CEX27ABC", None, self.t3, self.operador)
        mesa = get_user_model().objects.create_user("mesa-edita", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.url = reverse("mesa:recepcion_editar", args=[self.orden.pk])

    def _post(self, **datos):
        return self.client.post(self.url, datos, follow=True)

    def _en_recepcion(self):
        return sorted(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY, cantidad__gt=0).values_list("ubicacion__codigo", "cantidad"))

    def _fila(self):
        return next(f for f in realidad_recepcion(self.orden) if f["sku"] == self.sku)

    def test_realidad_y_pantalla(self):
        f = self._fila()
        self.assertEqual((f["anunciadas"], f["contadas"], f["recibidas"], f["por_confirmar"], f["diferencia"]), (144, 144, 106, 38, 0))
        self.assertEqual([(t["tarima"].codigo, t["recibidas"], t["por_confirmar"]) for t in f["tarimas"]], [("TAR-01", 68, 0), ("TAR-02", 38, 0), ("TAR-03", 0, 38)])
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Editar recepción")
        self.assertContains(respuesta, "38 contadas sin confirmar")
        self.assertContains(respuesta, '<option value="RES-1-D-F-1">RES-1-D-F-1</option>')
        self.assertContains(respuesta, 'name="accion" value="mover"')
        self.assertNotContains(respuesta, "Exportar plan")
        self.assertNotContains(respuesta, 'value="cerrar"')  # hay cajas sin confirmar
        self.assertContains(respuesta, "Exportar realidad (CSV)")

    def test_conteo_por_tarima_sube_y_baja_con_stock(self):
        respuesta = self._post(accion="conteo", linea_id=self.linea.pk, tarima="TAR-02", contadas=40, danadas=1, motivo="recontamos")
        self.assertContains(respuesta, "COLIMITA-SIX lote CEX27ABC en TAR-02: contadas +2, dañadas +1")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (108, 1))
        self.assertEqual(self._en_recepcion(), [("TAR-01", 68), ("TAR-02", 40)])
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 1)
        respuesta = self._post(accion="conteo", linea_id=self.linea.pk, tarima="TAR-02", contadas=35, danadas=0, motivo="eran menos")
        self.assertContains(respuesta, "contadas -5, dañadas -1")
        self.assertEqual(self._en_recepcion(), [("TAR-01", 68), ("TAR-02", 35)])
        self.assertTrue(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RECEPCION, delta=-1, referencia=self.orden.folio).exists())
        self.assertEqual(EscaneoRecepcion.objects.filter(orden=self.orden, estado=EscaneoRecepcion.ANULADA).count(), 6)
        evento = EventoAuditoria.objects.filter(entidad_id=self.orden.folio, accion="recepcion_corregida").latest("ts")
        self.assertEqual((evento.delta["tarima"], evento.delta["contadas"], evento.delta["danadas"]), ("TAR-02", [40, 35], [1, 0]))
        # Cajas en una posición nueva para la línea (no escaneadas en el piso).
        respuesta = self._post(accion="conteo", linea_id=self.linea.pk, tarima="RES-1-D-F-1", contadas=4, danadas=0, motivo="estaban en el rack")
        self.assertContains(respuesta, "en RES-1-D-F-1: contadas +4")
        self.assertEqual(self._en_recepcion(), [("RES-1-D-F-1", 4), ("TAR-01", 68), ("TAR-02", 35)])
        respuesta = self._post(accion="conteo", linea_id=self.linea.pk, tarima="TAR-02", contadas=1, danadas=0, motivo="")
        self.assertContains(respuesta, "Di por qué se corrige")

    def test_mover_y_danar(self):
        kardex = Movimiento.objects.filter(sku=self.sku).count()
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="TAR-02", destino="RES-1-D-F-1", cantidad=10, motivo="pasaron al rack")
        self.assertContains(respuesta, "10 de COLIMITA-SIX lote CEX27ABC: TAR-02 → RES-1-D-F-1")
        self.assertEqual(self._en_recepcion(), [("RES-1-D-F-1", 10), ("TAR-01", 68), ("TAR-02", 28)])
        self.assertEqual(Movimiento.objects.filter(sku=self.sku).count(), kardex)  # mover no es kardex: el estado no cambia
        f = self._fila()
        self.assertEqual({t["tarima"].codigo: t["recibidas"] for t in f["tarimas"]}, {"TAR-01": 68, "TAR-02": 28, "RES-1-D-F-1": 10, "TAR-03": 0})
        respuesta = self._post(accion="danar", linea_id=self.linea.pk, origen="TAR-01", cantidad=3, motivo="rotas al bajar")
        self.assertContains(respuesta, "3 de COLIMITA-SIX lote CEX27ABC marcadas dañadas")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (103, 3))
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 3)
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="TAR-02", destino="RES-1-D-F-1", cantidad=99, motivo="x")
        self.assertContains(respuesta, "solo hay 28")

    def test_editar_linea_y_agregar_producto_con_cajas(self):
        from apps.catalogo.models import SKU

        respuesta = self._post(accion="linea", linea_id=self.linea.pk, lote="CEX27ABC", caducidad="2027-06-12", anunciadas=150, motivo="caducidad corregida")
        self.assertContains(respuesta, "Línea de COLIMITA-SIX corregida")
        self.linea.refresh_from_db()
        self.assertEqual((str(self.linea.fecha_caducidad), self.linea.cantidad_anunciada), ("2027-06-12", 150))
        self.assertEqual(str(Lote.objects.get(sku=self.sku, codigo="CEX27ABC").fecha_caducidad), "2027-06-12")
        otro = SKU.objects.create(cliente=self.cliente, codigo="BP355", codigo_barras="7500000000355", descripcion="Botella 355", peso_gr=500, requiere_lote=True)
        respuesta = self._post(accion="agregar", sku_id=otro.pk, lote="L2603011", caducidad="2027-06-12", anunciadas=0, contadas=24, danadas=1, tarima="TAR-03", motivo="llegó sin anunciar")
        self.assertContains(respuesta, "BP355 lote L2603011 agregado")
        nueva = LineaASN.objects.get(orden=self.orden, sku=otro)
        self.assertEqual((nueva.cantidad_anunciada, nueva.cantidad_recibida, nueva.cantidad_danada, str(nueva.fecha_caducidad)), (0, 24, 1, "2027-06-12"))
        self.assertEqual(Saldo.objects.get(sku=otro, estado=Saldo.EN_PUTAWAY).ubicacion.codigo, "TAR-03")
        self.assertEqual(str(Lote.objects.get(sku=otro, codigo="L2603011").fecha_caducidad), "2027-06-12")
        filas = realidad_recepcion(self.orden)
        self.assertEqual([(f["sku"].codigo, f["diferencia"]) for f in filas], [("BP355", 25), ("COLIMITA-SIX", -6)])
        respuesta = self._post(accion="agregar", sku_id=otro.pk, lote="L2603011", anunciadas=0, motivo="x")
        self.assertContains(respuesta, "ya tiene una línea con ese lote")

    def test_csv_cerrar_y_solo_lectura_y_portal(self):
        respuesta = self.client.get(reverse("mesa:recepcion_realidad_csv", args=[self.orden.pk]))
        cuerpo = respuesta.content.decode("utf-8-sig")
        self.assertEqual(cuerpo.splitlines()[0], "sku,nombre,codigo_barras,lote,caducidad,posicion,estado,piezas")
        self.assertIn("COLIMITA-SIX,Colimita six pack,7501234567890,CEX27ABC,2027-01-31,TAR-01,recibida,68", cuerpo)
        self.assertIn(",TAR-03,contada sin confirmar,38", cuerpo)
        # Con cajas sin confirmar no se cierra; confirmada la tarima, sí.
        respuesta = self._post(accion="cerrar", tarimas_recibidas=3)
        self.assertContains(respuesta, "sin confirmar en TAR-03")
        confirmar_tarima(self.orden, self.t3, self.operador)
        respuesta = self._post(accion="cerrar", tarimas_recibidas=3)
        self.assertContains(respuesta, "cerrada: todo lo recibido quedó vendible")
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.tarimas_recibidas), (OrdenEntrada.CERRADA, 3))
        self.assertEqual(sorted(Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).values_list("ubicacion__codigo", "cantidad")), [("TAR-01", 68), ("TAR-02", 38), ("TAR-03", 38)])
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "solo lectura")
        self.assertNotContains(respuesta, 'name="accion" value="mover"')
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="TAR-01", destino="RES-1-D-F-1", cantidad=1, motivo="x")
        self.assertContains(respuesta, "ya está cerrada")
        # Mesa → Recepciones enlaza al editor; el portal ve dónde quedó, sin acciones.
        respuesta = self.client.get(reverse("mesa:recepciones"), {"cliente": self.cliente.slug})
        self.assertContains(respuesta, f'href="{self.url}">Ver realidad</a>')
        self.assertNotContains(respuesta, "Plan de acomodo")
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, "Dónde quedó")
        self.assertContains(respuesta, "TAR-02")
        self.assertNotContains(respuesta, "Editar recepción")
        self.assertNotContains(respuesta, "Reiniciar recepción")

    def test_reiniciar_desde_la_lista(self):
        respuesta = self.client.get(reverse("mesa:recepciones"), {"cliente": self.cliente.slug})
        self.assertContains(respuesta, "Reiniciar recepción (conteos a cero)")
        respuesta = self.client.post(reverse("mesa:recepciones"), {"accion": "reiniciar_recepcion", "orden_id": self.orden.pk}, follow=True)
        self.assertContains(respuesta, "144 caja(s) anuladas, 106 regresaron de su tarima")
        self.assertEqual(self._en_recepcion(), [])
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertEqual(self._fila()["contadas"], 0)
