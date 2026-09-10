from django.urls import path

from . import views

app_name = "mesa"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("bodega/", views.bodega, name="bodega"),
    path("incidencias/", views.incidencias, name="incidencias"),
    path("incidencias/<int:pk>/", views.incidencia_detalle, name="incidencia_detalle"),
    path("pedidos/", views.pedidos, name="pedidos"),
    path("pedidos/nuevo/", views.pedido_nuevo, name="pedido_nuevo"),
    path("inventario/", views.inventario, name="inventario"),
    path("inventario/reconciliar/", views.inventario_reconciliar, name="inventario_reconciliar"),
    path("inventario/conteo.csv", views.inventario_exportar_conteo, name="inventario_exportar_conteo"),
    path("recepciones/", views.recepciones, name="recepciones"),
    path("recepciones/plantilla.csv", views.recepciones_plantilla, name="recepciones_plantilla"),
    path("sync/", views.sync, name="sync"),
    path("clientes/", views.clientes, name="clientes"),
    path("clientes/nuevo/", views.cliente_nuevo, name="cliente_nuevo"),
    path("clientes/<int:pk>/", views.cliente_detalle, name="cliente_detalle"),
    path("clientes/<int:pk>/editar/", views.cliente_editar, name="cliente_editar"),
    path("clientes/<int:pk>/tarifario/", views.cliente_tarifario, name="cliente_tarifario"),
    path("clientes/<int:pk>/skus/", views.cliente_skus, name="cliente_skus"),
    path("clientes/<int:pk>/cajas/", views.cliente_cajas, name="cliente_cajas"),
    path("clientes/<int:pk>/lotes/", views.cliente_lotes, name="cliente_lotes"),
    path("clientes/<int:pk>/skus/exportar/", views.cliente_skus_exportar, name="cliente_skus_exportar"),
    path("finanzas/", views.finanzas, name="finanzas"),
    path("manuales/", views.manuales, name="manuales"),
    path("manuales/<slug:slug>/", views.manual_detalle, name="manual_detalle"),
]
