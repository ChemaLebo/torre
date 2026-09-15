"""Backfill de Paquete.ts_cierre/foto_cierre (migración envios 0009) desde los
eventos caja_cerrada_con_evidencia: ts del evento y delta.evidencia_id."""
from decimal import Decimal

from django.apps import apps
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.core.models import EvidenciaFoto
from apps.core.services import registrar_evento
from apps.envios.models import Paquete

from .base import crear_cliente, crear_pedido, crear_tienda


@override_settings(MEDIA_ROOT="/tmp/torre-test-migracion-cierre")
class BackfillCierreTests(TestCase):
    def test_estampa_ts_y_foto_desde_los_eventos(self):
        from importlib import import_module
        desde_eventos = import_module("apps.envios.migrations.0009_paquete_cierre").desde_eventos

        cliente = crear_cliente()
        pedido = crear_pedido(cliente, crear_tienda(cliente))
        cerrada = Paquete.objects.create(pedido=pedido, numero=1, peso_kg=Decimal("2"), carrier="estafeta")
        abierta = Paquete.objects.create(pedido=pedido, numero=2, peso_kg=Decimal("2"), carrier="estafeta")
        foto = EvidenciaFoto.objects.create(
            entidad="pedido", entidad_id=str(pedido.pk), tipo="caja_cerrada",
            archivo=ContentFile(b"\x89PNG", name="c.png"), tomada_por="piso1",
        )
        evento = registrar_evento(
            "paquete", cerrada.pk, "caja_cerrada_con_evidencia", cliente=cliente,
            delta={"pedido": pedido.folio, "caja": 1, "evidencia_id": foto.pk},
        )
        registrar_evento("paquete", "no-numerico", "caja_cerrada_con_evidencia", delta={})

        desde_eventos(apps, None)
        cerrada.refresh_from_db()
        abierta.refresh_from_db()
        self.assertEqual(cerrada.ts_cierre, evento.ts)
        self.assertEqual(cerrada.foto_cierre, foto)
        self.assertIsNone(abierta.ts_cierre)
