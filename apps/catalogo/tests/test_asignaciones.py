"""Producto fijo por posición (Chema 2026-10-13): máximo dos por posición, un
producto en una sola posición, capacidad repartida, y la carga desde el plan
de acomodo con `cargar_asignaciones`."""
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.catalogo.models import SKU, AsignacionPosicion, Ubicacion
from apps.catalogo.services import asignaciones_de, asignar_producto, desasignar_producto, rack_de
from apps.core.models import Cliente, EventoAuditoria
from apps.inventario.services import capacidad_asignada, capacidad_sku_en


class AsignacionesBase(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima", integracion_envios="envia")
        self.s1, self.s2, self.s3 = (
            SKU.objects.create(cliente=self.cliente, codigo=c, descripcion=c, largo_cm=26, ancho_cm=20, alto_cm=25)
            for c in ("CCPL24L", "12PCCSA", "CC24LCAR")
        )
        self.a = Ubicacion.objects.create(codigo="A-1", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.c = Ubicacion.objects.create(codigo="C-3", tipo=Ubicacion.PICKING, largo_cm=180, ancho_cm=58, alto_cm=52)
        self.merch = Ubicacion.objects.create(codigo="A-8", tipo=Ubicacion.RESERVA)
        self.libre = Ubicacion.objects.create(codigo="D-9", tipo=Ubicacion.PICKING)
        self.inactiva = Ubicacion.objects.create(codigo="X-1", tipo=Ubicacion.PICKING, activo=False)


class ServiciosTests(AsignacionesBase):
    def test_maximo_dos_por_posicion_y_un_producto_en_una_sola(self):
        self.assertEqual(asignar_producto(self.a, self.s1, actor="mesa1").orden, 1)
        self.assertEqual(asignar_producto(self.a, self.s2).orden, 2)
        with self.assertRaisesMessage(ValueError, "A-1 ya tiene 2 productos (CCPL24L, 12PCCSA)"):
            asignar_producto(self.a, self.s3)
        with self.assertRaisesMessage(ValueError, "no es una posición de almacén activa"):
            asignar_producto(self.inactiva, self.s3)
        self.assertEqual(asignar_producto(self.a, self.s1).orden, 1)  # repetir no duplica
        movida = asignar_producto(self.c, self.s2)
        self.assertEqual((movida.ubicacion, movida.orden), (self.c, 1))
        self.assertEqual([a.sku for a in asignaciones_de(self.a)], [self.s1])
        self.assertEqual(rack_de(self.s2), self.c)
        evento = EventoAuditoria.objects.filter(entidad="ubicacion", entidad_id="C-3", accion="producto_asignado").get()
        self.assertEqual(evento.delta, {"sku": "12PCCSA", "orden": 1, "antes": "A-1"})
        self.assertEqual(desasignar_producto(self.s1, actor="mesa1"), self.a)
        self.assertIsNone(rack_de(self.s1))
        with self.assertRaisesMessage(ValueError, "no tiene posición fija"):
            desasignar_producto(self.s1)
        self.assertEqual(AsignacionPosicion.objects.count(), 1)

    def test_capacidad_se_reparte_entre_los_fijos(self):
        self.assertEqual(capacidad_sku_en(self.a, self.s1), 36)
        asignar_producto(self.a, self.s1)
        self.assertEqual(capacidad_asignada(self.a, self.s1), 36)
        asignar_producto(self.a, self.s2)
        self.assertEqual((capacidad_asignada(self.a, self.s1), capacidad_sku_en(self.a, self.s1, 2)), (16, 16))
        self.assertIsNone(capacidad_asignada(self.libre, self.s1))  # sin medidas


class ComandoCargarAsignacionesTests(AsignacionesBase):
    def escribir(self, filas):
        carpeta = Path(tempfile.mkdtemp())
        archivo = carpeta / "plan-acomodo-posiciones.csv"
        archivo.write_text("nombre_nuevo,modo,producto\n" + "".join(f"{p},{m},{s}\n" for p, m, s in filas), encoding="utf-8")
        return str(archivo)

    def test_dry_run_aplicar_y_recarga(self):
        archivo = self.escribir([("A-1", "sencilla", "CCPL24L"), ("C-3", "doble (½ largo)", "12PCCSA"), ("C-3", "doble (½ largo)", "CC24LCAR"),
                                 ("A-8", "repisa de merch", "MERCH"), ("D-9", "libre", "")])
        salida = StringIO()
        call_command("cargar_asignaciones", archivo=archivo, stdout=salida)
        self.assertIn("A-1: CCPL24L · pone CCPL24L", salida.getvalue())
        self.assertIn("Dry-run", salida.getvalue())
        self.assertEqual(AsignacionPosicion.objects.count(), 0)
        call_command("cargar_asignaciones", archivo=archivo, aplicar=True, stdout=StringIO())
        self.assertEqual({(a.ubicacion.codigo, a.sku.codigo, a.orden) for a in AsignacionPosicion.objects.all()},
                         {("A-1", "CCPL24L", 1), ("C-3", "12PCCSA", 1), ("C-3", "CC24LCAR", 2)})
        # Recarga con cambios: A-1 ahora lleva 12PCCSA (se mueve) y CCPL24L sale.
        archivo = self.escribir([("A-1", "sencilla", "12PCCSA"), ("C-3", "doble (½ largo)", "CC24LCAR")])
        call_command("cargar_asignaciones", archivo=archivo, aplicar=True, stdout=StringIO())
        self.assertEqual({(a.ubicacion.codigo, a.sku.codigo) for a in AsignacionPosicion.objects.all()},
                         {("A-1", "12PCCSA"), ("C-3", "CC24LCAR")})

    def test_errores_no_escriben_nada(self):
        archivo = self.escribir([("A-1", "sencilla", "NOEXISTE"), ("Z-9", "sencilla", "CCPL24L"), ("C-3", "doble", "CCPL24L"), ("C-3", "doble", "12PCCSA"), ("C-3", "doble", "CC24LCAR")])
        with self.assertRaises(CommandError) as ctx:
            call_command("cargar_asignaciones", archivo=archivo, aplicar=True, stdout=StringIO())
        texto = str(ctx.exception)
        self.assertIn("NOEXISTE no existe", texto)
        self.assertIn("Z-9: no existe", texto)
        self.assertIn("C-3: 3 productos", texto)
        self.assertEqual(AsignacionPosicion.objects.count(), 0)
