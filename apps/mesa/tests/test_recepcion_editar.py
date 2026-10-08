"""Editor de recepción en Mesa (Chema 2026-10-06): la tabla Realidad y sus
correcciones (conteos, lote, mover, regresar, acomodar, dañadas, agregar
producto, cerrar), todas con motivo y en el historial; el portal ve dónde
quedó cada cosa, solo lectura."""
from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.catalogo.models import Lote, Ubicacion
from apps.core.models import EventoAuditoria, PerfilUsuario
from apps.inventario.models import LineaASN, Movimiento, OrdenEntrada, Saldo
from apps.inventario.services import realidad_recepcion, recibir_y_ubicar
from apps.piso.tests.base import PisoTestCase


class EditorRecepcionTests(PisoTestCase):
    """Reproduce el ASN-0007 de hoy: 144 contadas, 38+30+38 en tres anaqueles y 38 a cuarentena por falta de zona de desborde."""

    def setUp(self):
        self.sku.requiere_lote = True
        self.sku.save()
        self.a1 = Ubicacion.objects.create(codigo="PIC-4-I-F-1", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.a2 = Ubicacion.objects.create(codigo="PIC-3-I-F-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.a3 = Ubicacion.objects.create(codigo="PIC-2-D-B-1", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.tar = Ubicacion.objects.create(codigo="TAR-04", tipo=Ubicacion.RESERVA)
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=2)
        self.linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=144, lote_codigo="CEX27ABC", fecha_caducidad=date(2027, 1, 31))
        recibir_y_ubicar(self.orden, self.sku, [{
            "linea": self.linea, "lote_codigo": "CEX27ABC", "fecha_caducidad": date(2027, 1, 31), "contadas": 144, "danadas": 0,
            "acomodos": [(self.a1, 38), (self.a2, 30), (self.a3, 38), (None, 38)],
        }], self.operador)
        mesa = get_user_model().objects.create_user("mesa-edita", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        self.url = reverse("mesa:recepcion_editar", args=[self.orden.pk])

    def _post(self, **datos):
        return self.client.post(self.url, datos, follow=True)

    def _vendible(self):
        return sorted(Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE, cantidad__gt=0).values_list("ubicacion__codigo", "cantidad"))

    def test_realidad_y_pantalla(self):
        filas = realidad_recepcion(self.orden)
        f = filas[0]
        self.assertEqual((f["contadas"], f["acomodadas"], f["en_recepcion"], f["cuarentena_sin_espacio"], f["cuadra"]), (144, 106, 0, 38, True))
        self.assertEqual([(p["ubicacion"].codigo, p["cantidad"]) for p in f["posiciones"]], [("PIC-2-D-B-1", 38), ("PIC-3-I-F-2", 30), ("PIC-4-I-F-1", 38)])
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Editar recepción")
        self.assertContains(respuesta, "cuarentena sin espacio")
        self.assertContains(respuesta, '<option value="TAR-04">TAR-04</option>')
        self.assertContains(respuesta, 'name="accion" value="mover"')
        self.assertNotContains(respuesta, "SKU sin cuadrar")
        self.assertNotContains(respuesta, 'value="cerrar"')  # hay 38 por resolver
        self.assertContains(respuesta, "Exportar realidad (CSV)")

    def test_las_38_vuelven_y_se_acomodan_en_la_tarima(self):
        respuesta = self._post(accion="de_cuarentena", linea_id=self.linea.pk, cantidad=38, motivo="nunca se acomodaron")
        self.assertContains(respuesta, "38 de COLIMITA-SIX regresaron de cuarentena a recepción")
        self.assertEqual(Saldo.objects.filter(sku=self.sku, estado=Saldo.CUARENTENA, cantidad__gt=0).count(), 0)
        f = realidad_recepcion(self.orden)[0]
        self.assertEqual((f["en_recepcion"], f["cuarentena_sin_espacio"], f["cuadra"]), (38, 0, True))
        respuesta = self._post(accion="acomodar", linea_id=self.linea.pk, cantidad=38, destino="TAR-04", motivo="a la tarima 4")
        self.assertContains(respuesta, "38 de COLIMITA-SIX lote CEX27ABC acomodadas en TAR-04")
        self.assertEqual(self._vendible(), [("PIC-2-D-B-1", 38), ("PIC-3-I-F-2", 30), ("PIC-4-I-F-1", 38), ("TAR-04", 38)])
        self.assertEqual(Saldo.objects.get(ubicacion=self.tar).lote.codigo, "CEX27ABC")
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.plan_acomodo["pasos"], [])  # ya no falta nada
        self.assertContains(self.client.get(self.url), 'value="cerrar"')
        tipos = [e.delta["tipo"] for e in EventoAuditoria.objects.filter(entidad="asn", entidad_id=self.orden.folio, accion="recepcion_corregida").order_by("ts")]
        self.assertEqual(tipos, ["de_cuarentena", "acomodar"])

    def test_mover_regresar_y_danar(self):
        kardex = Movimiento.objects.filter(sku=self.sku).count()
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="PIC-3-I-F-2", destino="TAR-04", cantidad=10, motivo="se ubicó mal")
        self.assertContains(respuesta, "10 de COLIMITA-SIX lote CEX27ABC: PIC-3-I-F-2 → TAR-04")
        self.assertEqual(self._vendible(), [("PIC-2-D-B-1", 38), ("PIC-3-I-F-2", 20), ("PIC-4-I-F-1", 38), ("TAR-04", 10)])
        self.assertEqual(Movimiento.objects.filter(sku=self.sku).count(), kardex)  # mover no es kardex: el estado no cambia
        respuesta = self._post(accion="regresar", linea_id=self.linea.pk, origen="TAR-04", cantidad=10, motivo="de nuevo a recepción")
        self.assertContains(respuesta, "regresaron de TAR-04 a recepción")
        self.assertEqual(realidad_recepcion(self.orden)[0]["en_recepcion"], 10)
        self.assertTrue(Movimiento.objects.filter(sku=self.sku, referencia=self.orden.folio, tipo=Movimiento.PUTAWAY, estado_destino=Saldo.EN_PUTAWAY).exists())
        respuesta = self._post(accion="danar", linea_id=self.linea.pk, origen="PIC-4-I-F-1", cantidad=3, motivo="rotas al bajar")
        self.assertContains(respuesta, "3 de COLIMITA-SIX lote CEX27ABC marcadas dañadas")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (141, 3))
        self.assertEqual(Saldo.objects.filter(sku=self.sku, estado=Saldo.CUARENTENA).aggregate(t=__import__("django.db.models", fromlist=["Sum"]).Sum("cantidad"))["t"], 41)
        f = realidad_recepcion(self.orden)[0]
        self.assertEqual((f["contadas"], f["danadas"], f["cuadra"]), (141, 3, True))
        # Más de lo que hay en la posición: nada cambia.
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="PIC-3-I-F-2", destino="TAR-04", cantidad=99, motivo="x")
        self.assertContains(respuesta, "solo hay 20")
        # Sin motivo: nada cambia.
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="PIC-3-I-F-2", destino="TAR-04", cantidad=1, motivo="")
        self.assertContains(respuesta, "Di por qué se corrige")

    def test_corregir_conteo_sube_y_baja(self):
        respuesta = self._post(accion="conteo", linea_id=self.linea.pk, contadas=150, danadas=2, motivo="recontamos")
        self.assertContains(respuesta, "contadas +6, dañadas +2")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (150, 2))
        f = realidad_recepcion(self.orden)[0]
        self.assertEqual((f["en_recepcion"], f["cuadra"]), (6, True))
        # Bajar: primero de recepción (6), luego de los anaqueles del lote (4).
        respuesta = self._post(accion="conteo", linea_id=self.linea.pk, contadas=140, danadas=0, motivo="eran menos")
        self.assertContains(respuesta, "contadas -10, dañadas -2")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (140, 0))
        self.assertEqual(realidad_recepcion(self.orden)[0]["en_recepcion"], 0)
        self.assertEqual(sum(n for _c, n in self._vendible()), 102)
        self.assertTrue(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RECEPCION, delta__lt=0, referencia=self.orden.folio).exists())
        evento = EventoAuditoria.objects.filter(entidad_id=self.orden.folio, accion="recepcion_corregida").latest("ts")
        self.assertEqual((evento.delta["contadas"], evento.delta["danadas"]), ([150, 140], [2, 0]))

    def test_editar_linea_y_agregar_producto_con_conteo_y_posicion(self):
        from apps.catalogo.models import SKU

        respuesta = self._post(accion="linea", linea_id=self.linea.pk, lote="CEX27ABC", caducidad="2027-06-12", anunciadas=150, motivo="caducidad corregida")
        self.assertContains(respuesta, "Línea de COLIMITA-SIX corregida")
        self.linea.refresh_from_db()
        self.assertEqual((str(self.linea.fecha_caducidad), self.linea.cantidad_anunciada), ("2027-06-12", 150))
        self.assertEqual(str(Lote.objects.get(sku=self.sku, codigo="CEX27ABC").fecha_caducidad), "2027-06-12")
        otro = SKU.objects.create(cliente=self.cliente, codigo="BP355", codigo_barras="7500000000355", descripcion="Botella 355", peso_gr=500, requiere_lote=True)
        respuesta = self._post(accion="agregar", sku_id=otro.pk, lote="L2603011", caducidad="2027-06-12", anunciadas=0, contadas=24, danadas=1, ubicacion="TAR-04", ubicadas=24, motivo="llegó sin anunciar")
        self.assertContains(respuesta, "BP355 lote L2603011 agregado")
        nueva = LineaASN.objects.get(orden=self.orden, sku=otro)
        self.assertEqual((nueva.cantidad_anunciada, nueva.cantidad_recibida, nueva.cantidad_danada, str(nueva.fecha_caducidad)), (0, 24, 1, "2027-06-12"))
        self.assertEqual(Saldo.objects.get(sku=otro, estado=Saldo.UBICADO_VENDIBLE).ubicacion.codigo, "TAR-04")
        self.assertEqual(str(Lote.objects.get(sku=otro, codigo="L2603011").fecha_caducidad), "2027-06-12")
        filas = realidad_recepcion(self.orden)
        self.assertEqual([(f["sku"].codigo, f["diferencia"]) for f in filas], [("BP355", 25), ("COLIMITA-SIX", -6)])
        # Repetir el mismo lote no duplica la línea.
        respuesta = self._post(accion="agregar", sku_id=otro.pk, lote="L2603011", anunciadas=0, motivo="x")
        self.assertContains(respuesta, "ya tiene una línea con ese lote")

    def test_csv_cerrar_y_solo_lectura_y_portal(self):
        respuesta = self.client.get(reverse("mesa:recepcion_realidad_csv", args=[self.orden.pk]))
        cuerpo = respuesta.content.decode("utf-8-sig")
        self.assertEqual(cuerpo.splitlines()[0], "sku,nombre,codigo_barras,lote,caducidad,posicion,estado,piezas")
        self.assertIn("COLIMITA-SIX,Colimita six pack,7501234567890,CEX27ABC,2027-01-31,PIC-2-D-B-1,vendible,38", cuerpo)
        self.assertIn(",RECEPCION,cuarentena sin espacio,38", cuerpo)
        self._post(accion="de_cuarentena", linea_id=self.linea.pk, cantidad=38, motivo="x")
        self._post(accion="acomodar", linea_id=self.linea.pk, cantidad=38, destino="TAR-04", motivo="x")
        respuesta = self._post(accion="cerrar", tarimas_recibidas=3)
        self.assertContains(respuesta, "cerrada: todo el producto quedó vendible")
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.tarimas_recibidas), (OrdenEntrada.CERRADA, 3))
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "solo lectura")
        self.assertNotContains(respuesta, 'name="accion" value="mover"')
        respuesta = self._post(accion="mover", linea_id=self.linea.pk, origen="TAR-04", destino="PIC-3-I-F-2", cantidad=1, motivo="x")
        self.assertContains(respuesta, "ya está cerrada")
        # Mesa → Recepciones enlaza al editor; el portal ve dónde quedó, sin acciones.
        respuesta = self.client.get(reverse("mesa:recepciones"), {"cliente": self.cliente.slug})
        self.assertContains(respuesta, f'href="{self.url}">Ver realidad</a>')
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.get(reverse("portal:recepciones"))
        self.assertContains(respuesta, "Dónde quedó")
        self.assertContains(respuesta, "TAR-04")
        self.assertNotContains(respuesta, "Editar recepción")

    def test_editar_ubicadas_de_una_posicion(self):
        # Subir sin piezas en recepción: aviso claro, nada cambia.
        respuesta = self._post(accion="ubicadas", linea_id=self.linea.pk, origen="PIC-3-I-F-2", ubicadas=50, motivo="había más")
        self.assertContains(respuesta, "primero sube las contadas")
        self.assertEqual(self._vendible(), [("PIC-2-D-B-1", 38), ("PIC-3-I-F-2", 30), ("PIC-4-I-F-1", 38)])
        # Con piezas en recepción (contadas +20) sí sube; bajar regresa la diferencia a recepción.
        self._post(accion="conteo", linea_id=self.linea.pk, contadas=164, danadas=0, motivo="faltaban 20 por contar")
        respuesta = self._post(accion="ubicadas", linea_id=self.linea.pk, origen="PIC-3-I-F-2", ubicadas=50, motivo="caben")
        self.assertContains(respuesta, "PIC-3-I-F-2: 30 → 50 (+20 desde recepción)")
        self.assertEqual(self._vendible(), [("PIC-2-D-B-1", 38), ("PIC-3-I-F-2", 50), ("PIC-4-I-F-1", 38)])
        self.assertEqual(realidad_recepcion(self.orden)[0]["en_recepcion"], 0)
        respuesta = self._post(accion="ubicadas", linea_id=self.linea.pk, origen="PIC-3-I-F-2", ubicadas=45, motivo="eran menos")
        self.assertContains(respuesta, "PIC-3-I-F-2: 50 → 45 (5 de vuelta a recepción")
        self.assertEqual(realidad_recepcion(self.orden)[0]["en_recepcion"], 5)
        respuesta = self._post(accion="ubicadas", linea_id=self.linea.pk, origen="PIC-3-I-F-2", ubicadas=45, motivo="x")
        self.assertContains(respuesta, "sigue en 45")
