"""URLs del portal del cliente (contrato CONVENTIONS.md §portal)."""
from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("pedidos/", views.pedidos, name="pedidos"),
    path("pedidos/<int:pk>/", views.pedido_detalle, name="pedido_detalle"),
    path("inventario/", views.inventario, name="inventario"),
    path("bodega/", views.bodega, name="bodega"),
    path("incidencias/", views.incidencias, name="incidencias"),
    path("incidencias/nueva/", views.incidencia_nueva, name="incidencia_nueva"),
    path("incidencias/<int:pk>/", views.incidencia_detalle, name="incidencia_detalle"),
    path("recepciones/", views.recepciones, name="recepciones"),
    path("recepciones/plantilla.csv", views.recepciones_plantilla, name="recepciones_plantilla"),
    path("manuales/", views.manuales, name="manuales"),
    path("manuales/<slug:slug>/", views.manual_detalle, name="manual_detalle"),
    path("exportar/", views.exportar, name="exportar"),
    path("reportes/dia/", views.reporte_dia, name="reporte_dia"),
    path("reportes/dia.csv", views.reporte_dia_csv, name="reporte_dia_csv"),
    path("reportes/", views.reportes, name="reportes"),
    path("reportes/<slug:clave>/", views.reporte, name="reporte"),
    path("reportes/<slug:clave>.csv", views.reporte_csv, name="reporte_csv"),
]
