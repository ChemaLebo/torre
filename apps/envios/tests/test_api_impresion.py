"""API del relay de impresión (/api/impresion/): token Bearer + JSON plano.

Sin token válido NADIE ve nada — ni la lista ni los PDFs (las etiquetas llevan
datos del comprador; MEDIA en dev no tiene auth, por eso el PDF se sirve por
la API). El agente reporta resultados; al agotar TORRE["IMPRESION_MAX_INTENTOS"]
el trabajo pasa a ERROR con evento de auditoría para que Mesa lo vea.
"""
import json
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.core.models import EventoAuditoria
from apps.envios.models import Guia, TrabajoImpresion

from .base import crear_cliente, crear_pedido, crear_tienda

TOKEN = "token-secreto-de-prueba"
PDF = b"%PDF-1.4 etiqueta de prueba"

_MEDIA_TEMPORAL = tempfile.mkdtemp(prefix="torre-api-impresion-")


@override_settings(MEDIA_ROOT=_MEDIA_TEMPORAL, TORRE_TOKEN_IMPRESION=TOKEN)
class ApiImpresionTestCase(TestCase):
    """Fixture común: cliente/tienda + helpers de trabajos y requests con token."""

    @classmethod
    def setUpTestData(cls):
        cls.cliente = crear_cliente()
        cls.tienda = crear_tienda(cls.cliente)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TEMPORAL, ignore_errors=True)

    def crear_trabajo(self, **kwargs):
        pedido = crear_pedido(self.cliente, self.tienda, estado="GUIA_GENERADA")
        guia = Guia.objects.create(
            pedido=pedido, carrier="paquetexpress", servicio="terrestre",
            numero=f"PQX-{pedido.pk:04d}",
        )
        defaults = {"guia": guia, "pdf": ContentFile(PDF, name="etiqueta.pdf")}
        defaults.update(kwargs)
        return TrabajoImpresion.objects.create(**defaults)

    def get(self, url, token=TOKEN):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return self.client.get(url, headers=headers)

    def get_pendientes(self, token=TOKEN):
        return self.get(reverse("envios:impresion_pendientes"), token=token)

    def post_resultado(self, trabajo, cuerpo, token=TOKEN, cliente_http=None):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return (cliente_http or self.client).post(
            reverse("envios:impresion_resultado", args=[trabajo.pk]),
            data=json.dumps(cuerpo), content_type="application/json", headers=headers,
        )


class AutenticacionTests(ApiImpresionTestCase):
    def test_sin_header_403(self):
        self.assertEqual(self.get_pendientes(token=None).status_code, 403)

    def test_token_equivocado_403(self):
        self.assertEqual(self.get_pendientes(token="otro-token").status_code, 403)

    def test_esquema_que_no_es_bearer_403(self):
        respuesta = self.client.get(
            reverse("envios:impresion_pendientes"),
            headers={"Authorization": f"Token {TOKEN}"},
        )
        self.assertEqual(respuesta.status_code, 403)

    @override_settings(TORRE_TOKEN_IMPRESION="")
    def test_sin_token_configurado_responde_403_siempre(self):
        # API cerrada: ni con header vacío ni con cualquier token entra nadie.
        self.assertEqual(self.get_pendientes(token="").status_code, 403)
        self.assertEqual(self.get_pendientes(token="lo-que-sea").status_code, 403)
        self.assertEqual(self.get_pendientes(token=None).status_code, 403)

    def test_resultado_sin_token_403_y_no_toca_el_trabajo(self):
        trabajo = self.crear_trabajo()
        respuesta = self.post_resultado(trabajo, {"ok": True}, token="malo")
        self.assertEqual(respuesta.status_code, 403)
        trabajo.refresh_from_db()
        self.assertEqual(trabajo.estado, TrabajoImpresion.PENDIENTE)

    def test_pdf_sin_token_403(self):
        trabajo = self.crear_trabajo()
        url = reverse("envios:impresion_pdf", args=[trabajo.pk])
        self.assertEqual(self.get(url, token=None).status_code, 403)
        self.assertEqual(self.get(url, token="malo").status_code, 403)


class PendientesTests(ApiImpresionTestCase):
    def test_regresa_los_pendientes_con_url_del_pdf(self):
        trabajo = self.crear_trabajo()
        respuesta = self.get_pendientes()
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        self.assertEqual(len(datos), 1)
        self.assertEqual(datos[0]["id"], trabajo.pk)
        self.assertEqual(datos[0]["folio"], trabajo.guia.pedido.folio)
        self.assertEqual(datos[0]["guia"], trabajo.guia.numero)
        self.assertEqual(
            datos[0]["url_pdf"],
            "http://testserver" + reverse("envios:impresion_pdf", args=[trabajo.pk]),
        )

    def test_excluye_impresos_errores_y_agotados(self):
        vivo = self.crear_trabajo(intentos=4)  # aún le queda un intento
        self.crear_trabajo(estado=TrabajoImpresion.IMPRESO)
        self.crear_trabajo(estado=TrabajoImpresion.ERROR)
        self.crear_trabajo(intentos=5)  # agotado pero sin transicionar aún
        datos = self.get_pendientes().json()
        self.assertEqual([t["id"] for t in datos], [vivo.pk])

    def test_maximo_10_mas_viejos_primero(self):
        trabajos = [self.crear_trabajo() for _ in range(12)]
        datos = self.get_pendientes().json()
        self.assertEqual(len(datos), 10)
        self.assertEqual([t["id"] for t in datos], [t.pk for t in trabajos[:10]])


class ResultadoTests(ApiImpresionTestCase):
    def test_ok_marca_impreso_con_timestamp_y_evento(self):
        trabajo = self.crear_trabajo()
        respuesta = self.post_resultado(trabajo, {"ok": True})
        self.assertEqual(respuesta.status_code, 200)
        trabajo.refresh_from_db()
        self.assertEqual(trabajo.estado, TrabajoImpresion.IMPRESO)
        self.assertIsNotNone(trabajo.ts_impreso)
        evento = EventoAuditoria.objects.filter(
            entidad="trabajo_impresion", entidad_id=str(trabajo.pk),
            accion="impresion_impreso",
        ).latest("id")
        self.assertEqual(evento.delta["folio"], trabajo.guia.pedido.folio)

    def test_error_suma_intento_y_guarda_el_detalle(self):
        trabajo = self.crear_trabajo()
        self.post_resultado(trabajo, {"ok": False, "error": "la térmica no tiene papel"})
        trabajo.refresh_from_db()
        self.assertEqual(trabajo.estado, TrabajoImpresion.PENDIENTE)  # aún reintentable
        self.assertEqual(trabajo.intentos, 1)
        self.assertEqual(trabajo.error, "la térmica no tiene papel")
        self.assertIsNone(trabajo.ts_impreso)

    def test_al_quinto_error_pasa_a_error_con_evento_para_mesa(self):
        trabajo = self.crear_trabajo()
        for numero in range(1, 6):
            self.post_resultado(trabajo, {"ok": False, "error": f"falla {numero}"})
        trabajo.refresh_from_db()
        self.assertEqual(trabajo.estado, TrabajoImpresion.ERROR)
        self.assertEqual(trabajo.intentos, 5)
        self.assertEqual(trabajo.error, "falla 5")
        evento = EventoAuditoria.objects.filter(
            entidad="trabajo_impresion", entidad_id=str(trabajo.pk),
            accion="impresion_error",
        ).latest("id")
        self.assertEqual(evento.cliente, self.cliente)
        self.assertEqual(evento.delta["intentos"], 5)
        # Agotado ya no aparece en pendientes: el agente deja de martillarlo.
        self.assertEqual(self.get_pendientes().json(), [])

    def test_el_error_se_recorta_a_300_caracteres(self):
        trabajo = self.crear_trabajo()
        self.post_resultado(trabajo, {"ok": False, "error": "x" * 500})
        trabajo.refresh_from_db()
        self.assertEqual(len(trabajo.error), 300)

    def test_trabajo_ya_cerrado_es_noop_idempotente(self):
        trabajo = self.crear_trabajo(estado=TrabajoImpresion.IMPRESO)
        respuesta = self.post_resultado(trabajo, {"ok": False, "error": "reintento tardío"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["estado"], TrabajoImpresion.IMPRESO)
        trabajo.refresh_from_db()
        self.assertEqual(trabajo.intentos, 0)
        self.assertEqual(trabajo.error, "")

    def test_json_invalido_400(self):
        trabajo = self.crear_trabajo()
        respuesta = self.client.post(
            reverse("envios:impresion_resultado", args=[trabajo.pk]),
            data="esto no es json", content_type="application/json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(respuesta.status_code, 400)

    def test_sin_campo_ok_400(self):
        trabajo = self.crear_trabajo()
        self.assertEqual(self.post_resultado(trabajo, {"listo": True}).status_code, 400)

    def test_trabajo_inexistente_404(self):
        respuesta = self.client.post(
            reverse("envios:impresion_resultado", args=[99999]),
            data=json.dumps({"ok": True}), content_type="application/json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(respuesta.status_code, 404)

    def test_get_no_permitido_405(self):
        trabajo = self.crear_trabajo()
        respuesta = self.get(reverse("envios:impresion_resultado", args=[trabajo.pk]))
        self.assertEqual(respuesta.status_code, 405)

    def test_csrf_no_bloquea_al_agente(self):
        # El agente no es un navegador: el POST va csrf_exempt (el token manda).
        trabajo = self.crear_trabajo()
        estricto = Client(enforce_csrf_checks=True)
        respuesta = self.post_resultado(trabajo, {"ok": True}, cliente_http=estricto)
        self.assertEqual(respuesta.status_code, 200)
        trabajo.refresh_from_db()
        self.assertEqual(trabajo.estado, TrabajoImpresion.IMPRESO)


class PdfTests(ApiImpresionTestCase):
    def test_descarga_el_pdf_con_token(self):
        trabajo = self.crear_trabajo()
        respuesta = self.get(reverse("envios:impresion_pdf", args=[trabajo.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(respuesta.streaming_content), PDF)

    def test_trabajo_inexistente_404(self):
        respuesta = self.get(reverse("envios:impresion_pdf", args=[99999]))
        self.assertEqual(respuesta.status_code, 404)
