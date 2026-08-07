from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.raiz, name="raiz"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("entrar/", views.LoginView.as_view(), name="login"),
    path("salir/", views.LogoutView.as_view(), name="logout"),
    path("despues-de-entrar/", views.post_login, name="post_login"),
]
