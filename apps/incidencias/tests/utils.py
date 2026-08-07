"""Helpers de fixtures para los tests de incidencias.

`crear_pedido` importa pedidos/integraciones (apps hermanas): se usa solo en
los tests que necesitan un pedido real; los demás abren incidencias sin pedido.
"""
from datetime import time
from decimal import Decimal

from apps.core.models import Cliente


def crear_cliente(slug="colima", nombre="Cervecería Colima"):
    return Cliente.objects.create(nombre=nombre, slug=slug)


def crear_pedido(cliente, folio="PED-00001", shopify_order_id="1001"):
    from apps.integraciones.models import Tienda
    from apps.pedidos.models import Pedido

    tienda, _ = Tienda.objects.get_or_create(
        cliente=cliente,
        dominio=f"{cliente.slug}.myshopify.com",
        defaults={"plataforma": "shopify"},
    )
    return Pedido.objects.create(
        tienda=tienda,
        cliente=cliente,
        shopify_order_id=shopify_order_id,
        folio=folio,
        origen="webhook",
        comprador_nombre="Comprador de Prueba",
        direccion={},
        cp="28017",
        es_local=True,
        valor_declarado=Decimal("500.00"),
        estado="PENDIENTE",
        corte_vigente_al_ingreso=time(14, 0),
        peso_esperado_gr=1000,
    )
