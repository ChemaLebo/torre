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


class RecepcionPiezaPorPiezaTests(PisoTestCase):
    """Flujo nuevo: foto de llegada, un escaneo = una pieza recibida, la
    pantalla de ubicar sigue el plan de la orden, dañada va a cuarentena."""

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
        self.url_ubicar = reverse("piso:recepcion_ubicar", args=[self.orden.pk])

    def _foto(self):
        return self.client.post(self.url, {"accion": "foto", "foto_llegada": self.foto("llegada.jpg")})

    def test_sin_foto_no_se_escanea_y_la_pantalla_lo_pide(self):
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "Foto de llegada")
        self.assertNotContains(respuesta, 'id="form-escanear"')
        respuesta = self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"}, follow=True)
        self.assertContains(respuesta, "Tómale foto al camión/tarimas")
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 0)

    def test_escaneo_recibe_una_pieza_y_manda_a_ubicar_con_el_plan(self):
        from apps.inventario.models import OrdenEntrada, Saldo

        self._foto()
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, 'id="form-escanear"')
        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        self.assertEqual(orden.plan_acomodo["pasos"][0]["ubicacion"], "PIC-1-I-F-2")  # plan por orden: 5 anunciadas
        respuesta = self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"})
        self.assertRedirects(respuesta, f"{self.url_ubicar}?sku={self.sku.pk}", fetch_redirect_response=False)
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 1)
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 1)
        # La pieza contada y sin anaquel sale en la tabla "Por ubicar" con su botón.
        respuesta = self.client.get(self.url)
        self.assertContains(respuesta, "<h2>Por ubicar</h2>")
        self.assertContains(respuesta, f'href="{self.url_ubicar}?sku={self.sku.pk}">Ubicar 1</a>')
        self.assertContains(respuesta, "1 pieza(s) en recepción")
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk})
        self.assertContains(respuesta, 'id="anaquel-sugerido">PIC-1-I-F-2')
        self.assertContains(respuesta, "pieza 1 de 5")
        self.assertContains(respuesta, 'name="lote" value="L-ASN"')  # lote fijo: el anunciado
        respuesta = self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku.pk, "ubicacion": "PIC-1-I-F-2", "lote": "L-ASN", "fecha_caducidad": "2027-01-31"}, follow=True)
        self.assertContains(respuesta, "ubicada en PIC-1-I-F-2")
        saldo = Saldo.objects.get(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE)
        self.assertEqual((saldo.ubicacion.codigo, saldo.lote.codigo, saldo.cantidad), ("PIC-1-I-F-2", "L-ASN", 1))
        orden.refresh_from_db()
        self.assertEqual(orden.plan_acomodo["pasos"][0]["ubicadas"], 1)

    def test_codigo_ajeno_y_sin_piezas_por_ubicar(self):
        self._foto()
        respuesta = self.client.post(self.url, {"accion": "escanear", "codigo": "NO-EXISTE"}, follow=True)
        self.assertContains(respuesta, "no es de un producto de esta orden")
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk}, follow=True)
        self.assertContains(respuesta, "No hay piezas")

    def test_danada_en_ubicar_va_a_cuarentena_y_ajusta_la_linea(self):
        from apps.inventario.models import Saldo

        self._foto()
        self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"})
        respuesta = self.client.post(self.url_ubicar, {"accion": "danada", "sku_id": self.sku.pk}, follow=True)
        self.assertContains(respuesta, "marcada dañada")
        self.linea.refresh_from_db()
        self.assertEqual((self.linea.cantidad_recibida, self.linea.cantidad_danada), (0, 1))
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.EN_PUTAWAY).exists())
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 1)

    def test_sin_espacio_manda_a_cuarentena_y_varios_lotes_piden_elegir(self):
        from apps.inventario.models import LineaASN, Saldo

        self.anaquel.lleno_manual = True
        self.anaquel.save()
        LineaASN.objects.create(orden=self.orden, sku=self.sku, cantidad_anunciada=2, lote_codigo="L-OTRO")
        self._foto()
        self.client.post(self.url, {"accion": "escanear", "codigo": "COLIMITA-SIX"})
        # Con dos lotes anunciados, primero se pregunta el lote; el anaquel viene después.
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk})
        self.assertContains(respuesta, "¿De qué lote es esta pieza?")
        self.assertContains(respuesta, 'value="L-OTRO"')
        self.assertNotContains(respuesta, "CUARENTENA")
        respuesta = self.client.get(self.url_ubicar, {"sku": self.sku.pk, "lote": "L-ASN"})
        self.assertContains(respuesta, "CUARENTENA")
        self.assertContains(respuesta, 'name="lote" value="L-ASN"')
        respuesta = self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku.pk, "ubicacion": "", "lote": "L-ASN"}, follow=True)
        self.assertContains(respuesta, "fue a cuarentena")
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.CUARENTENA).cantidad, 1)

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

    def test_rehacer_el_plan_respeta_lo_ya_ubicado(self):
        from apps.inventario.models import OrdenEntrada
        from apps.inventario.services import planear_acomodo

        self._foto()
        self.client.get(self.url)
        for _ in range(2):  # dos piezas escaneadas y ubicadas por el plan
            self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"})
            self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku.pk, "ubicacion": "PIC-1-I-F-2", "lote": "L-ASN"})
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

    def test_reiniciar_acomodo_regresa_lo_ubicado_por_el_plan(self):
        from apps.inventario.models import OrdenEntrada, Saldo
        from apps.inventario.services import reiniciar_acomodo

        self._foto()
        self.client.get(self.url)
        for _ in range(2):
            self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"})
            self.client.post(self.url_ubicar, {"accion": "ubicar", "sku_id": self.sku.pk, "ubicacion": "PIC-1-I-F-2", "lote": "L-ASN"})
        self.client.post(self.url, {"accion": "escanear", "codigo": "7500000000017"})  # una más, sin ubicar
        orden = OrdenEntrada.objects.get(pk=self.orden.pk)
        r = reiniciar_acomodo(orden, self.operador)
        self.assertEqual(r, {"regresadas": 2, "no_movidas": 0})
        self.assertFalse(Saldo.objects.filter(sku=self.sku, estado=Saldo.UBICADO_VENDIBLE).exists())
        self.assertEqual(Saldo.objects.get(sku=self.sku, estado=Saldo.EN_PUTAWAY).cantidad, 3)
        orden.refresh_from_db()
        paso = orden.plan_acomodo["pasos"][0]
        self.assertEqual((paso["ubicacion"], paso["cantidad"], paso["ubicadas"]), ("PIC-1-I-F-2", 5, 0))
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_recibida, 3)  # recibidas no cambian

    def test_mesa_ve_el_plan_y_lo_rehace(self):
        from apps.inventario.models import OrdenEntrada

        from .base import PisoTestCase  # noqa: F401 (misma base)
        self._foto()
        self.client.get(self.url)
        self.client.logout()
        from django.contrib.auth import get_user_model

        from apps.core.models import PerfilUsuario
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
