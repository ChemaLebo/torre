"""Los servicios que abren con select_for_update DEBEN correr dentro de
transaction.atomic: Postgres truena fuera de una transacción y SQLite (los
tests) lo ignora en silencio. El 2026-09-21 un helper nuevo se coló entre el
decorador y `empacar_caja`, y el piso vio un 500 al confirmar cada caja. Esta
prueba vigila que el decorador siga donde debe."""
from django.test import SimpleTestCase

from apps.pedidos import services


class ServiciosAtomicosTests(SimpleTestCase):
    def test_los_servicios_con_select_for_update_llevan_transaction_atomic(self):
        for nombre in ("empacar_caja", "cerrar_caja", "corregir_peso_caja", "reemplazar_foto_pedido"):
            servicio = getattr(services, nombre)
            self.assertTrue(
                hasattr(servicio, "__wrapped__"),
                f"{nombre} debe llevar @transaction.atomic (select_for_update fuera de transacción truena en Postgres)",
            )

    def test_los_helpers_puros_no_abren_transaccion(self):
        self.assertFalse(hasattr(services._peso_esperado_caja, "__wrapped__"))
