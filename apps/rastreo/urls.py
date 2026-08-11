from django.urls import path

from . import views

app_name = "rastreo"

urlpatterns = [
    # /r/e/<token>/ — corta a propósito: es la que codifica el QR de la etiqueta.
    path("e/<str:token>/", views.etiqueta_repartidor, name="etiqueta_repartidor"),
    path("<str:token>/", views.pagina, name="pagina"),
    path("<str:token>/reporte/", views.reporte, name="reporte"),
    path("<str:token>/pod/", views.pod, name="pod"),
]
