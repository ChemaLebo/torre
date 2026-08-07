"""Fixtures mínimos para los tests de envios.

Construyen Cliente/Tienda/Pedido con los campos del contrato (CONVENTIONS.md).
Los imports de otras apps son lazy: se resuelven al correr, en integración.
"""
import itertools
from datetime import time
from decimal import Decimal

from apps.core.models import Cliente

_consecutivo = itertools.count(1)


def crear_cliente(**kwargs):
    n = next(_consecutivo)
    defaults = {
        "nombre": f"Cervecería Colima {n}",
        "slug": f"colima-{n}",
        "carrier_preferente": "paquetexpress",
    }
    defaults.update(kwargs)
    return Cliente.objects.create(**defaults)


def crear_tienda(cliente, **kwargs):
    from apps.integraciones.models import Tienda

    n = next(_consecutivo)
    defaults = {
        "cliente": cliente,
        "plataforma": "shopify",
        "dominio": f"tienda-{n}.myshopify.com",
        "token": "",
        "location_id": "",
        "activo": True,
    }
    defaults.update(kwargs)
    return Tienda.objects.create(**defaults)


def crear_pedido(cliente, tienda, **kwargs):
    from apps.pedidos.models import Pedido

    n = next(_consecutivo)
    defaults = {
        "cliente": cliente,
        "tienda": tienda,
        "shopify_order_id": f"9000{n}",
        "folio": f"PED-{n:05d}",
        "origen": "webhook",
        "comprador_nombre": "Ana Compradora",
        "comprador_tel": "3120000000",
        "comprador_email": "ana@example.com",
        "direccion": {
            "address1": "Calle Reforma 123",
            "city": "Guadalajara",
            "province_code": "JAL",
            "zip": "44100",
        },
        "cp": "44100",
        "es_local": False,
        "valor_declarado": Decimal("500.00"),
        "estado": "EMPACADO",
        "corte_vigente_al_ingreso": time(14, 0),
        "peso_esperado_gr": 1200,
    }
    defaults.update(kwargs)
    return Pedido.objects.create(**defaults)
