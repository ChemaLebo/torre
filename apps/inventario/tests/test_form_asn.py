"""FormAnuncioASNBase: renglones dinámicos, dropdown por categoría y CSV de renglones."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import SKU, Categoria
from apps.core.models import Cliente
from apps.inventario.forms import FormAnuncioASNBase


def hoy():
    return timezone.localdate().isoformat()


class BaseFormASN(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cliente = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        tes = Categoria.objects.create(cliente=cls.cliente, nombre="Tés")
        cls.te = SKU.objects.create(
            cliente=cls.cliente, codigo="TE-1", descripcion="Té verde", categoria=tes,
        )
        cls.taza = SKU.objects.create(cliente=cls.cliente, codigo="TAZA-1", descripcion="Taza")


class RenglonesDinamicosTests(BaseFormASN):
    def test_acepta_mas_renglones_que_los_iniciales(self):
        datos = {"fecha_compromiso": hoy()}
        for i in range(1, 11):
            datos[f"sku_{i}"] = str(self.te.pk)
            datos[f"cantidad_{i}"] = "2"
        form = FormAnuncioASNBase(self.cliente, datos)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["lineas"], [(self.te, 20)])  # consolidado

    def test_renglon_incompleto_es_error(self):
        form = FormAnuncioASNBase(
            self.cliente, {"fecha_compromiso": hoy(), "sku_1": str(self.te.pk)},
        )
        self.assertFalse(form.is_valid())

    def test_dropdown_agrupado_con_otros_al_final(self):
        form = FormAnuncioASNBase(self.cliente)
        choices = list(form.fields["sku_1"].choices)
        self.assertEqual(choices[0], ("", "Elige un producto"))
        self.assertEqual([grupo for grupo, _ in choices[1:]], ["Tés", Categoria.OTROS])
        self.assertEqual(choices[1][1], [(self.te.pk, "Té verde — TE-1")])
        self.assertEqual(choices[2][1], [(self.taza.pk, "Taza — TAZA-1")])


class RenglonesCSVTests(BaseFormASN):
    def _form_con_csv(self, contenido):
        archivo = SimpleUploadedFile(
            "renglones.csv", contenido.encode("utf-8-sig"), content_type="text/csv",
        )
        return FormAnuncioASNBase(
            self.cliente, {"fecha_compromiso": hoy()}, {"renglones_csv": archivo},
        )

    def test_csv_reemplaza_renglones_y_consolida(self):
        form = self._form_con_csv("codigo,cantidad\nTE-1,5\nTAZA-1,2\nTE-1,1\n")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(dict(form.cleaned_data["lineas"]), {self.te: 6, self.taza: 2})

    def test_csv_con_sku_desconocido_lista_el_error(self):
        form = self._form_con_csv("codigo,cantidad\nNO-EXISTE,5\n")
        self.assertFalse(form.is_valid())
        self.assertIn("SKU desconocido", str(form.errors))

    def test_csv_sin_encabezados_correctos_es_error(self):
        form = self._form_con_csv("sku,piezas\nTE-1,5\n")
        self.assertFalse(form.is_valid())
        self.assertIn("codigo,cantidad", str(form.errors))
