"""Completar el catálogo de localidades por CP (Chema 2026-10-15): una consulta
a envia por CP que falte; dry-run por default; lo que no responde se reintenta."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.envios.models import LocalidadCP
from apps.envios.tests.base import crear_cliente, crear_pedido, crear_tienda

RESPUESTAS = {
    "45019": {"localidad": "Zapopan", "municipio": "Zapopan", "estado": "JA", "colonias": ["Centro"]},
    "99999": {"localidad": "", "municipio": "", "estado": "", "colonias": []},  # envia no lo conoce
    "06700": None,  # sin respuesta (red)
}
OTRA = {"localidad": "Alguna", "municipio": "Alguno", "estado": "XX", "colonias": []}
CONSULTA = "apps.envios.localidades._consultar_geocodes"
PAUSA = "apps.envios.management.commands.completar_localidades.time.sleep"


@override_settings(ENVIA_API_KEY="llave", ENVIA_MODO="cotizar")
class CompletarLocalidadesTests(TestCase):
    def setUp(self):
        cliente = crear_cliente()
        tienda = crear_tienda(cliente)
        LocalidadCP.objects.create(cp="03100", localidad="Ciudad de México", municipio="Benito Juárez", estado="CX")
        for cp in ("03100", "45019", "45019", "99999", "06700"):
            crear_pedido(cliente, tienda, cp=cp)
        crear_pedido(cliente, tienda, cp="", direccion={"zip": "1780"})  # 4 dígitos → 01780, sin catálogo
        crear_pedido(cliente, tienda, cp="", direccion={})  # sin CP: se ignora

    def test_dry_run_lista_sin_consultar(self):
        salida = StringIO()
        with patch(CONSULTA) as consulta:
            call_command("completar_localidades", stdout=salida)
        consulta.assert_not_called()
        texto = salida.getvalue()
        self.assertIn("5 CP distintos en los pedidos; 1 ya con catálogo; 4 por consultar.", texto)
        self.assertIn("01780, 06700, 45019, 99999", texto)
        self.assertIn("Dry-run", texto)
        self.assertEqual(LocalidadCP.objects.count(), 1)

    def test_aplicar_consulta_una_vez_por_cp_y_guarda(self):
        salida = StringIO()
        with patch(CONSULTA, side_effect=lambda cp: RESPUESTAS.get(cp, OTRA)) as consulta, patch(PAUSA):
            call_command("completar_localidades", "--aplicar", stdout=salida)
        self.assertEqual(sorted(c.args[0] for c in consulta.call_args_list), ["01780", "06700", "45019", "99999"])
        texto = salida.getvalue()
        self.assertIn("45019: Zapopan · Zapopan · JA", texto)
        self.assertIn("99999: envia no conoce este CP", texto)
        self.assertIn("06700: sin respuesta", texto)
        self.assertIn("2 CP con localidad; 1 desconocidos para envia; 1 sin respuesta.", texto)
        self.assertEqual(set(LocalidadCP.objects.values_list("cp", flat=True)), {"03100", "01780", "45019", "99999"})
        self.assertEqual(LocalidadCP.objects.get(cp="99999").localidad, "")
        # Segunda corrida: solo vuelve a preguntar por el que no respondió.
        salida = StringIO()
        with patch(CONSULTA, return_value=None) as consulta, patch(PAUSA):
            call_command("completar_localidades", "--aplicar", stdout=salida)
        self.assertEqual([c.args[0] for c in consulta.call_args_list], ["06700"])

    @override_settings(ENVIA_API_KEY="", ENVIA_MODO="off")
    def test_sin_llave_no_consulta(self):
        salida = StringIO()
        with patch(CONSULTA) as consulta:
            call_command("completar_localidades", "--aplicar", stdout=salida)
        consulta.assert_not_called()
        self.assertIn("Sin ENVIA_API_KEY", salida.getvalue())
        self.assertEqual(LocalidadCP.objects.count(), 1)
