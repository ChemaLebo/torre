"""Recepción en piso: recibir con dañadas + foto de llegada, ubicar y cerrar."""
from django.db.models import Sum
from django.urls import reverse

from apps.core.models import EvidenciaFoto
from apps.inventario.models import LineaASN, OrdenEntrada, Saldo

from .base import PisoTestCase


def _suma(sku, estado):
    return Saldo.objects.filter(sku=sku, estado=estado).aggregate(t=Sum("cantidad"))["t"] or 0


class RecepcionPisoTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=10,
        )
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])

    def test_recibir_con_danadas_y_foto_de_llegada(self):
        respuesta = self.client.post(self.url, {
            "accion": "recibir",
            "linea_id": self.linea.pk,
            "cantidad_ok": "8",
            "cantidad_danada": "2",
            "foto_llegada": self.foto("llegada.jpg"),
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)

        self.linea.refresh_from_db()
        self.orden.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 8)
        self.assertEqual(self.linea.cantidad_danada, 2)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 8)
        self.assertEqual(_suma(self.sku, Saldo.CUARENTENA), 2)
        # Todas las líneas quedaron completas → la orden avanza sola a RECIBIDA.
        self.assertEqual(self.orden.estado, OrdenEntrada.RECIBIDA)
        self.assertTrue(
            EvidenciaFoto.objects.filter(
                entidad="asn", entidad_id=self.orden.folio, tipo="llegada",
            ).exists()
        )

    def test_recibir_sin_cantidades_muestra_error(self):
        respuesta = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "0", "cantidad_danada": "0",
        }, follow=True)
        self.assertContains(respuesta, "Nada que recibir")
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)

    def test_ubicar_a_ubicacion_escaneada(self):
        from apps.inventario.services import recibir
        recibir(self.linea, 10, 0, self.operador)

        respuesta = self.client.post(self.url, {
            "accion": "ubicar",
            "sku_id": self.sku.pk,
            "cantidad": "10",
            "ubicacion": "a-01-1",  # escaneo tolerante a mayúsculas/minúsculas
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)
        self.assertEqual(_suma(self.sku, Saldo.UBICADO_VENDIBLE), 10)

    def test_ubicar_a_ubicacion_inexistente_da_error_claro(self):
        from apps.inventario.services import recibir
        recibir(self.linea, 10, 0, self.operador)

        respuesta = self.client.post(self.url, {
            "accion": "ubicar", "sku_id": self.sku.pk,
            "cantidad": "10", "ubicacion": "Z-99-9",
        }, follow=True)
        self.assertContains(respuesta, "No existe la ubicación")
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 10)

    def test_cerrar_exige_todo_ubicado(self):
        from apps.inventario.services import recibir, ubicar
        recibir(self.linea, 10, 0, self.operador)

        respuesta = self.client.post(self.url, {"accion": "cerrar"}, follow=True)
        self.orden.refresh_from_db()
        self.assertContains(respuesta, "sin ubicar")
        self.assertNotEqual(self.orden.estado, OrdenEntrada.CERRADA)

        ubicar(self.sku, 10, self.ubic_picking, None, self.operador)
        self.client.post(self.url, {"accion": "cerrar"}, follow=True)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertIsNotNone(self.orden.ts_vendible)


class RecepcionCiegaYFotoObligatoriaTests(PisoTestCase):
    """B4: el que cuenta no ve lo anunciado, y la primera línea exige foto."""

    def setUp(self):
        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=3)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=8887,
        )
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])

    def test_captura_no_muestra_cantidad_anunciada(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "8887")       # la cifra anunciada no aparece
        self.assertNotContains(respuesta, "Anunciado")  # ni la columna
        self.assertContains(respuesta, "COLIMITA-SIX")  # el SKU sí: es lo que se cuenta

    def test_linea_completa_muestra_pill_sin_cifras(self):
        from apps.inventario.services import recibir
        self.linea.cantidad_anunciada = 10
        self.linea.save(update_fields=["cantidad_anunciada"])
        EvidenciaFoto.objects.create(
            entidad="asn", entidad_id=self.orden.folio, tipo="llegada",
            archivo=self.foto(), tomada_por="piso1",
        )
        recibir(self.linea, 10, 0, self.operador)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Completa")

    def test_primer_recibir_sin_foto_se_rechaza(self):
        respuesta = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "5", "cantidad_danada": "0",
        }, follow=True)
        self.assertContains(respuesta, "Tómale foto al camión/tarimas")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)

    def test_primer_recibir_con_foto_pasa_y_el_segundo_ya_no_la_exige(self):
        con_foto = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "5", "cantidad_danada": "0",
            "foto_llegada": self.foto("llegada.jpg"),
        }, follow=True)
        self.assertEqual(con_foto.status_code, 200)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 5)

        sin_foto = self.client.post(self.url, {
            "accion": "recibir", "linea_id": self.linea.pk,
            "cantidad_ok": "4", "cantidad_danada": "0",
        }, follow=True)
        self.assertNotContains(sin_foto, "Tómale foto")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 9)


class CerrarConTarimasTests(PisoTestCase):
    """B1: al cerrar se capturan las tarimas recibidas (default: las anunciadas)."""

    def setUp(self):
        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente, tarimas=3)
        self.linea = LineaASN.objects.create(
            orden=self.orden, sku=self.sku, cantidad_anunciada=10,
        )
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])

    def _recibir_y_ubicar_todo(self):
        from apps.inventario.services import recibir, ubicar
        recibir(self.linea, 10, 0, self.operador)
        ubicar(self.sku, 10, self.ubic_picking, None, self.operador)

    def test_cerrar_guarda_tarimas_recibidas(self):
        self._recibir_y_ubicar_todo()
        respuesta = self.client.post(self.url, {
            "accion": "cerrar", "tarimas_recibidas": "2",
        }, follow=True)
        self.assertEqual(respuesta.status_code, 200)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(self.orden.tarimas_recibidas, 2)

    def test_cerrar_sin_capturar_usa_las_anunciadas(self):
        self._recibir_y_ubicar_todo()
        self.client.post(self.url, {"accion": "cerrar"}, follow=True)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(self.orden.tarimas_recibidas, 3)

    def test_replay_de_cierre_sobre_cerrada_no_pisa_tarimas(self):
        # Botón atrás + reenviar POST con otro valor: la orden cerrada no debe
        # mutar el dato que factura finanzas ni duplicar transiciones.
        self._recibir_y_ubicar_todo()
        self.client.post(self.url, {"accion": "cerrar", "tarimas_recibidas": "16"}, follow=True)
        respuesta = self.client.post(
            self.url, {"accion": "cerrar", "tarimas_recibidas": "2"}, follow=True,
        )
        self.assertContains(respuesta, "ya está cerrada")
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(self.orden.tarimas_recibidas, 16)


class RecepcionPorLoteTests(PisoTestCase):
    """Flujo por lote (Chema 2026-10-06): foto de llegada, el escaneo abre
    Contar (todos los lotes del producto, sin registrar nada), las cuentas
    viajan por GET a Acomodar (una fila por lote y posición del plan, dañadas
    editables) y un solo POST registra cuenta y acomodo en una transacción."""

    def setUp(self):
        from datetime import date
        from decimal import Decimal

        from apps.catalogo.models import Ubicacion
        from apps.inventario.models import LineaASN, OrdenEntrada

        self.login_piso()
        self.anaquel = Ubicacion.objects.create(codigo="PIC-1-I-F-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=1)
        self.sku.largo_cm, self.sku.ancho_cm, self.sku.alto_cm = 36, 24, 17
        self.sku.codigo_barras = "7500000000017"
        self.sku.requiere_lote = True
        self.sku.rotacion = "A"  # clase A: toma el mejor anaquel (C tomaría el peor)
        self.sku.precio_declarado = Decimal(1)
        self.sku.save()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)
        self.linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=5, lote_codigo="L-ASN", fecha_caducidad=date(2027, 1, 31))
        self.url = reverse("piso:recepcion_detalle", args=[self.orden.pk])
        self.url_contar = reverse("piso:recepcion_contar", args=[self.orden.pk])
        self.url_ubicar = reverse("piso:recepcion_ubicar", args=[self.orden.pk])

    def _foto(self):
        return self.client.post(self.url, {"accion": "foto", "foto_llegada": self.foto("llegada.jpg")})

    def _escanear(self, codigo="7500000000017"):
        return self.client.post(self.url, {"accion": "escanear", "codigo": codigo})

    def _get_ubicar(self, cuentas=None, follow=False, **extra):
        """GET de Acomodar con lo que Contar manda: {lote: (contadas, dañadas)}."""
        from apps.inventario.models import LineaASN

        params = {"sku": self.sku.pk}
        for lote, (contadas, danadas) in (cuentas or {}).items():
            linea = LineaASN.objects.get(orden=self.orden, lote_codigo=lote)
            params[f"c_{linea.pk}"] = contadas
            params[f"d_{linea.pk}"] = danadas
        params.update(extra)
        return self.client.get(self.url_ubicar, params, follow=follow)

    def _acomodar(self, lotes, follow=True):
        """POST de Acomodar (tras el modal): lotes = [(lote, contadas, dañadas,
        [(ubicación, cantidad), ...][, caducidad])]; un lote sin línea en la
        orden va como lote nuevo."""
        from apps.inventario.models import LineaASN

        datos = {"accion": "ubicar", "sku_id": self.sku.pk, "n_lotes": len(lotes)}
        for i, entrada in enumerate(lotes):
            lote, contadas, danadas, filas = entrada[:4]
            linea = LineaASN.objects.filter(orden=self.orden, sku=self.sku, lote_codigo=lote).first()
            caducidad = entrada[4] if len(entrada) > 4 else (linea.fecha_caducidad.isoformat() if linea and linea.fecha_caducidad else "")
            datos.update({
                f"l_{i}_linea": linea.pk if linea else "", f"l_{i}_lote": lote, f"l_{i}_cad": caducidad,
                f"l_{i}_contadas": contadas, f"l_{i}_danadas": danadas, f"l_{i}_n": len(filas),
            })
            for j, (ubicacion, cantidad) in enumerate(filas):
                datos[f"a_{i}_{j}_ubicacion"] = ubicacion
                datos[f"a_{i}_{j}_cantidad"] = cantidad
        return self.client.post(self.url_ubicar, datos, follow=follow)

    def _pasos(self):
        from apps.inventario.models import OrdenEntrada

        return [(p["lote"], p["ubicacion"], p["cantidad"], p["ubicadas"]) for p in OrdenEntrada.objects.get(pk=self.orden.pk).plan_acomodo["pasos"]]

    def test_sin_foto_no_se_escanea_y_la_pantalla_lo_pide(self):
        from apps.inventario.models import Saldo

        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Foto de llegada")
        self.assertNotContains(respuesta, 'id="form-escanear"')
        respuesta = self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"}, follow=True)
        self.assertContains(respuesta, "Tómale foto al camión/tarimas")
        respuesta = self._acomodar([("L-ASN", 2, 0, [("PIC-1-I-F-2", 2)])])
        self.assertContains(respuesta, "Tómale foto al camión/tarimas")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertFalse(Saldo.objects.filter(sku=self.sku).exists())

    def test_escaneo_abre_la_cuenta_y_lo_contado_va_a_acomodar_con_el_plan(self):
        from apps.inventario.models import OrdenEntrada, Saldo

        self._foto()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'id="form-escanear"')
        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        self.assertEqual(orden.plan_acomodo["pasos"][0]["ubicacion"], "PIC-1-I-F-2")  # plan por orden: 5 anunciadas
        # El escaneo identifica el producto y abre su cuenta: NO suma piezas.
        respuesta = self._escanear()
        self.assertRedirects(respuesta, f"{self.url_contar}?sku={self.sku.pk}", fetch_redirect_response=False)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        respuesta = self.client.get(self.url_contar, {"sku": self.sku.pk})
        self.assertContains(respuesta, f'action="{self.url_ubicar}"')  # las cuentas viajan por GET a Acomodar
        self.assertContains(respuesta, f'name="c_{self.linea.pk}"')
        self.assertContains(respuesta, 'name="nl_codigo"')  # renglón para un lote que no venía en la orden
        self.assertContains(respuesta, "L-ASN")
        self.assertContains(respuesta, "contadas hasta ahora 0")
        self.assertNotContains(respuesta, "anunciada")  # conteo ciego: sin lo anunciado
        # Contar no registra nada: Acomodar muestra la cuenta editable y la fila del plan.
        respuesta = self._get_ubicar({"L-ASN": (1, 0)})
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)
        self.assertFalse(Saldo.objects.filter(sku=self.sku).exists())
        self.assertContains(respuesta, 'name="n_lotes" value="1"')
        self.assertContains(respuesta, 'name="l_0_lote" value="L-ASN"')
        self.assertContains(respuesta, 'name="l_0_cad" value="2027-01-31"')
        self.assertContains(respuesta, 'name="l_0_contadas" id="l_0_contadas" min="0" inputmode="numeric" value="1"')
        self.assertContains(respuesta, 'id="a_0_0_ubicacion" class="mono ubicacion" autocomplete="off" list="ubicaciones-destino" value="PIC-1-I-F-2"')
        self.assertContains(respuesta, 'id="a_0_0_cantidad" class="cantidad" min="0" inputmode="numeric" value="1"')
        self.assertContains(respuesta, "según el plan 5")
        self.assertContains(respuesta, "anaquel libre para clase A")  # el motivo del paso del plan
        self.assertContains(respuesta, 'id="modal-confirmar"')
        self.assertContains(respuesta, "Vas a acomodar")
        # Confirmar registra cuenta y acomodo juntos.
        respuesta = self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 1)])])
        self.assertContains(respuesta, "lote L-ASN: 1 contada")
        self.assertContains(respuesta, "1 pieza (lote L-ASN) en PIC-1-I-F-2")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 1)
        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((saldo.ubicacion.codigo, saldo.lote.codigo, saldo.cantidad), ("PIC-1-I-F-2", "L-ASN", 1))
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).exists())
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 5, 1)])
        # Reescanear y contar cero: nada que acomodar, de vuelta al escáner.
        respuesta = self._get_ubicar({"L-ASN": (0, 0)}, follow=True)
        self.assertContains(respuesta, "No hay piezas")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 1)

    def test_codigo_ajeno_y_sin_piezas_por_ubicar(self):
        self._foto()
        respuesta = self.client.post(self.url, {"accion": "escanear", "codigo": "NO-EXISTE"}, follow=True)
        self.assertContains(respuesta, "no es de un producto de esta orden")
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk}, follow=True)
        self.assertContains(respuesta, "No hay piezas")
        respuesta = self.client.get(self.url_contar, {"sku": 999999}, follow=True)
        self.assertContains(respuesta, "Escanea un producto de la orden para contarlo")

    def test_danadas_viajan_de_contar_y_se_corrigen_en_acomodar(self):
        from apps.inventario.models import Saldo

        self._foto()
        respuesta = self._get_ubicar({"L-ASN": (1, 1)})
        self.assertContains(respuesta, 'name="l_0_danadas" id="l_0_danadas" min="0" inputmode="numeric" value="1"')
        respuesta = self._acomodar([("L-ASN", 1, 1, [("PIC-1-I-F-2", 1)])])
        self.assertContains(respuesta, "lote L-ASN: 1 contada, 1 dañada a cuarentena")
        self.assertContains(respuesta, "1 pieza (lote L-ASN) en PIC-1-I-F-2")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (1, 1))
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 1)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).cantidad, 1)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).exists())
        # Solo dañadas (corregidas en Acomodar), sin acomodar nada: la cuenta se registra sola.
        respuesta = self._acomodar([("L-ASN", 0, 1, [("PIC-1-I-F-2", 0)])])
        self.assertContains(respuesta, "lote L-ASN: 1 dañada a cuarentena")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (1, 2))
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 2)

    def test_sin_espacio_manda_a_cuarentena_y_dos_lotes_van_en_una_pantalla(self):
        from apps.inventario.models import LineaASN, Saldo

        self.anaquel.lleno_manual = True
        self.anaquel.save()
        otra = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=2, lote_codigo="L-OTRO")
        self._foto()
        respuesta = self._escanear("COLIMITA-SIX")
        self.assertRedirects(respuesta, f"{self.url_contar}?sku={self.sku.pk}", fetch_redirect_response=False)
        # Contar muestra los dos lotes anunciados, cada uno con su cuenta.
        respuesta = self.client.get(self.url_contar, {"sku": self.sku.pk})
        self.assertContains(respuesta, "L-OTRO")
        self.assertContains(respuesta, f'name="c_{self.linea.pk}"')
        self.assertContains(respuesta, f'name="c_{otra.pk}"')
        # Las dos cuentas llegan juntas a Acomodar: un bloque por lote; sin anaquel ni zona de desborde, cuarentena.
        respuesta = self._get_ubicar({"L-ASN": (1, 0), "L-OTRO": (2, 0)})
        self.assertContains(respuesta, 'name="n_lotes" value="2"')
        self.assertContains(respuesta, 'name="l_0_lote" value="L-ASN"')
        self.assertContains(respuesta, 'name="l_1_lote" value="L-OTRO"')
        self.assertContains(respuesta, "cuarentena")
        self.assertContains(respuesta, 'id="a_1_0_cantidad" class="cantidad" min="0" inputmode="numeric" value="2"')
        respuesta = self._acomodar([("L-ASN", 1, 0, [("", 1)]), ("L-OTRO", 2, 0, [("", 2)])])
        self.assertContains(respuesta, "1 pieza (lote L-ASN) a cuarentena (sin anaquel con espacio)")
        self.assertContains(respuesta, "2 piezas (lote L-OTRO) a cuarentena (sin anaquel con espacio)")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 3)
        self.linea.refresh_from_db()
        otra.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, otra.cantidad_recibida), (1, 2))

    def test_sin_espacio_con_zona_de_desborde_queda_vendible_ahi_con_su_lote(self):
        from apps.catalogo.models import Ubicacion
        from apps.core.models import EventoAuditoria
        from apps.inventario.models import Saldo

        Ubicacion.objects.create(codigo="RES-CUAR", tipo=Ubicacion.RESERVA)
        self.anaquel.lleno_manual = True
        self.anaquel.save()
        self._foto()
        respuesta = self._get_ubicar({"L-ASN": (2, 0)})
        self.assertContains(respuesta, 'value="RES-CUAR"')
        self.assertContains(respuesta, "reservas, vendible")  # el plan ya nombra la zona (2026-09-24)
        self.assertNotContains(respuesta, ">CUARENTENA<")
        # Confirmar tal cual (prellenado) → vendible en la zona, con lote, y el plan avanza su paso "sin espacio".
        respuesta = self._acomodar([("L-ASN", 2, 0, [("RES-CUAR", 2)])])
        self.assertContains(respuesta, "2 piezas (lote L-ASN) en RES-CUAR")
        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((saldo.ubicacion.codigo, saldo.lote.codigo, saldo.cantidad), ("RES-CUAR", "L-ASN", 2))
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.CUARENTENA).exists())
        self.assertEqual(self._pasos(), [("L-ASN", "RES-CUAR", 5, 2)])
        self.assertTrue(EventoAuditoria.objects.filter(entidad="sku", entidad_id=self.sku.codigo, accion="a_desborde").exists())
        # Posición vacía = la zona de desborde (vendible), no cuarentena.
        respuesta = self._acomodar([("L-ASN", 1, 0, [("", 1)])])
        self.assertContains(respuesta, "1 pieza (lote L-ASN) en RES-CUAR (zona de desborde, vendible)")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).cantidad, 3)

    def test_el_plan_separa_lotes_en_anaqueles_distintos(self):
        from apps.catalogo.models import Ubicacion
        from apps.inventario.models import LineaASN, OrdenEntrada

        Ubicacion.objects.create(codigo="PIC-1-I-B-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=2)
        LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=3, lote_codigo="L-OTRO")
        self._foto()
        self.client.get(self.url)
        pasos = OrdenEntrada.objects.get(pk=self.orden.pk).plan_acomodo["pasos"]
        por_lote = {p["lote"]: p["ubicacion"] for p in pasos}
        self.assertEqual(por_lote, {"L-ASN": "PIC-1-I-F-2", "L-OTRO": "PIC-1-I-B-2"})

    def test_dos_lotes_y_uno_nuevo_no_anunciado_en_una_transaccion(self):
        from apps.catalogo.models import Lote, Ubicacion
        from apps.core.models import EventoAuditoria
        from apps.inventario.models import LineaASN, Saldo

        Ubicacion.objects.create(codigo="PIC-1-I-B-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=2)
        Ubicacion.objects.create(codigo="PIC-1-I-B-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=3)
        otra = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=3, lote_codigo="L-OTRO")
        self._foto()
        self.client.get(self.url)
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 5, 0), ("L-OTRO", "PIC-1-I-B-2", 3, 0)])
        # Contar: 2 de L-ASN, 3 de L-OTRO y 4 de un lote que no venía en la orden.
        respuesta = self.client.get(self.url_ubicar, {
            "sku": self.sku.pk, f"c_{self.linea.pk}": 2, f"c_{otra.pk}": 3,
            "nl_codigo": "L-NUEVO", "nl_cad": "2028-06-30", "nl_c": 4, "nl_d": 0,
        })
        self.assertContains(respuesta, 'name="n_lotes" value="3"')
        self.assertContains(respuesta, 'name="l_2_lote" value="L-NUEVO"')
        self.assertContains(respuesta, 'name="l_2_cad" value="2028-06-30"')
        self.assertContains(respuesta, "no venía en la orden")
        self.assertContains(respuesta, 'id="a_1_0_ubicacion" class="mono ubicacion" autocomplete="off" list="ubicaciones-destino" value="PIC-1-I-B-2"')
        self.assertContains(respuesta, "fuera del plan")  # el lote nuevo no está en el plan: sugerencia ad hoc
        respuesta = self._acomodar([
            ("L-ASN", 2, 0, [("PIC-1-I-F-2", 2)]),
            ("L-OTRO", 3, 0, [("PIC-1-I-B-2", 3)]),
            ("L-NUEVO", 4, 0, [("PIC-1-I-B-3", 4)], "2028-06-30"),
        ])
        self.assertContains(respuesta, "lote L-NUEVO: 4 contadas")
        self.assertContains(respuesta, "4 piezas (lote L-NUEVO) en PIC-1-I-B-3")
        nueva = LineaASN.objects.get(orden=self.orden, lote_codigo="L-NUEVO")
        self.assertEqual((nueva.cantidad_anunciada, nueva.cantidad_recibida, str(nueva.fecha_caducidad)), (0, 4, "2028-06-30"))
        self.assertEqual(str(Lote.objects.get(sku=self.sku, codigo="L-NUEVO").fecha_caducidad), "2028-06-30")
        self.assertTrue(EventoAuditoria.objects.filter(entidad="asn", entidad_id=self.orden.folio, accion="lote_no_anunciado").exists())
        self.assertEqual(
            sorted(Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).values_list("ubicacion__codigo", "lote__codigo", "cantidad")),
            [("PIC-1-I-B-2", "L-OTRO", 3), ("PIC-1-I-B-3", "L-NUEVO", 4), ("PIC-1-I-F-2", "L-ASN", 2)],
        )
        # El lote nuevo se salió del plan → se replanea lo que falta (3 de L-ASN) con lo acomodado como fijo;
        # lo que llegó de más del lote nuevo no tapa lo que falta de L-ASN.
        evento = EventoAuditoria.objects.get(entidad="asn", entidad_id=self.orden.folio, accion="acomodo_replaneado")
        self.assertEqual((evento.delta["por"], evento.delta["lote"], evento.delta["cantidad"]), ("sin_paso", "L-NUEVO", 4))
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 3, 0)])
        # Acomodar avisa (sin bloquear) dónde ya hay otro lote de este producto.
        respuesta = self._get_ubicar({"L-ASN": (1, 0)})
        self.assertContains(respuesta, '"PIC-1-I-B-2": ["L-OTRO"]')
        self.assertContains(respuesta, "Ya hay: COLIMITA-SIX ×2 (L-ASN)")

    def test_acomodar_fuera_del_plan_replanea_con_lo_acomodado_como_fijo(self):
        from apps.catalogo.models import Ubicacion
        from apps.core.models import EventoAuditoria

        Ubicacion.objects.create(codigo="PIC-1-I-B-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=2)
        self._foto()
        self.client.get(self.url)
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 5, 0)])
        # Dos a otro anaquel: la realidad manda, el plan de lo que falta sigue a esas dos.
        self._acomodar([("L-ASN", 2, 0, [("PIC-1-I-B-2", 2)])])
        evento = EventoAuditoria.objects.get(entidad="asn", entidad_id=self.orden.folio, accion="acomodo_replaneado")
        self.assertEqual(evento.delta, {"sku": "COLIMITA-SIX", "lote": "L-ASN", "ubicacion": "PIC-1-I-B-2", "cantidad": 2, "por": "otra_posicion", "pendientes_antes": 3, "pasos": 1})
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-B-2", 3, 0)])
        # Seguir el plan nuevo no vuelve a replanear.
        self._acomodar([("L-ASN", 3, 0, [("PIC-1-I-B-2", 3)])])
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-B-2", 3, 3)])
        self.assertEqual(EventoAuditoria.objects.filter(accion="acomodo_replaneado").count(), 1)

    def test_rehacer_el_plan_respeta_lo_ya_ubicado(self):
        from apps.inventario.models import OrdenEntrada
        from apps.inventario.services import planear_acomodo

        self._foto()
        self.client.get(self.url)
        for _ in range(2):  # dos piezas contadas y acomodadas según el plan
            self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 1)])])
        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        self.assertEqual(orden.plan_acomodo["pasos"][0]["ubicadas"], 2)
        # Un plan viejo SIN lote (versión anterior) también se acredita.
        orden.plan_acomodo["pasos"][0].pop("lote")
        orden.save(update_fields=["plan_acomodo"])
        plan = planear_acomodo(orden)
        self.assertEqual([(p["lote"], p["cantidad"], p["ubicadas"]) for p in plan["pasos"]], [("L-ASN", 3, 0)])
        self.assertEqual(plan["ubicadas"], {f"{self.sku.pk}|L-ASN": 2})
        # Ubicar a mano fuera del plan tampoco se replanea: el tope es lo físico que falta.
        from apps.catalogo.models import Lote
        from apps.inventario.services import recibir, ubicar
        self.linea.refresh_from_db()
        recibir(self.linea, 3, 0, self.operador)
        ubicar(self.sku, 2, self.anaquel, Lote.objects.get(codigo="L-ASN"), self.operador)
        plan = planear_acomodo(orden)
        self.assertEqual([p["cantidad"] for p in plan["pasos"]], [1])
        # Ya todo ubicado: la línea sale como completa, no desaparece del plan.
        self.linea.refresh_from_db()
        ubicar(self.sku, 1, self.anaquel, Lote.objects.get(codigo="L-ASN"), self.operador)
        plan = planear_acomodo(orden)
        self.assertEqual(plan["pasos"], [])
        self.assertEqual(plan["completas"], [{"sku": "COLIMITA-SIX", "lote": "L-ASN", "anunciadas": 5, "recibidas": 5, "danadas": 0, "diferencia": 0}])

    def test_acomodar_varias_de_una_vez_y_lo_contado_de_antes(self):
        from apps.core.models import EventoAuditoria
        from apps.inventario.models import Saldo

        self._foto()
        self.client.get(self.url)
        respuesta = self._get_ubicar({"L-ASN": (3, 0)})
        self.assertContains(respuesta, "según el plan 5")
        self.assertContains(respuesta, 'id="a_0_0_cantidad" class="cantidad" min="0" inputmode="numeric" value="3"')
        respuesta = self._acomodar([("L-ASN", 3, 0, [("PIC-1-I-F-2", 3)])])
        self.assertContains(respuesta, "3 piezas (lote L-ASN) en PIC-1-I-F-2")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).cantidad, 3)
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 5, 3)])
        self.assertFalse(EventoAuditoria.objects.filter(accion="acomodo_replaneado").exists())  # siguió el plan
        respuesta = self._get_ubicar({"L-ASN": (1, 0)})
        self.assertContains(respuesta, "según el plan 2")
        self.assertContains(respuesta, "Ya hay: COLIMITA-SIX ×3 (L-ASN)")
        # Contar sin acomodar: solo se registra la cuenta y queda en recepción.
        respuesta = self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 0)])])
        self.assertContains(respuesta, "lote L-ASN: 1 contada")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 1)
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "<h2>Por ubicar</h2>")
        self.assertContains(respuesta, f'href="{self.url_ubicar}?sku={self.sku.pk}">Ubicar</a>')
        self.assertContains(respuesta, "1 pieza(s) en recepción")
        # Acomodar sin contar nada: la fila ofrece lo que ya estaba en recepción.
        respuesta = self._get_ubicar()
        self.assertContains(respuesta, "1 contada de antes sin acomodar")
        self.assertContains(respuesta, 'name="l_0_contadas" id="l_0_contadas" min="0" inputmode="numeric" value="0"')
        self.assertContains(respuesta, 'id="a_0_0_cantidad" class="cantidad" min="0" inputmode="numeric" value="1"')
        respuesta = self._acomodar([("L-ASN", 0, 0, [("PIC-1-I-F-2", 1)])])
        self.assertContains(respuesta, "1 pieza (lote L-ASN) en PIC-1-I-F-2")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).cantidad, 4)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).exists())
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 5, 4)])

    def test_acomodar_mas_de_lo_contado_se_rechaza_sin_registrar_nada(self):
        from apps.inventario.models import Saldo

        self._foto()
        self.client.get(self.url)
        respuesta = self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 4)])])
        self.assertContains(respuesta, "Vas a acomodar 4 de COLIMITA-SIX pero solo hay 1 contada ahora.")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)  # ni la cuenta: todo o nada
        self.assertFalse(Saldo.objects.filter(sku=self.sku).exists())
        self.assertEqual(self._pasos(), [("L-ASN", "PIC-1-I-F-2", 5, 0)])
        respuesta = self._acomodar([("L-ASN", 0, 0, [("PIC-1-I-F-2", 0)])])
        self.assertContains(respuesta, "Captura cuántas contaste o cuántas acomodas")

    def test_reiniciar_acomodo_regresa_todo_menos_danadas_y_stock_ajeno(self):
        from apps.catalogo.models import Lote, Ubicacion
        from apps.inventario.models import OrdenEntrada, Saldo
        from apps.inventario.services import reiniciar_acomodo

        # Otro anaquel con stock previo del mismo SKU pero de otro lote: no es de la orden, no se toca.
        otro = Ubicacion.objects.create(codigo="PIC-1-I-B-2", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52, prioridad=2)
        viejo = Lote.objects.create(sku=self.sku, codigo="L-VIEJO")
        Saldo.objects.create(sku=self.sku, ubicacion=otro, lote=viejo, estado=Saldo.UBICADO_VENDIBLE, cantidad=4)
        self._foto()
        self.client.get(self.url)
        # 2 al anaquel del plan, 1 a cuarentena por falta de espacio (vacío, sin zona), 1 dañada.
        self._acomodar([("L-ASN", 3, 1, [("PIC-1-I-F-2", 2), ("", 1)])])
        # 1 a mano en otro anaquel (replanea) y 1 que se queda en recepción sin acomodar.
        self._acomodar([("L-ASN", 2, 0, [("PIC-1-I-B-2", 1)])])
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 2)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 1)

        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        r = reiniciar_acomodo(orden, self.operador)
        self.assertEqual((r["regresadas"], r["de_cuarentena"], r["no_encontradas"]), (4, 1, 0))
        self.assertEqual(r["del_plan"] + sum(r["a_mano"].values()), 3)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 5)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 1)  # la dañada se queda
        vendible = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((vendible.ubicacion.codigo, vendible.lote.codigo, vendible.cantidad), ("PIC-1-I-B-2", "L-VIEJO", 4))
        orden.refresh_from_db()
        paso = orden.plan_acomodo["pasos"][0]
        self.assertEqual((paso["ubicacion"], paso["cantidad"], paso["ubicadas"]), ("PIC-1-I-F-2", 5, 0))
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (5, 1))  # la línea no cambia
        # Reiniciar otra vez sin haber acomodado nada: no mueve nada ni vuelve a sacar de cuarentena.
        r = reiniciar_acomodo(OrdenEntrada.objects.get(pk=self.orden.pk), self.operador)
        self.assertEqual(r["regresadas"], 0)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 1)

    def test_reiniciar_recepcion_pone_los_conteos_en_cero_para_volver_a_escanear(self):
        from apps.inventario.models import Movimiento, OrdenEntrada, Saldo
        from apps.inventario.services import reiniciar_recepcion

        self._foto()
        self.client.get(self.url)
        # 2 al anaquel del plan, 1 a cuarentena por falta de espacio, 1 dañada, 1 en recepción: línea completa.
        self._acomodar([("L-ASN", 4, 1, [("PIC-1-I-F-2", 2), ("", 1)])])
        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        self.assertEqual(orden.estado, OrdenEntrada.RECIBIDA)

        r = reiniciar_recepcion(orden, self.operador)
        self.assertEqual(r["acomodo"]["regresadas"], 3)  # 2 del plan + 1 de cuarentena
        self.assertEqual((r["recibidas"], r["danadas"], r["siguen_contadas"]), (4, 1, {"recibidas": 0, "danadas": 0}))
        self.assertFalse(Saldo.objects.filter(sku=self.sku).exists())  # ni en recepción, ni en anaquel, ni en cuarentena
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (0, 0))
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.ts_descarga_fin), (OrdenEntrada.EN_RECEPCION, None))
        paso = orden.plan_acomodo["pasos"][0]
        self.assertEqual((paso["ubicacion"], paso["cantidad"], paso["ubicadas"]), ("PIC-1-I-F-2", 5, 0))
        self.assertEqual(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RECEPCION, delta__lt=0).count(), 2)
        # Se vuelve a contar desde cero; la foto de llegada se queda.
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'id="form-escanear"')
        self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 0)])])
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 1)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 1)

    def test_reiniciar_recepcion_deja_contado_lo_que_no_encuentra(self):
        from apps.inventario.models import OrdenEntrada, Saldo
        from apps.inventario.services import recibir, reiniciar_recepcion

        recibir(self.linea, 3, 1, self.operador)
        Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).update(cantidad=2)  # una ya no está (vendida, apartada…)
        r = reiniciar_recepcion(OrdenEntrada.objects.get(pk=self.orden.pk), self.operador)
        self.assertEqual((r["recibidas"], r["danadas"], r["siguen_contadas"]), (2, 1, {"recibidas": 1, "danadas": 0}))
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (1, 0))

    def test_completar_con_lo_anunciado_recibe_lo_que_falta_ubica_segun_plan_y_cierra(self):
        from apps.inventario.models import OrdenEntrada, Saldo
        from apps.inventario.services import completar_recepcion_con_lo_anunciado

        self._foto()
        self.client.get(self.url)
        self._acomodar([("L-ASN", 3, 0, [("PIC-1-I-F-2", 2)])])  # 2 ubicadas por el plan, 1 en recepción
        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        r = completar_recepcion_con_lo_anunciado(orden, self.operador)
        self.assertEqual(r, {"recibidas": 2, "descontadas": 0, "retiradas": {"recepcion": 0, "anaqueles": 0, "no_encontradas": 0}, "ubicadas": 3, "a_cuarentena": 0})
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (5, 0))
        vendible = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((vendible.ubicacion.codigo, vendible.lote.codigo, vendible.cantidad), ("PIC-1-I-F-2", "L-ASN", 5))
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).exists())
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenEntrada.CERRADA)
        self.assertEqual(orden.plan_acomodo["pasos"][0]["ubicadas"], 5)
        with self.assertRaises(ValueError):
            completar_recepcion_con_lo_anunciado(orden, self.operador)  # ya cerrada

    def test_completar_descuenta_lo_contado_de_mas_hasta_dejar_el_asn_exacto(self):
        from apps.inventario.models import Movimiento, OrdenEntrada, Saldo
        from apps.inventario.services import completar_recepcion_con_lo_anunciado

        self._foto()
        self.client.get(self.url)
        self._acomodar([("L-ASN", 7, 0, [("PIC-1-I-F-2", 4)])])  # 7 contadas contra 5 anunciadas, 4 acomodadas
        r = completar_recepcion_con_lo_anunciado(OrdenEntrada.objects.get(pk=self.orden.pk), self.operador)
        self.assertEqual((r["recibidas"], r["descontadas"], r["retiradas"], r["ubicadas"]), (0, 2, {"recepcion": 2, "anaqueles": 0, "no_encontradas": 0}, 1))
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 5)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).cantidad, 5)
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).exists())
        self.assertEqual(OrdenEntrada.objects.get(pk=self.orden.pk).estado, OrdenEntrada.CERRADA)
        self.assertEqual(Movimiento.objects.filter(sku=self.sku, tipo=Movimiento.RECEPCION, delta=-2).count(), 1)

    def test_completar_retira_de_anaqueles_si_lo_de_mas_ya_estaba_ubicado(self):
        from apps.inventario.models import OrdenEntrada, Saldo
        from apps.inventario.services import completar_recepcion_con_lo_anunciado

        self._foto()
        self.client.get(self.url)
        self._acomodar([("L-ASN", 6, 0, [("PIC-1-I-F-2", 6)])])  # 6 contadas y ubicadas
        r = completar_recepcion_con_lo_anunciado(OrdenEntrada.objects.get(pk=self.orden.pk), self.operador)
        self.assertEqual((r["descontadas"], r["retiradas"]), (1, {"recepcion": 0, "anaqueles": 1, "no_encontradas": 0}))
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 5)
        vendible = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((vendible.ubicacion.codigo, vendible.cantidad), ("PIC-1-I-F-2", 5))
        self.assertEqual(OrdenEntrada.objects.get(pk=self.orden.pk).estado, OrdenEntrada.CERRADA)

    def test_mesa_exporta_el_plan_tal_cual_y_completa_con_lo_anunciado(self):
        from django.contrib.auth import get_user_model

        from apps.core.models import PerfilUsuario
        from apps.inventario.models import OrdenEntrada

        self._foto()
        self.client.get(self.url)
        self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 1)])])
        self.client.logout()
        mesa = get_user_model().objects.create_user("mesa-plan-csv", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        respuesta = self.client.get(reverse("mesa:recepciones"), {"cliente": self.cliente.slug})
        self.assertContains(respuesta, "Exportar plan (CSV)")
        self.assertContains(respuesta, 'value="completar_anunciado"')
        respuesta = self.client.get(reverse("mesa:recepcion_plan_csv", args=[self.orden.pk]))
        self.assertEqual(respuesta["Content-Type"], "text/csv; charset=utf-8")
        cuerpo = respuesta.content.decode("utf-8-sig")
        self.assertEqual(cuerpo.splitlines()[0], "sku,nombre,codigo_barras,lote,rack,cantidad")
        self.assertIn(f"{self.sku.codigo},{self.sku.descripcion},7500000000017,L-ASN,PIC-1-I-F-2,5", cuerpo)  # el plan sin descontar la ubicada
        respuesta = self.client.post(reverse("mesa:recepciones"), {"accion": "completar_anunciado", "orden_id": self.orden.pk}, follow=True)
        self.assertContains(respuesta, "cerrada exactamente con lo anunciado: 4 pieza(s) recibidas")
        self.assertEqual(OrdenEntrada.objects.get(pk=self.orden.pk).estado, OrdenEntrada.CERRADA)

    def test_mesa_ve_el_plan_y_lo_rehace(self):
        from django.contrib.auth import get_user_model

        from apps.core.models import PerfilUsuario
        from apps.inventario.models import OrdenEntrada

        self._foto()
        self.client.get(self.url)
        self.client.logout()
        mesa = get_user_model().objects.create_user("mesa-plan", password="x12345678")
        PerfilUsuario.objects.create(usuario=mesa, rol="mesa")
        self.client.force_login(mesa)
        respuesta = self.client.get(reverse("mesa:recepciones"), {"cliente": self.cliente.slug})
        self.assertContains(respuesta, "Plan de acomodo")
        self.assertContains(respuesta, 'value="replanear"')
        respuesta = self.client.post(reverse("mesa:recepciones"), {"accion": "replanear", "orden_id": self.orden.pk}, follow=True)
        self.assertContains(respuesta, "rehecho")
        self.assertContains(respuesta, 'value="reiniciar_acomodo"')
        respuesta = self.client.post(reverse("mesa:recepciones"), {"accion": "reiniciar_acomodo", "orden_id": self.orden.pk}, follow=True)
        self.assertContains(respuesta, "regresaron a recepción")
        self.assertTrue(OrdenEntrada.objects.get(pk=self.orden.pk).plan_acomodo["pasos"])
        self.assertNotContains(respuesta, 'value="reiniciar_recepcion"')  # sin nada recibido no hay qué reiniciar
        self.client.logout()
        self.login_piso()
        self._acomodar([("L-ASN", 1, 0, [("PIC-1-I-F-2", 0)])])
        self.client.logout()
        self.client.force_login(mesa)
        respuesta = self.client.get(reverse("mesa:recepciones"), {"cliente": self.cliente.slug})
        self.assertContains(respuesta, 'value="reiniciar_recepcion"')
        respuesta = self.client.post(reverse("mesa:recepciones"), {"accion": "reiniciar_recepcion", "orden_id": self.orden.pk}, follow=True)
        self.assertContains(respuesta, "recepción reiniciada")
        self.assertContains(respuesta, "los conteos bajaron 1 recibida(s)")


class ReingresoYaContadoTests(PisoTestCase):
    """ASN-0006 (Chema 2026-09-30): el reingreso que nace de una cancelación en
    bodega ya viene contado y en put-away; contarlo otra vez lo duplicaba.
    Contar manda directo a Ubicar y el servicio rechaza recibir de nuevo."""

    def setUp(self):
        from apps.inventario.services import recibir

        self.login_piso()
        self.orden = OrdenEntrada.objects.create(cliente=self.cliente)  # una ASN normal, para dejar 1 pieza en put-away
        linea = LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=1)
        recibir(linea, 1, 0, self.operador)
        self.reingreso = OrdenEntrada.objects.create(
            cliente=self.cliente, tipo=OrdenEntrada.TIPO_REINGRESO, estado=OrdenEntrada.RECIBIDA,
        )
        self.linea = LineaASN.objects.create(orden=self.reingreso, sku=self.sku, cantidad_anunciada=1, cantidad_recibida=1)

    def test_contar_manda_a_ubicar_sin_recibir_otra_vez(self):
        url = reverse("piso:recepcion_contar", args=[self.reingreso.pk])
        url_ubicar = reverse("piso:recepcion_ubicar", args=[self.reingreso.pk])
        respuesta = self.client.get(url, {"sku": self.sku.pk})
        self.assertRedirects(respuesta, f"{url_ubicar}?sku={self.sku.pk}")
        # Acomodar ofrece la pieza que ya estaba en recepción, sin cuenta nueva.
        respuesta = self.client.get(url_ubicar, {"sku": self.sku.pk})
        self.assertContains(respuesta, "1 contada de antes sin acomodar")
        self.assertContains(respuesta, 'id="a_0_0_cantidad" class="cantidad" min="0" inputmode="numeric" value="1"')
        # Contarla otra vez se rechaza (y no se acomoda nada: todo o nada).
        EvidenciaFoto.objects.create(entidad="asn", entidad_id=self.reingreso.folio, tipo="llegada", archivo=self.foto(), tomada_por="piso1")
        datos = {"accion": "ubicar", "sku_id": self.sku.pk, "n_lotes": 1, "l_0_linea": self.linea.pk, "l_0_lote": "", "l_0_cad": "",
                 "l_0_contadas": 1, "l_0_danadas": 0, "l_0_n": 1, "a_0_0_ubicacion": "A-01-1", "a_0_0_cantidad": 1}
        respuesta = self.client.post(url_ubicar, datos, follow=True)
        self.assertContains(respuesta, "reingreso ya contado")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 1)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 1)  # nada se duplicó
        # Solo acomodarla sí pasa.
        datos["l_0_contadas"] = 0
        respuesta = self.client.post(url_ubicar, datos, follow=True)
        self.assertContains(respuesta, "1 pieza en A-01-1")
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 0)
        self.assertEqual(_suma(self.sku, Saldo.UBICADO_VENDIBLE), 1)

    def test_el_servicio_tambien_lo_rechaza(self):
        from apps.inventario.services import recibir

        with self.assertRaises(ValueError):
            recibir(self.linea, 1, 0, self.operador)
        self.assertEqual(_suma(self.sku, Saldo.EN_PUTAWAY), 1)
