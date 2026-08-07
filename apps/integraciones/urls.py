from django.urls import path

from . import views

app_name = "integraciones"

# Montado bajo "hooks/" en torre_project/urls.py → POST hooks/shopify/<tienda_id>/
urlpatterns = [
    path("shopify/<int:tienda_id>/", views.webhook_shopify, name="webhook_shopify"),
]
