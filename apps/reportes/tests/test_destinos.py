"""Pedidos por código postal y por ciudad (Chema 2026-10-15): volumen de envíos
por destino sin reposiciones, con lo pagado a 99minutos y la tarifa estimada
para los CP que 99minutos no ha cotizado."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.envios.models import Guia, LocalidadCP, Paquete, PaqueteLinea
from apps.pedidos.models import LineaPedido, Pedido
from apps.reportes import ciudades, destinos
from apps.reportes.base import limites

from .base import ReportesTestCase


class DestinosTests(ReportesTestCase):
    def setUp(self):
        hoy = timezone.localdate()
        self.desde, self.hasta = hoy - timedelta(days=7), hoy
        self.inicio, self.fin = limites(self.desde, self.hasta)
        LocalidadCP.objects.create(cp="45019", localidad="Zapopan", municipio="Zapopan", estado="JA")
        LocalidadCP.objects.create(cp="45200", localidad="El Álamo", municipio="Zapopan", estado="JA")
        # CDMX con dos cajas: la 2 repone a la 1 y no cuenta; la 1 viajó por 99minutos.
        p1 = self._pedido("03100", "Mexico", Pedido.ENTREGADO)
        c1 = self._caja(p1, 1, "noventa9Minutos", "99minutos", "110.20")
        self._caja(p1, 2, "estafeta", "envia", "150.00", repone_a=c1)
        # Zapopan: pedido que salió entero (sin cajas) con guía de 99minutos; El Álamo es el mismo municipio.
        p2 = self._pedido("45019", "Zapopan", Pedido.EN_TRANSITO)
        Guia.objects.create(pedido=p2, carrier="noventa9Minutos", numero="G-p2", proveedor="99minutos",
                            estado=Guia.EN_TRANSITO, costo_cotizado=Decimal("131.08"))
        p3 = self._pedido("45200", "Zapopan", Pedido.ENTREGADO)
        self._caja(p3, 1, "estafeta", "envia", "160.00")
        # CDMX sin catálogo: la alcaldía sale del prefijo del CP; el comprador escribió "CDMX".
        p5 = self._pedido("06700", "CDMX", Pedido.RECOLECTADO)
        self._caja(p5, 1, "noventa9Minutos", "99minutos", "99.76")
        # Guadalajara sin catálogo ni 99minutos: la tarifa se estima por estado y zona.
        p6 = self._pedido("44100", "guadalajara, Jalisco", Pedido.ENTREGADO)
        self._caja(p6, 1, "estafeta", "envia", "140.00")
        # Fuera: un pedido que no ha salido y uno de otro cliente.
        self._pedido("03100", "Mexico", Pedido.PENDIENTE)
        Pedido.objects.create(cliente=self.otro, comprador_nombre="Ajeno", cp="03100", estado=Pedido.ENTREGADO)

    def _pedido(self, cp, ciudad, estado):
        return Pedido.objects.create(
            cliente=self.colima, comprador_nombre=f"C-{cp}", cp=cp, estado=estado, direccion={"city": ciudad, "zip": cp},
        )

    def _caja(self, pedido, numero, carrier, proveedor, costo, repone_a=None):
        linea = pedido.lineas.first() or LineaPedido.objects.create(
            pedido=pedido, sku=self.six, cantidad=2, reservada=True, cantidad_pickeada=2,
        )
        caja = Paquete.objects.create(pedido=pedido, numero=numero, peso_kg=Decimal(4), carrier=carrier, estado=Paquete.DESPACHADO)
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1, repone_a=repone_a)
        Guia.objects.create(pedido=pedido, paquete=caja, carrier=carrier, numero=f"G-{pedido.pk}-{numero}",
                            proveedor=proveedor, estado=Guia.ENTREGADO, costo_cotizado=Decimal(costo))
        return caja

    def test_por_codigo_postal(self):
        r = destinos.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        hoy = timezone.localdate()
        self.assertEqual(filas["03100"], ["03100", "Ciudad de México", "Benito Juárez", "Ciudad de México", "local", 1, 1, hoy, hoy,
                                          1, Decimal("110.20"), Decimal("110.20"), Decimal("110.20"), "real del CP"])
        self.assertEqual(filas["06700"][1:3], ["Ciudad de México", "Cuauhtémoc"])
        self.assertEqual(filas["45200"][:5], ["45200", "El Álamo", "Zapopan", "Jalisco", "metro"])
        self.assertEqual(filas["45200"][9:], [0, None, None, Decimal("131.08"), "real de la ciudad"])
        self.assertEqual(filas["44100"][1:5], ["Guadalajara", "", "Jalisco", "metro"])
        self.assertEqual(filas["44100"][12:], [Decimal("131.08"), "promedio del estado y zona"])
        self.assertEqual(filas["Total"][5:7], [5, 5])
        self.assertEqual(filas["Total"][9:12], [3, Decimal("113.68"), Decimal("341.04")])
        self.assertEqual(dict(r["resumen"])["códigos postales"], 5)
        self.assertEqual([f[0] for f in r["filas"]], ["03100", "06700", "44100", "45019", "45200", "Total"])
        # Portal: sin las columnas de costo.
        portal = destinos.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(destinos.COLUMNAS))
        self.assertNotIn("pagado a 99minutos", dict(portal["resumen"]))
        # Filtro de zona.
        local = destinos.generar(self.colima, self.inicio, self.fin, {"zona": "local"}, es_mesa=True)
        self.assertEqual([f[0] for f in local["filas"]], ["03100", "06700", "Total"])

    def test_por_ciudad(self):
        r = ciudades.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=True)
        filas = {f[0]: f for f in r["filas"]}
        self.assertEqual(filas["Ciudad de México"], ["Ciudad de México", "Ciudad de México", "local", 2, 2, 2,
                                                     2, Decimal("104.98"), Decimal("209.96"), Decimal("104.98"), "real de la ciudad", Decimal("209.96")])
        self.assertEqual(filas["Zapopan"], ["Zapopan", "Jalisco", "metro", 2, 2, 2,
                                            1, Decimal("131.08"), Decimal("131.08"), Decimal("131.08"), "real de la ciudad", Decimal("262.16")])
        self.assertEqual(filas["Guadalajara"][6:], [0, None, None, Decimal("131.08"), "promedio del estado y zona", Decimal("131.08")])
        self.assertEqual(filas["Total"][3:6], [5, 5, 5])
        self.assertEqual(filas["Total"][-1], Decimal("603.20"))
        self.assertEqual([f[0] for f in r["filas"]], ["Ciudad de México", "Zapopan", "Guadalajara", "Total"])
        self.assertEqual(dict(r["resumen"])["costo estimado si todo fuera 99"], Decimal("603.20"))
        portal = ciudades.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=False)
        self.assertEqual(len(portal["filas"][0]), len(ciudades.COLUMNAS))

    def test_la_misma_ciudad_escrita_distinto_cae_junta(self):
        for ciudad in ("LEÓN", "Leon", "león, Guanajuato"):
            self._caja(self._pedido("37545", ciudad, Pedido.ENTREGADO), 1, "estafeta", "envia", "140.00")
        r = ciudades.generar(self.colima, self.inicio, self.fin, {"zona": ""}, es_mesa=False)
        leon = [f for f in r["filas"] if f[1] == "Guanajuato"]
        self.assertEqual(leon, [["León", "Guanajuato", "nacional", 1, 3, 3]])

    def test_csv_desde_mesa_y_portal(self):
        fechas = {"desde": self.desde.isoformat(), "hasta": self.hasta.isoformat()}
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:reporte_csv", args=["ciudades"]), fechas)
        self.assertEqual(respuesta.status_code, 200)
        cuerpo = respuesta.content.decode("utf-8-sig")
        self.assertTrue(cuerpo.startswith("Cliente,Ciudad,Estado,Zona,Códigos postales,Pedidos,Paquetes,Paquetes 99minutos"), cuerpo[:120])
        self.assertIn("Cervecería Colima,Ciudad de México,Ciudad de México,local,2,2,2,2,104.98,209.96,104.98,real de la ciudad,209.96", cuerpo)
        self.client.force_login(self.portal)
        respuesta = self.client.get(reverse("portal:reporte_csv", args=["destinos"]), fechas)
        cuerpo = respuesta.content.decode("utf-8-sig")
        self.assertTrue(cuerpo.startswith("CP,Ciudad,Municipio / alcaldía,Estado,Zona,Pedidos,Paquetes,Primer pedido,Último pedido"), cuerpo[:120])
        self.assertNotIn("Tarifa", cuerpo)
        self.assertIn("45200,El Álamo,Zapopan,Jalisco,metro,1,1,", cuerpo)
