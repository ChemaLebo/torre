"""Montado en la raíz bajo "oauth/": el callback de Shopify (la redirect URL
registrada en cada app) y el link de conexión para el cliente."""
from django.urls import path

from . import views

app_name = "oauth"

urlpatterns = [
    path("callback", views.oauth_callback, name="callback"),
    path("app", views.oauth_app, name="app"),
    path("conectar/<str:firma>/", views.oauth_conectar_por_link, name="conectar_por_link"),
]
