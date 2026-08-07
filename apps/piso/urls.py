from django.urls import path

from . import views

app_name = "piso"

urlpatterns = [
    path("", views.home, name="home"),
    path("recepciones/", views.recepciones, name="recepciones"),
    path("recepciones/<int:pk>/", views.recepcion_detalle, name="recepcion_detalle"),
    path("picking/", views.picking, name="picking"),
    path("picking/<int:pk>/", views.picking_pedido, name="picking_pedido"),
    path("empaque/", views.empaque, name="empaque"),
    path("empaque/<int:pk>/", views.empaque_pedido, name="empaque_pedido"),
    path("salida/", views.salida, name="salida"),
    path("etiqueta/<int:guia_pk>/", views.etiqueta, name="etiqueta"),
    path("conteos/", views.conteos, name="conteos"),
    path("cuarentena/", views.cuarentena, name="cuarentena"),
    path("entrega-local/", views.entrega_local, name="entrega_local"),
    path("entrega-local/<int:pk>/", views.entrega_local_pedido, name="entrega_local_pedido"),
]
