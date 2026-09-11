"""Reporte del día (pedidos.reportes): qué pedidos entran por fecha y cliente,
cómo se catalogan las fotos por etapa, quién hizo cada paso y el CSV."""
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Cliente, EvidenciaFoto, PerfilUsuario
from apps.envios.models import Guia, Paquete
from apps.incidencias.models import Incidencia
from apps.integraciones.models import Tienda
from apps.pedidos import reportes
from apps.pedidos.models import Pedido

PNG = b"\x89PNG\r\n\x1a\n"


def aware(fecha, hora=10):
    return timezone.make_aware(datetime.combine(fecha, time(hora, 0)))


@override_settings(MEDIA_ROOT="/tmp/torre-test-reportes")
class ReporteDiaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hoy = timezone.localdate()
        cls.ayer = cls.hoy - timedelta(days=1)
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.otro = Cliente.objects.create(nombre="Mezcal Nocturno", slug="nocturno")
        cls.tienda = Tienda.objects.create(cliente=cls.colima, dominio="colima-mx.myshopify.com")
        cls.piso = get_user_model().objects.create_user("piso1", password="x12345678")
        PerfilUsuario.objects.create(usuario=cls.piso, rol="piso")

    def pedido(self, cliente=None, creado=None, **extra):
        pedido = Pedido.objects.create(
            cliente=cliente or self.colima, comprador_nombre="Ana", cp="28017", **extra,
        )
        campos = {"actualizado": creado or aware(self.hoy)}
        if creado is not None:
            campos["creado"] = creado
        Pedido.objects.filter(pk=pedido.pk).update(**campos)
        pedido.refresh_from_db()
        return pedido

    def foto(self, entidad, entidad_id, tipo, tomada_por="piso1"):
        return EvidenciaFoto.objects.create(
            entidad=entidad, entidad_id=str(entidad_id), tipo=tipo,
            archivo=ContentFile(PNG, name=f"{entidad_id}-{tipo}.png"), tomada_por=tomada_por,
        )

    # ── Qué entra ──

    def test_entra_lo_creado_o_con_paso_o_actualizado_ese_dia(self):
        hoy_creado = self.pedido(creado=aware(self.hoy))
        viejo_entregado_hoy = self.pedido(creado=aware(self.ayer), ts_entregado=aware(self.hoy, 12), estado=Pedido.ENTREGADO)
        viejo_tocado_hoy = self.pedido(creado=aware(self.ayer))
        viejo_sin_nada = self.pedido(creado=aware(self.ayer))
        Pedido.objects.filter(pk=viejo_sin_nada.pk).update(actualizado=aware(self.ayer))
        Pedido.objects.filter(pk=viejo_tocado_hoy.pk).update(actualizado=aware(self.hoy, 15))

        hoy = set(reportes.pedidos_con_actividad(self.hoy))
        self.assertEqual(hoy, {hoy_creado, viejo_entregado_hoy, viejo_tocado_hoy})
        ayer = set(reportes.pedidos_con_actividad(self.ayer))
        self.assertEqual(ayer, {viejo_entregado_hoy, viejo_tocado_hoy, viejo_sin_nada})

    def test_filtra_por_cliente(self):
        mio = self.pedido()
        ajeno = self.pedido(cliente=self.otro)
        self.assertEqual(list(reportes.pedidos_con_actividad(self.hoy, self.colima)), [mio])
        self.assertEqual(set(reportes.pedidos_con_actividad(self.hoy)), {mio, ajeno})

    def test_fecha_desde_get(self):
        self.assertEqual(reportes.fecha_desde_get("2026-09-10"), (date(2026, 9, 10), True))
        self.assertEqual(reportes.fecha_desde_get(""), (self.hoy, True))
        self.assertEqual(reportes.fecha_desde_get("10/09/2026"), (self.hoy, False))

    # ── Etapas, fotos y quién ──

    def test_fotos_por_etapa_y_operador_solo_en_mesa(self):
        pedido = self.pedido(
            tienda=self.tienda, shopify_order_id="5501", estado=Pedido.ENTREGADO,
            ts_picking=aware(self.hoy, 9), ts_empacado=aware(self.hoy, 10), ts_guia=aware(self.hoy, 10),
            ts_recolectado=aware(self.hoy, 12), ts_entregado=aware(self.hoy, 16),
        )
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.PENDIENTE)
        pedido.refresh_from_db()
        pedido.transicionar(Pedido.EN_PICKING, actor=self.piso)
        pedido.transicionar(Pedido.EMPACADO, actor=self.piso)
        Pedido.objects.filter(pk=pedido.pk).update(estado=Pedido.ENTREGADO)
        paquete = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=1)
        contenido = self.foto("pedido", pedido.pk, "contenido")
        cerrada = self.foto("pedido", pedido.folio, "caja_cerrada")
        from apps.core.services import registrar_evento
        registrar_evento("paquete", paquete.pk, "caja_cerrada_con_evidencia", actor=self.piso,
                         delta={"caja": 1, "evidencia_id": cerrada.pk})
        pod = self.foto("entrega_local", pedido.pk, "pod", tomada_por="jefe")
        Guia.objects.create(pedido=pedido, carrier="fedex", numero="7788 9900", proveedor="mock")
        inc = Incidencia.objects.create(cliente=self.colima, pedido=pedido, tipo="DAN", origen="comprador")
        self.foto("incidencia", inc.folio, "dano", tomada_por="comprador")

        [r] = reportes.armar_reporte(reportes.pedidos_con_actividad(self.hoy), con_operador=True)
        etapas = {e["clave"]: e for e in r["etapas"]}
        self.assertEqual([e["clave"] for e in r["etapas"]],
                         ["recibido", "picking", "empaque", "guia", "salida", "transito", "entrega"])
        self.assertEqual([f["foto"] for f in etapas["empaque"]["fotos"]], [contenido, cerrada])
        self.assertEqual([f["etiqueta"] for f in etapas["empaque"]["fotos"]], ["Contenido", "Caja cerrada · caja 1"])
        self.assertEqual([f["foto"] for f in etapas["entrega"]["fotos"]], [pod])
        self.assertEqual((etapas["picking"]["operador"], etapas["empaque"]["operador"]), ("piso1", "piso1"))
        self.assertFalse(etapas["transito"]["hecha"])
        self.assertEqual(r["shopify_url"], "https://colima-mx.myshopify.com/admin/orders/5501")
        self.assertEqual(r["rastreo_url"], "https://www.fedex.com/fedextrack/?trknbr=7788%209900")
        self.assertEqual(r["fotos_total"], 3)
        self.assertEqual(r["resumen_fotos"], "empaque 2 · entrega 1")
        self.assertEqual((r["incidencias"][0]["fotos"], r["incidencias"][0]["abierta"]), (1, True))

        [sin] = reportes.armar_reporte(reportes.pedidos_con_actividad(self.hoy), con_operador=False)
        self.assertTrue(all(e["operador"] == "" for e in sin["etapas"]))

    def test_cancelado_agrega_etapa_de_cierre_y_sin_tienda_no_hay_link(self):
        pedido = self.pedido()
        pedido.transicionar(Pedido.CANCELADO, actor=self.piso, motivo="prueba")
        [r] = reportes.armar_reporte(reportes.pedidos_con_actividad(self.hoy), con_operador=True)
        cierre = r["etapas"][-1]
        self.assertEqual((cierre["clave"], cierre["etiqueta"], cierre["operador"]), ("cierre", "Cancelado", "piso1"))
        self.assertEqual(r["shopify_url"], "")
        self.assertIsNone(r["guia"])
        self.assertEqual(r["rastreo_url"], "")
        self.assertEqual(reportes.resumen_estados([r]), [("Cancelado", "", 1)])

    def test_carrier_sin_patron_no_lleva_link(self):
        pedido = self.pedido()
        Guia.objects.create(pedido=pedido, carrier="local", numero="L-1", proveedor="mock")
        [r] = reportes.armar_reporte([pedido])
        self.assertEqual(r["rastreo_url"], "")

    def test_csv(self):
        pedido = self.pedido(tienda=self.tienda, shopify_order_id="5502")
        foto = self.foto("pedido", pedido.pk, "contenido")
        renglones = reportes.armar_reporte([pedido])
        [fila] = reportes.filas_csv(renglones, lambda f: f"http://t/evidencia/{f.pk}/")
        self.assertEqual(fila[0], pedido.folio)
        self.assertEqual(fila[1], "5502")
        self.assertEqual(fila[-3:], [1, 0, f"http://t/evidencia/{foto.pk}/"])
        self.assertEqual(len(fila), len(reportes.COLUMNAS_CSV))
