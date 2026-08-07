"""Incidencias no expone URLs propias.

Las vistas de incidencias viven en `portal` (incidencias del cliente) y en
`mesa` (gestión completa), que consumen los servicios de este módulo
(CONVENTIONS.md §portal, §mesa). El namespace queda registrado por si una
fase futura agrega endpoints propios (p. ej. export PDF por folio).
"""

app_name = "incidencias"

urlpatterns = []
