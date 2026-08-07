"""envios no expone vistas de humanos (portal/piso/mesa consumen sus servicios).

Lo único público es la API del relay de impresión, montada en /api/impresion/
(torre_project/urls.py): la consume deploy/agente_impresion.py con token Bearer.
"""
from django.urls import path

from . import api_impresion

app_name = "envios"

urlpatterns = [
    path("pendientes/", api_impresion.pendientes, name="impresion_pendientes"),
    path("<int:trabajo_id>/resultado/", api_impresion.resultado, name="impresion_resultado"),
    path("<int:trabajo_id>/pdf/", api_impresion.pdf, name="impresion_pdf"),
]
