from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "core"

urlpatterns = [
    path("", views.raiz, name="raiz"),
    path("sw.js", views.service_worker, name="service_worker"),
    # Los navegadores lo piden solos en cada carga: sin ruta era un 404 por página.
    path("favicon.ico", RedirectView.as_view(url="/static/pwa/icono-192.png", permanent=True)),
    path("entrar/", views.LoginView.as_view(), name="login"),
    path("salir/", views.LogoutView.as_view(), name="logout"),
    path("despues-de-entrar/", views.post_login, name="post_login"),
    path("evidencia/<int:pk>/", views.evidencia, name="evidencia"),
]
