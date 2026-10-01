"""Fixtures compartidos de las pruebas de finanzas (motor y vista de Mesa):
usuario con rol, pedido, caja, guía y entrada (ASN)."""
from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.core.models import PerfilUsuario
from apps.envios.models import Guia, Paquete


def crear_usuario(username, rol, cliente=None):
    user = get_user_model().objects.create_user(username=username, password="x12345678")
    PerfilUsuario.objects.create(usuario=user, rol=rol, cliente=cliente)
    return user


def crear_pedido(cliente, folio, **extra):
    from apps.pedidos.models import Pedido

    defaults = {
        "cliente": cliente,
        "origen": "manual",
        "folio": folio,
        "comprador_nombre": "Comprador de Prueba",
        "direccion": {},
        "cp": "01780",
        "es_local": True,
        "valor_declarado": Decimal("500.00"),
        "estado": Pedido.PENDIENTE,
        "corte_vigente_al_ingreso": time(14, 0),
    }
    defaults.update(extra)
    return Pedido.objects.create(**defaults)


def paquete(pedido, numero, peso, carrier="local", precio="100"):
    return Paquete.objects.create(
        pedido=pedido, numero=numero, peso_kg=Decimal(str(peso)),
        carrier=carrier, precio_cotizado=Decimal(precio),
    )


def guia(pedido, carrier, costo, paquete=None):
    return Guia.objects.create(
        pedido=pedido, paquete=paquete, carrier=carrier,
        numero=f"G-{pedido.folio}-{carrier}", costo_preferencial=Decimal(str(costo)),
    )


def asn(cliente, estado="CERRADA", tarimas=0, tarimas_recibidas=0, descarga=None):
    from apps.inventario.models import OrdenEntrada

    return OrdenEntrada.objects.create(
        cliente=cliente, estado=estado, tarimas=tarimas,
        tarimas_recibidas=tarimas_recibidas, ts_descarga_fin=descarga,
    )
