"""Tests de manuales en Mesa/Piso + conversor markdown del command.

Los partials pre-renderizados (templates/manuales/) viven en el repo — los
genera `manage.py render_manuales` — así que estos tests NO dependen de la
carpeta fuente ../manuales: usan los templates directo.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import Cliente, PerfilUsuario
from apps.core.services import registrar_evento
from apps.mesa.management.commands.render_manuales import (
    convertir, extraer_propiedades, inline, renderizar_manual,
)

SLUG_SOP02 = "sop-02-recepcion-descarga"


def crear_usuario(username, rol, cliente=None):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


class BaseManuales(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.mesa = crear_usuario("mesa1", "mesa")
        cls.piso = crear_usuario("piso1", "piso")
        cls.portal = crear_usuario("karina", "portal", cliente=cls.colima)


class AccesoManualesTests(BaseManuales):
    def test_lista_mesa_200(self):
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:manuales"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "SOP-02")
        self.assertContains(respuesta, "Recepción y descarga")
        self.assertContains(respuesta, "Operación diaria")
        self.assertContains(respuesta, "min de lectura")

    def test_lista_piso_200(self):
        self.client.force_login(self.piso)
        respuesta = self.client.get(reverse("mesa:manuales"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "SOP-13")

    def test_rol_portal_403_en_mesa(self):
        self.client.force_login(self.portal)
        self.assertEqual(self.client.get(reverse("mesa:manuales")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("mesa:manual_detalle", args=[SLUG_SOP02])).status_code,
            403,
        )

    def test_anonimo_302(self):
        respuesta = self.client.get(reverse("mesa:manuales"))
        self.assertEqual(respuesta.status_code, 302)


class DetalleManualTests(BaseManuales):
    def test_detalle_sop02_con_texto_real(self):
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:manual_detalle", args=[SLUG_SOP02]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "conos")          # cuerpo real del SOP
        self.assertContains(respuesta, "Recepción y descarga")
        self.assertContains(respuesta, "Dueño")          # fila de propiedades
        self.assertContains(respuesta, "En esta página")  # TOC de h2

    def test_detalle_piso_200(self):
        self.client.force_login(self.piso)
        respuesta = self.client.get(reverse("mesa:manual_detalle", args=[SLUG_SOP02]))
        self.assertEqual(respuesta.status_code, 200)

    def test_detalle_inexistente_404(self):
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:manual_detalle", args=["sop-99-nada"]))
        self.assertEqual(respuesta.status_code, 404)


class SugerenciasEnMesaTests(BaseManuales):
    def _sembrar_sugerencia(self):
        registrar_evento(
            "manual", "SOP-02", "sugerencia_cliente",
            actor=self.portal, cliente=self.colima,
            delta={"texto": "El paso 5 merece una foto de los conos."},
        )

    def test_lista_mesa_muestra_sugerencias(self):
        self._sembrar_sugerencia()
        self.client.force_login(self.mesa)
        respuesta = self.client.get(reverse("mesa:manuales"))
        self.assertContains(respuesta, "Sugerencias de clientes")
        self.assertContains(respuesta, "El paso 5 merece una foto de los conos.")
        self.assertContains(respuesta, "Cervecería Colima")

    def test_lista_piso_no_muestra_sugerencias(self):
        self._sembrar_sugerencia()
        self.client.force_login(self.piso)
        respuesta = self.client.get(reverse("mesa:manuales"))
        self.assertNotContains(respuesta, "Sugerencias de clientes")


class ConversorMarkdownTests(TestCase):
    """El conversor propio del command: seguridad primero, formato después."""

    def test_escapa_html_antes_de_formatear(self):
        self.assertIn("&lt;script&gt;", inline("<script>alert(1)</script>"))
        self.assertNotIn("<script>", inline("<script>alert(1)</script>"))

    def test_escapa_llaves_para_django(self):
        # El partial se mete con {% include %}: nada del contenido debe
        # interpretarse como sintaxis de template.
        self.assertNotIn("{{", inline("hola {{ usuario }}"))
        self.assertIn("&#123;", inline("hola {{ usuario }}"))

    def test_inline_negritas_cursivas_codigo(self):
        self.assertEqual(inline("**fuerte**"), "<strong>fuerte</strong>")
        self.assertEqual(inline("*suave*"), "<em>suave</em>")
        self.assertEqual(inline("`btn`"), "<code>btn</code>")
        # dentro de `code` no se aplica más formato
        self.assertEqual(inline("`**crudo**`"), "<code>**crudo**</code>")

    def test_checklist_checkbox_deshabilitado(self):
        _, html, _, _ = convertir("- [ ] pendiente\n- [x] hecho")
        self.assertIn('<input type="checkbox" disabled> pendiente', html)
        self.assertIn('<input type="checkbox" disabled checked> hecho', html)

    def test_callout_parrafo_y_item(self):
        _, html, _, _ = convertir("⚠️ Nadie debajo de la carga.")
        self.assertIn('<div class="callout warn">⚠️ Nadie debajo de la carga.</div>', html)
        _, html, _, _ = convertir("1. Paso normal\n   - ⚠️ Cuidado aquí")
        self.assertIn('<li class="warn">⚠️ Cuidado aquí</li>', html)

    def test_lista_numerada_conserva_arranque(self):
        # Los SOPs numeran pasos a través de secciones: 4..8 no debe volverse 1..5.
        _, html, _, _ = convertir("4. Cuarto paso\n5. Quinto paso")
        self.assertIn('<ol start="4">', html)

    def test_tabla_y_separador(self):
        _, html, _, _ = convertir("| A | B |\n|---|---|\n| 1 | 2 |\n\n---")
        self.assertIn('<div class="tabla-wrap"><table>', html)
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)
        self.assertIn("<hr>", html)

    def test_bloque_preformateado(self):
        _, html, _, _ = convertir("```\n[ ] casilla imprimible\n```")
        self.assertIn('<pre class="pre-manual">[ ] casilla imprimible</pre>', html)

    def test_h2_genera_id_y_toc(self):
        _, html, toc, _ = convertir("## A. Antes de que llegue el camión")
        self.assertEqual(toc, [{
            "id": "a-antes-de-que-llegue-el-camion",
            "titulo": "A. Antes de que llegue el camión",
        }])
        self.assertIn('<h2 id="a-antes-de-que-llegue-el-camion">', html)

    def test_propiedades_del_encabezado(self):
        md = (
            "# SOP-99 · De prueba\n\n"
            "| Campo | Valor |\n|---|---|\n"
            "| **Código / versión** | SOP-99 · v1.0 · Agosto 2026 |\n"
            "| **Dueño** | OP-1 Jefe de bodega |\n\n"
            "Primer párrafo del cuerpo.\n"
        )
        props, version, resto = extraer_propiedades(md)
        self.assertEqual(props, [["Dueño", "OP-1 Jefe de bodega"]])
        self.assertEqual(version, "v1.0 · Agosto 2026")
        self.assertNotIn("| Campo |", resto)
        slug, entrada, cuerpo = renderizar_manual("SOP-99-de-prueba.md", md)
        self.assertEqual(slug, "sop-99-de-prueba")
        self.assertEqual(entrada["codigo"], "SOP-99")
        self.assertEqual(entrada["titulo"], "De prueba")
        self.assertEqual(entrada["resumen"], "Primer párrafo del cuerpo.")
        self.assertIn("<p>Primer párrafo del cuerpo.</p>", cuerpo)


class IndiceGeneradoTests(TestCase):
    """El índice commiteado en templates/manuales/ está completo y coherente."""

    def test_indice_trae_los_14_manuales(self):
        from apps.core.manuales import cargar_indice, manuales_publicados, portada

        self.assertEqual(len(cargar_indice()), 14)
        publicados = manuales_publicados()
        self.assertEqual(len(publicados), 13)
        self.assertEqual(publicados[0]["codigo"], "SOP-01")
        self.assertEqual(portada()["codigo"], "README")
        for manual in publicados:
            self.assertTrue(manual["emoji"])
            self.assertGreater(manual["palabras"], 100)
            self.assertEqual(len(manual["propiedades"]), 5)

    def test_grupos_del_indice(self):
        from apps.core.manuales import agrupar, manuales_publicados

        grupos = agrupar(manuales_publicados())
        self.assertEqual(
            [(g["nombre"], len(g["manuales"])) for g in grupos],
            [("Operación diaria", 8), ("Control", 2), ("Seguridad", 3)],
        )
