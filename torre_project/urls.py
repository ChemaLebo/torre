from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls", namespace="core")),
    path("", include("apps.mensajeria.urls", namespace="mensajeria")),
    path("portal/", include("apps.portal.urls", namespace="portal")),
    path("piso/", include("apps.piso.urls", namespace="piso")),
    path("mesa/", include("apps.mesa.urls", namespace="mesa")),
    path("hooks/", include("apps.integraciones.urls", namespace="integraciones")),
    path("r/", include("apps.rastreo.urls", namespace="rastreo")),
    path("api/impresion/", include("apps.envios.urls", namespace="envios")),
]

# MEDIA no se sirve nunca — ni en dev: la evidencia lleva datos de clientes y
# compradores y sale SOLO por vistas con autorización (core:evidencia,
# rastreo:pod, envios api_impresion.pdf). Dev = prod, sin rutas de cortesía.
