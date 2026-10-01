from django.urls import path

from . import views

app_name = "integraciones"

from apps.envios import webhooks as webhooks_carriers

# Montado bajo "hooks/" en torre_project/urls.py → POST hooks/shopify/<tienda_id>/
# y los webhooks de rastreo de los carriers (2026-09-30), con token en la URL.
urlpatterns = [
    path("shopify/<int:tienda_id>/", views.webhook_shopify, name="webhook_shopify"),
    path("carriers/envia/<str:token>/", webhooks_carriers.webhook_envia, name="webhook_envia"),
    path("carriers/99minutos/<str:token>/", webhooks_carriers.webhook_99minutos, name="webhook_99minutos"),
]
