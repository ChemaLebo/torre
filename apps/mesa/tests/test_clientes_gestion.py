"""Tests de la gestión de clientes desde Mesa (vertical alta de clientes).

Cubre: alta/edición de Cliente (slug, branding, delta de auditoría), editor de
tarifario (semántica de overrides), usuarios del portal (password solo en el
flash, jamás en la auditoría), catálogo de SKUs (alta/edición/CSV) y tiendas
Shopify — más el control de acceso por rol de todas las vistas nuevas.
"""
import json
import re
from decimal import Decimal

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Cliente, EventoAuditoria, PerfilUsuario

from .test_vistas import crear_usuario

RE_PASSWORD = re.compile(r"Contraseña: ([A-Za-z0-9_-]+)")


def datos_form_cliente(**extra):
    """POST mínimo válido del FormCliente (checkboxes prendidos)."""
    datos = {
        "nombre": "Ron Caney",
        "slug": "",
        "razon_social": "",
        "rfc": "",
        "contacto_nombre": "",
        "contacto_whatsapp": "",
        "buffer_stock": "0",
        "carrier_preferente": "paquetexpress",
        "naked_packing_local": "on",
        "umbral_visto_bueno_mxn": "2000",
        "guia_de_voz": "",
        "activo": "on",
    }
    datos.update(extra)
    return datos


class BaseGestionClientes(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.colima = Cliente.objects.create(nombre="Cervecería Colima", slug="colima")
        cls.usuario_mesa = crear_usuario("mesa1", "mesa", pin="3333")
        cls.usuario_portal = crear_usuario("karina", "portal", cliente=cls.colima)
        cls.usuario_piso = crear_usuario("piso1", "piso", pin="1111")

    def setUp(self):
        self.client.force_login(self.usuario_mesa)


class AltaClienteTests(BaseGestionClientes):
    def test_alta_con_slug_registra_evento(self):
        respuesta = self.client.post(
            reverse("mesa:cliente_nuevo"),
            datos_form_cliente(slug="ron-caney", contacto_nombre="Caridad Peña"),
            follow=True,
        )
        cliente = Cliente.objects.get(slug="ron-caney")
        self.assertRedirects(respuesta, reverse("mesa:cliente_detalle", args=[cliente.pk]))
        self.assertEqual(cliente.nombre, "Ron Caney")
        self.assertEqual(cliente.contacto_nombre, "Caridad Peña")
        self.assertTrue(cliente.activo)
        evento = EventoAuditoria.objects.get(entidad="cliente", entidad_id="ron-caney", accion="alta")
        self.assertEqual(evento.actor_id, "mesa1")
        self.assertEqual(evento.cliente, cliente)
        self.assertEqual(evento.motivo, "Alta desde Mesa de Control")
        self.assertContains(
            respuesta,
            "Cliente creado. Siguiente: carga sus SKUs, crea su usuario del portal y revisa su tarifario.",
        )

    def test_alta_sin_slug_lo_genera_del_nombre(self):
        self.client.post(
            reverse("mesa:cliente_nuevo"),
            datos_form_cliente(nombre="Café de Olla MX", slug=""),
        )
        cliente = Cliente.objects.get(nombre="Café de Olla MX")
        self.assertEqual(cliente.slug, "cafe-de-olla-mx")
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="cliente", entidad_id="cafe-de-olla-mx", accion="alta"
            ).exists()
        )

    def test_slug_duplicado_no_crea_y_avisa(self):
        antes = Cliente.objects.count()
        respuesta = self.client.post(
            reverse("mesa:cliente_nuevo"), datos_form_cliente(slug="colima")
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(Cliente.objects.count(), antes)
        self.assertContains(respuesta, "Ya existe un cliente con el slug")

    def test_branding_solo_guarda_claves_no_vacias(self):
        self.client.post(
            reverse("mesa:cliente_nuevo"),
            datos_form_cliente(
                slug="ron-caney",
                brand_color_primario="#123456",
                brand_nombre_publico="Ron Caney de Cuba",
                brand_logo_url="",
            ),
        )
        cliente = Cliente.objects.get(slug="ron-caney")
        self.assertEqual(
            cliente.branding,
            {"color_primario": "#123456", "nombre_publico": "Ron Caney de Cuba"},
        )


class EdicionClienteTests(BaseGestionClientes):
    def test_edicion_registra_delta_solo_de_cambios(self):
        url = reverse("mesa:cliente_editar", args=[self.colima.pk])
        respuesta = self.client.post(url, datos_form_cliente(
            nombre="Cervecería Colima",
            buffer_stock="5",
            contacto_nombre="Karina Fuentes",
        ))
        self.assertRedirects(respuesta, reverse("mesa:cliente_detalle", args=[self.colima.pk]))
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.buffer_stock, 5)
        self.assertEqual(self.colima.contacto_nombre, "Karina Fuentes")
        evento = EventoAuditoria.objects.get(entidad="cliente", entidad_id="colima", accion="edicion")
        self.assertEqual(set(evento.delta), {"buffer_stock", "contacto_nombre"})
        self.assertEqual(evento.delta["buffer_stock"], [0, 5])
        self.assertEqual(evento.delta["contacto_nombre"], ["", "Karina Fuentes"])

    def test_edicion_no_cambia_el_slug(self):
        url = reverse("mesa:cliente_editar", args=[self.colima.pk])
        self.client.post(url, datos_form_cliente(nombre="Cervecería Colima", slug="hackeado"))
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.slug, "colima")

    def test_edicion_sin_cambios_no_registra_evento(self):
        url = reverse("mesa:cliente_editar", args=[self.colima.pk])
        self.client.post(url, datos_form_cliente(nombre="Cervecería Colima"))
        self.assertFalse(
            EventoAuditoria.objects.filter(entidad="cliente", accion="edicion").exists()
        )


class TarifarioTests(BaseGestionClientes):
    def setUp(self):
        super().setUp()
        self.url = reverse("mesa:cliente_tarifario", args=[self.colima.pk])

    def test_guarda_solo_overrides_y_zonas_que_difieren(self):
        default = settings.TORRE["TARIFARIO_DEFAULT"]
        self.client.post(self.url, {
            "almacenaje_mes": "20000",                              # difiere → override
            "alistamiento_pedido": str(default["alistamiento_pedido"]),  # igual → fuera
            "envio_local": "150",                                   # difiere → override
            "envio_metro": str(default["envio_bloque"]["metro"]),   # igual → fuera
        })
        self.colima.refresh_from_db()
        self.assertEqual(
            self.colima.tarifario,
            {"almacenaje_mes": 20000, "envio_bloque": {"local": 150}},
        )

    def test_valores_iguales_al_default_no_se_guardan(self):
        default = settings.TORRE["TARIFARIO_DEFAULT"]
        self.client.post(self.url, {
            "almacenaje_mes": str(default["almacenaje_mes"]),
            "empaque_pedido": str(default["empaque_pedido"]),
            "envio_nacional": str(default["envio_bloque"]["nacional"]),
        })
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.tarifario, {})

    def test_vaciar_campo_quita_el_override(self):
        self.colima.tarifario = {"empaque_pedido": 80, "envio_bloque": {"nacional": 250}}
        self.colima.save(update_fields=["tarifario"])
        self.client.post(self.url, {})  # todo vacío = todo vuelve al default
        self.colima.refresh_from_db()
        self.assertEqual(self.colima.tarifario, {})

    def test_registra_evento_con_antes_y_despues(self):
        self.colima.tarifario = {"empaque_pedido": 80}
        self.colima.save(update_fields=["tarifario"])
        self.client.post(self.url, {"almacenaje_mes": "20000"})
        evento = EventoAuditoria.objects.get(
            entidad="cliente", entidad_id="colima", accion="tarifario_actualizado"
        )
        self.assertEqual(evento.delta["antes"], {"empaque_pedido": 80})
        self.assertEqual(evento.delta["despues"], {"almacenaje_mes": 20000})
        self.assertEqual(evento.actor_id, "mesa1")

    def test_get_prefillea_el_valor_efectivo_con_pill_propio(self):
        self.colima.tarifario = {"almacenaje_mes": 20000}
        self.colima.save(update_fields=["tarifario"])
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'value="20000"')  # efectivo, no el default
        self.assertContains(respuesta, "tarifario propio")
        self.assertContains(respuesta, "propio")
        self.assertContains(respuesta, "(default: $18000)")


class UsuariosPortalTests(BaseGestionClientes):
    def setUp(self):
        super().setUp()
        self.url = reverse("mesa:cliente_detalle", args=[self.colima.pk])

    def test_usuario_nuevo_muestra_password_una_vez_y_audita_sin_password(self):
        respuesta = self.client.post(self.url, {
            "accion": "usuario_nuevo",
            "username": "karina2",
            "nombre": "Karina Dos Fuentes",
        }, follow=True)
        self.assertContains(respuesta, "Usuario karina2 creado. Contraseña:")
        password = RE_PASSWORD.search(respuesta.content.decode()).group(1)

        usuario = PerfilUsuario.objects.get(usuario__username="karina2")
        self.assertEqual(usuario.rol, PerfilUsuario.ROL_PORTAL)
        self.assertEqual(usuario.cliente, self.colima)
        self.assertEqual(usuario.usuario.first_name, "Karina")
        self.assertEqual(usuario.usuario.last_name, "Dos Fuentes")
        self.assertEqual(usuario.usuario.email, "karina2@torre380e.mx")
        self.assertTrue(usuario.usuario.check_password(password))

        evento = EventoAuditoria.objects.get(
            entidad="usuario_portal", entidad_id="karina2", accion="alta"
        )
        self.assertEqual(evento.cliente, self.colima)
        self.assertNotIn(password, json.dumps(evento.delta) + evento.motivo)

    def test_usuario_nuevo_duplicado_es_error_claro(self):
        respuesta = self.client.post(self.url, {
            "accion": "usuario_nuevo", "username": "karina",
        }, follow=True)
        self.assertContains(respuesta, "ya existe")
        self.assertEqual(
            PerfilUsuario.objects.filter(usuario__username="karina").count(), 1
        )

    def test_usuario_reset_regenera_password_del_cliente(self):
        respuesta = self.client.post(self.url, {
            "accion": "usuario_reset", "usuario_id": self.usuario_portal.pk,
        }, follow=True)
        self.assertContains(respuesta, "Contraseña de karina regenerada")
        password = RE_PASSWORD.search(respuesta.content.decode()).group(1)
        self.usuario_portal.refresh_from_db()
        self.assertFalse(self.usuario_portal.check_password("x12345678"))
        self.assertTrue(self.usuario_portal.check_password(password))
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="usuario_portal", entidad_id="karina", accion="reset_password"
            ).exists()
        )

    def test_usuario_reset_de_usuario_ajeno_es_error(self):
        nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="mezcal-nocturno")
        ajeno = crear_usuario("aurelio", "portal", cliente=nocturno)
        respuesta = self.client.post(self.url, {
            "accion": "usuario_reset", "usuario_id": ajeno.pk,
        }, follow=True)
        self.assertContains(respuesta, "no es del portal de este cliente")
        ajeno.refresh_from_db()
        self.assertTrue(ajeno.check_password("x12345678"))  # no se tocó
        self.assertFalse(
            EventoAuditoria.objects.filter(accion="reset_password").exists()
        )

    def test_usuario_reset_de_rol_mesa_es_error(self):
        respuesta = self.client.post(self.url, {
            "accion": "usuario_reset", "usuario_id": self.usuario_mesa.pk,
        }, follow=True)
        self.assertContains(respuesta, "no es del portal de este cliente")

    def test_usuario_toggle_desactiva_y_reactiva_con_auditoria(self):
        datos = {"accion": "usuario_toggle", "usuario_id": self.usuario_portal.pk}
        self.client.post(self.url, datos)
        self.usuario_portal.refresh_from_db()
        self.assertFalse(self.usuario_portal.is_active)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="usuario_portal", entidad_id="karina", accion="desactivado"
            ).exists()
        )
        self.client.post(self.url, datos)
        self.usuario_portal.refresh_from_db()
        self.assertTrue(self.usuario_portal.is_active)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="usuario_portal", entidad_id="karina", accion="activado"
            ).exists()
        )


def datos_form_sku(**extra):
    datos = {
        "accion": "sku_guardar",
        "codigo": "COLIMITA-SIX",
        "descripcion": "Colimita six pack",
        "peso_gr": "3600",
        "empaques_divisibles": "1",
        "requiere_lote": "on",
        "activo": "on",
    }
    datos.update(extra)
    return datos


class SkusTests(BaseGestionClientes):
    def setUp(self):
        super().setUp()
        self.url = reverse("mesa:cliente_skus", args=[self.colima.pk])

    def test_alta_de_sku_con_evento(self):
        respuesta = self.client.post(self.url, datos_form_sku())
        self.assertRedirects(respuesta, self.url)
        from apps.catalogo.models import SKU

        sku = SKU.objects.get(cliente=self.colima, codigo="COLIMITA-SIX")
        self.assertEqual(sku.peso_gr, 3600)
        self.assertTrue(sku.requiere_lote)
        self.assertTrue(sku.activo)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="sku", entidad_id="COLIMITA-SIX", accion="alta", cliente=self.colima
            ).exists()
        )

    def test_edicion_prefillea_y_guarda(self):
        from apps.catalogo.models import SKU

        sku = SKU.objects.create(
            cliente=self.colima, codigo="PARAMO-SIX", descripcion="Páramo six", peso_gr=3600
        )
        prefill = self.client.get(self.url, {"editar": sku.pk})
        self.assertEqual(prefill.status_code, 200)
        self.assertContains(prefill, 'value="PARAMO-SIX"')

        self.client.post(self.url, datos_form_sku(
            sku_id=str(sku.pk), codigo="PARAMO-SIX",
            descripcion="Páramo six pack", peso_gr="4000",
        ))
        sku.refresh_from_db()
        self.assertEqual(sku.descripcion, "Páramo six pack")
        self.assertEqual(sku.peso_gr, 4000)
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="sku", entidad_id="PARAMO-SIX", accion="edicion"
            ).exists()
        )

    def test_editar_sku_ajeno_es_404(self):
        from apps.catalogo.models import SKU

        nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="mezcal-nocturno")
        ajeno = SKU.objects.create(cliente=nocturno, codigo="MEZCAL-750", descripcion="Botella", peso_gr=1300)
        respuesta = self.client.get(self.url, {"editar": ajeno.pk})
        self.assertEqual(respuesta.status_code, 404)

    def test_codigo_duplicado_es_error_claro(self):
        from apps.catalogo.models import SKU

        SKU.objects.create(cliente=self.colima, codigo="COLIMITA-SIX", descripcion="Ya existe", peso_gr=1)
        respuesta = self.client.post(self.url, datos_form_sku(), follow=True)
        self.assertContains(respuesta, "Ya existe el SKU")
        self.assertEqual(SKU.objects.filter(cliente=self.colima, codigo="COLIMITA-SIX").count(), 1)

    def test_lista_muestra_disponible(self):
        from apps.catalogo.models import SKU

        SKU.objects.create(cliente=self.colima, codigo="TICUS-SIX", descripcion="Ticús six", peso_gr=3600)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Disponible")
        self.assertContains(respuesta, "TICUS-SIX")

    def test_variante_se_guarda_y_prefillea(self):
        from apps.catalogo.models import SKU

        self.client.post(self.url, datos_form_sku(variante="500 g"))
        sku = SKU.objects.get(cliente=self.colima, codigo="COLIMITA-SIX")
        self.assertEqual(sku.variante, "500 g")
        prefill = self.client.get(self.url, {"editar": sku.pk})
        self.assertContains(prefill, 'value="500 g"')

    def test_alta_sin_categoria_cae_en_otros(self):
        from apps.catalogo.models import SKU, Categoria

        self.client.post(self.url, datos_form_sku())
        sku = SKU.objects.get(cliente=self.colima, codigo="COLIMITA-SIX")
        self.assertEqual(sku.categoria.nombre, Categoria.OTROS)

    def test_categoria_nueva_y_alta_con_ella(self):
        from apps.catalogo.models import SKU, Categoria

        self.client.post(self.url, {"accion": "categoria_nueva", "nombre": "Cervezas"})
        cerveza = Categoria.objects.get(cliente=self.colima, nombre="Cervezas")
        self.client.post(self.url, datos_form_sku(categoria=str(cerveza.pk)))
        sku = SKU.objects.get(cliente=self.colima, codigo="COLIMITA-SIX")
        self.assertEqual(sku.categoria, cerveza)

    def test_categoria_duplicada_rechazada(self):
        from apps.catalogo.models import Categoria

        Categoria.objects.create(cliente=self.colima, nombre="Cervezas")
        respuesta = self.client.post(
            self.url, {"accion": "categoria_nueva", "nombre": "cervezas"}, follow=True,
        )
        self.assertContains(respuesta, "Ya existe la categoría")
        self.assertEqual(
            Categoria.objects.filter(cliente=self.colima, nombre__iexact="cervezas").count(), 1,
        )

    def test_categorias_bulk_recategoriza_con_evento(self):
        from apps.catalogo.models import SKU, Categoria

        cerveza = Categoria.objects.create(cliente=self.colima, nombre="Cervezas")
        sku = SKU.objects.create(cliente=self.colima, codigo="TICUS-SIX", descripcion="Ticús", peso_gr=1)
        self.client.post(self.url, {"accion": "categorias_bulk", f"cat_{sku.pk}": str(cerveza.pk)})
        sku.refresh_from_db()
        self.assertEqual(sku.categoria, cerveza)
        evento = EventoAuditoria.objects.get(
            entidad="sku", entidad_id="categorias_bulk", accion="categorias_actualizadas",
        )
        self.assertEqual(evento.delta["cambios"][0]["ahora"], "Cervezas")

    def test_categorias_bulk_ignora_categoria_ajena(self):
        from apps.catalogo.models import SKU, Categoria

        nocturno = Cliente.objects.create(nombre="Mezcal Nocturno", slug="mezcal-nocturno")
        ajena = Categoria.objects.create(cliente=nocturno, nombre="Mezcales")
        sku = SKU.objects.create(cliente=self.colima, codigo="TICUS-SIX", descripcion="Ticús", peso_gr=1)
        self.client.post(self.url, {"accion": "categorias_bulk", f"cat_{sku.pk}": str(ajena.pk)})
        sku.refresh_from_db()
        self.assertIsNone(sku.categoria)


ENCABEZADOS_CSV = (
    "codigo,descripcion,codigo_barras,peso_gr,largo_cm,ancho_cm,alto_cm,"
    "precio_declarado,punto_reorden,requiere_lote,empaques_divisibles"
)


class ImportCsvTests(BaseGestionClientes):
    def setUp(self):
        super().setUp()
        self.url = reverse("mesa:cliente_skus", args=[self.colima.pk])

    def _importar(self, contenido):
        archivo = SimpleUploadedFile(
            "catalogo.csv", contenido.encode("utf-8-sig"), content_type="text/csv"
        )
        return self.client.post(
            self.url, {"accion": "importar_csv", "archivo": archivo}, follow=True
        )

    def test_import_con_columna_variante(self):
        from apps.catalogo.models import SKU

        contenido = (
            "codigo,descripcion,variante,peso_gr\n"
            "TE-NEGRO-500,Té negro,500 g,520\n"
        )
        respuesta = self._importar(contenido)
        self.assertContains(respuesta, "1 SKUs creados")
        sku = SKU.objects.get(cliente=self.colima, codigo="TE-NEGRO-500")
        self.assertEqual(sku.variante, "500 g")

    def test_import_feliz_crea_y_actualiza(self):
        from apps.catalogo.models import SKU, Categoria

        SKU.objects.create(cliente=self.colima, codigo="YA-EXISTE", descripcion="Vieja", peso_gr=1)
        contenido = "\n".join([
            ENCABEZADOS_CSV,
            "YA-EXISTE,Descripción nueva,,500,,,,,10,no,",
            "NUEVO-1,Producto nuevo,750100000001,1200,10,10,20,250.50,5,si,2",
        ])
        respuesta = self._importar(contenido)
        self.assertContains(respuesta, "1 SKUs creados, 1 actualizados")

        viejo = SKU.objects.get(cliente=self.colima, codigo="YA-EXISTE")
        self.assertEqual(viejo.descripcion, "Descripción nueva")
        self.assertEqual(viejo.peso_gr, 500)
        self.assertEqual(viejo.punto_reorden, 10)
        self.assertFalse(viejo.requiere_lote)

        nuevo = SKU.objects.get(cliente=self.colima, codigo="NUEVO-1")
        self.assertEqual(nuevo.codigo_barras, "750100000001")
        self.assertEqual(nuevo.precio_declarado, Decimal("250.50"))
        self.assertEqual(nuevo.empaques_divisibles, 2)
        self.assertTrue(nuevo.requiere_lote)
        # El creado sin columna categoria cae en Otros.
        self.assertEqual(nuevo.categoria.nombre, Categoria.OTROS)

        evento = EventoAuditoria.objects.get(entidad="sku", entidad_id="import_csv", accion="import_csv")
        self.assertEqual(evento.delta["creados"], 1)
        self.assertEqual(evento.delta["actualizados"], 1)
        self.assertEqual(evento.delta["errores"], [])

    def test_import_con_categoria_la_crea_y_reusa(self):
        from apps.catalogo.models import SKU, Categoria

        contenido = "\n".join([
            "codigo,descripcion,categoria",
            "TE-1,Té verde,Tés",
            "TE-2,Té negro,tés",
            "TAZA-1,Taza,",
        ])
        respuesta = self._importar(contenido)
        self.assertContains(respuesta, "3 SKUs creados")
        tes = Categoria.objects.get(cliente=self.colima, nombre="Tés")
        self.assertEqual(SKU.objects.get(codigo="TE-1").categoria, tes)
        self.assertEqual(SKU.objects.get(codigo="TE-2").categoria, tes)  # iexact: no duplica
        self.assertEqual(SKU.objects.get(codigo="TAZA-1").categoria.nombre, Categoria.OTROS)

    def test_fila_con_error_no_detiene_a_las_demas(self):
        from apps.catalogo.models import SKU

        contenido = "\n".join([
            ENCABEZADOS_CSV,
            "BUENO-1,Producto bueno,,800,,,,,,si,",
            "MALO-1,Producto malo,,abc,,,,,,,",
        ])
        respuesta = self._importar(contenido)
        self.assertContains(respuesta, "1 SKUs creados, 0 actualizados")
        self.assertContains(respuesta, "1 fila con error (fila 3: peso_gr no es número)")
        self.assertTrue(SKU.objects.filter(cliente=self.colima, codigo="BUENO-1").exists())
        self.assertFalse(SKU.objects.filter(cliente=self.colima, codigo="MALO-1").exists())
        evento = EventoAuditoria.objects.get(entidad="sku", entidad_id="import_csv", accion="import_csv")
        self.assertEqual(evento.delta["errores"], ["fila 3: peso_gr no es número"])

    def test_csv_sin_encabezados_obligatorios_es_error(self):
        respuesta = self._importar("nombre,precio\nCosa,100")
        self.assertContains(respuesta, "codigo y descripcion")

    def test_precio_nan_e_infinity_son_filas_con_error_no_500(self):
        from apps.catalogo.models import SKU

        contenido = "\n".join([
            ENCABEZADOS_CSV,
            "BUENO-2,Producto bueno,,,,,,150.00,,,",
            "NAN-1,Precio nan,,,,,,NaN,,,",
            "INF-1,Precio infinito,,,,,,Infinity,,,",
        ])
        respuesta = self._importar(contenido)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "1 SKUs creados, 0 actualizados")
        self.assertContains(respuesta, "fila 3: precio_declarado no es número")
        self.assertContains(respuesta, "fila 4: precio_declarado no es número")
        self.assertTrue(SKU.objects.filter(cliente=self.colima, codigo="BUENO-2").exists())
        self.assertFalse(
            SKU.objects.filter(cliente=self.colima, codigo__in=["NAN-1", "INF-1"]).exists()
        )
        evento = EventoAuditoria.objects.get(
            entidad="sku", entidad_id="import_csv", accion="import_csv"
        )
        self.assertEqual(len(evento.delta["errores"]), 2)

    def test_csv_mas_grande_del_limite_se_rechaza(self):
        from apps.catalogo.models import SKU

        contenido = ENCABEZADOS_CSV + "\n" + "X" * (2 * 1024 * 1024 + 100)
        respuesta = self._importar(contenido)
        self.assertContains(respuesta, "El CSV no debe pasar de 2 MB.")
        self.assertEqual(SKU.objects.filter(cliente=self.colima).count(), 0)
        self.assertFalse(EventoAuditoria.objects.filter(accion="import_csv").exists())

    def test_tope_de_filas_aborta_el_import_completo(self):
        from apps.catalogo.models import SKU

        torre_chico = {**settings.TORRE, "IMPORT_CSV_MAX_FILAS": 2}
        contenido = "\n".join([
            ENCABEZADOS_CSV,
            "UNO,Producto uno,,,,,,,,,",
            "DOS,Producto dos,,,,,,,,,",
            "TRES,Producto tres,,,,,,,,,",
        ])
        with override_settings(TORRE=torre_chico):
            respuesta = self._importar(contenido)
        self.assertContains(respuesta, "más de 2 filas")
        # El atomic revierte todo: nada a medias y sin evento fantasma.
        self.assertEqual(SKU.objects.filter(cliente=self.colima).count(), 0)
        self.assertFalse(EventoAuditoria.objects.filter(accion="import_csv").exists())

    def test_encabezados_con_espacios_importan_bien(self):
        from apps.catalogo.models import SKU

        respuesta = self._importar("codigo, descripcion\nESP-1, Con espacios\n")
        self.assertContains(respuesta, "1 SKUs creados, 0 actualizados")
        sku = SKU.objects.get(cliente=self.colima, codigo="ESP-1")
        self.assertEqual(sku.descripcion, "Con espacios")


class TiendasTests(BaseGestionClientes):
    def setUp(self):
        super().setUp()
        self.url = reverse("mesa:cliente_detalle", args=[self.colima.pk])

    def test_alta_de_tienda_muestra_url_del_webhook(self):
        respuesta = self.client.post(self.url, {
            "accion": "tienda_guardar",
            "dominio": "Caney.myshopify.com",
            "location_id": "loc-caney",
            "activo": "on",
        }, follow=True)
        from apps.integraciones.models import Tienda

        tienda = Tienda.objects.get(dominio="caney.myshopify.com")  # normalizado
        self.assertEqual(tienda.cliente, self.colima)
        self.assertTrue(tienda.activo)
        self.assertContains(respuesta, f"/hooks/shopify/{tienda.pk}/")
        self.assertTrue(
            EventoAuditoria.objects.filter(
                entidad="tienda", entidad_id="caney.myshopify.com", accion="alta"
            ).exists()
        )
        # La ficha del cliente muestra la URL del webhook para pegar en Shopify.
        detalle = self.client.get(self.url)
        self.assertContains(detalle, f"/hooks/shopify/{tienda.pk}/")

    def test_dominio_duplicado_es_error(self):
        from apps.integraciones.models import Tienda

        Tienda.objects.create(cliente=self.colima, dominio="caney.myshopify.com")
        respuesta = self.client.post(self.url, {
            "accion": "tienda_guardar", "dominio": "caney.myshopify.com", "activo": "on",
        }, follow=True)
        self.assertContains(respuesta, "Ya hay una tienda conectada")
        self.assertEqual(Tienda.objects.count(), 1)

    def test_edicion_no_registra_el_token_en_la_auditoria(self):
        from apps.integraciones.models import Tienda

        tienda = Tienda.objects.create(cliente=self.colima, dominio="caney.myshopify.com")
        self.client.post(self.url, {
            "accion": "tienda_guardar",
            "tienda_id": tienda.pk,
            "dominio": "caney.myshopify.com",
            "location_id": "loc-nueva",
            "token": "shhh-super-secreto",
            "activo": "on",
        })
        tienda.refresh_from_db()
        self.assertEqual(tienda.location_id, "loc-nueva")
        self.assertEqual(tienda.token, "shhh-super-secreto")
        evento = EventoAuditoria.objects.get(entidad="tienda", accion="edicion")
        self.assertIn("location_id", evento.delta)
        self.assertNotIn("shhh-super-secreto", json.dumps(evento.delta))

    def test_get_edicion_no_expone_el_token_completo(self):
        from apps.integraciones.models import Tienda

        tienda = Tienda.objects.create(
            cliente=self.colima, dominio="caney.myshopify.com",
            token="shpat-super-secreto-9876",
        )
        respuesta = self.client.get(f"{self.url}?tienda={tienda.pk}")
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "shpat-super-secreto-9876")
        self.assertContains(respuesta, 'type="password"')
        self.assertContains(respuesta, "…9876")  # referencia: solo los últimos 4
        self.assertContains(respuesta, "vacío = conservar el actual")

    def test_editar_con_token_vacio_conserva_el_actual(self):
        from apps.integraciones.models import Tienda

        tienda = Tienda.objects.create(
            cliente=self.colima, dominio="caney.myshopify.com", token="token-viejo",
        )
        self.client.post(self.url, {
            "accion": "tienda_guardar",
            "tienda_id": tienda.pk,
            "dominio": "caney.myshopify.com",
            "token": "",
            "activo": "on",
        })
        tienda.refresh_from_db()
        self.assertEqual(tienda.token, "token-viejo")
        evento = EventoAuditoria.objects.get(entidad="tienda", accion="edicion")
        self.assertNotIn("token", evento.delta)

    def test_editar_con_token_nuevo_lo_reemplaza(self):
        from apps.integraciones.models import Tienda

        tienda = Tienda.objects.create(
            cliente=self.colima, dominio="caney.myshopify.com", token="token-viejo",
        )
        self.client.post(self.url, {
            "accion": "tienda_guardar",
            "tienda_id": tienda.pk,
            "dominio": "caney.myshopify.com",
            "token": "token-nuevo",
            "activo": "on",
        })
        tienda.refresh_from_db()
        self.assertEqual(tienda.token, "token-nuevo")


class AccesoVistasNuevasTests(BaseGestionClientes):
    def _urls_nuevas(self):
        return [
            reverse("mesa:cliente_nuevo"),
            reverse("mesa:cliente_editar", args=[self.colima.pk]),
            reverse("mesa:cliente_tarifario", args=[self.colima.pk]),
            reverse("mesa:cliente_skus", args=[self.colima.pk]),
        ]

    def test_portal_y_piso_reciben_403(self):
        for usuario in (self.usuario_portal, self.usuario_piso):
            self.client.force_login(usuario)
            for url in self._urls_nuevas():
                respuesta = self.client.get(url)
                self.assertEqual(respuesta.status_code, 403, f"{url} dejó pasar a {usuario.username}")

    def test_portal_no_gestiona_usuarios_del_detalle(self):
        self.client.force_login(self.usuario_portal)
        respuesta = self.client.post(
            reverse("mesa:cliente_detalle", args=[self.colima.pk]),
            {"accion": "usuario_nuevo", "username": "intruso"},
        )
        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(
            PerfilUsuario.objects.filter(usuario__username="intruso").exists()
        )

    def test_anonimo_va_al_login(self):
        self.client.logout()
        for url in self._urls_nuevas():
            respuesta = self.client.get(url)
            self.assertEqual(respuesta.status_code, 302, f"{url} no redirigió al login")

    def test_mesa_carga_todas_las_vistas_nuevas(self):
        for url in self._urls_nuevas():
            respuesta = self.client.get(url)
            self.assertEqual(respuesta.status_code, 200, f"{url} no cargó para mesa")
