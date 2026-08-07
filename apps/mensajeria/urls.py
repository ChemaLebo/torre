from django.urls import path

from . import views

app_name = "mensajeria"

urlpatterns = [
    # Montado en la raíz del proyecto: URL neutra para piso Y mesa.
    path("push/suscribir/", views.suscribir_push, name="suscribir_push"),
    path("push/baja/", views.baja_push, name="baja_push"),
]
