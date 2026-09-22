from django.urls import path

from . import views

app_name = "piso"

urlpatterns = [
    path("", views.home, name="home"),
    path("recepciones/", views.recepciones, name="recepciones"),
    path("recepciones/<int:pk>/", views.recepcion_detalle, name="recepcion_detalle"),
    path("recepciones/<int:pk>/contar/", views.recepcion_contar, name="recepcion_contar"),
    path("recepciones/<int:pk>/ubicar/", views.recepcion_ubicar, name="recepcion_ubicar"),
    path("picking/", views.picking, name="picking"),
    path("picking/<int:pk>/", views.picking_pedido, name="picking_pedido"),
    path("empaque/", views.empaque, name="empaque"),
    path("empaque/<int:pk>/", views.empaque_pedido, name="empaque_pedido"),
    path("salida/", views.salida, name="salida"),
    path("salida/registrar/", views.salida_registrar, name="salida_registrar"),
    path("salida/resumen/", views.salida_resumen, name="salida_resumen"),
    path("salida/manifiestos/<int:pk>/", views.manifiesto, name="manifiesto"),
    path("etiqueta/<int:guia_pk>/", views.etiqueta, name="etiqueta"),
    path("conteos/", views.conteos, name="conteos"),
    path("cuarentena/", views.cuarentena, name="cuarentena"),
    path("entrega-local/", views.entrega_local, name="entrega_local"),
    path("entrega-local/<int:pk>/", views.entrega_local_pedido, name="entrega_local_pedido"),
]
