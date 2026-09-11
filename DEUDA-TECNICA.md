# Deuda técnica

Registro de decisiones pospuestas a propósito: qué se dejó, por qué y qué falta
para cerrarlo. Una entrada por tema; se borra cuando se resuelve (el commit que
la cierra la menciona).

## Manuales por tipo de usuario

**Estado (2026-09-11):** el link "Manuales" del menú del portal está comentado
en `templates/base.html`; la ruta `portal:manuales` sigue viva y sirve el mismo
índice que Mesa.

**Por qué:** los 14 SOPs del índice (`templates/manuales/_indice.json`, generado
por `manage.py render_manuales` desde `manuales/`) son procedimientos internos
de bodega con dueño piso o Mesa; ninguno está escrito para el cliente. Mostrarlos
en el portal exponía cosas que no le son pertinentes.

**Decisión pendiente:** si se usan manuales en el portal, serán manuales
escritos para ese tipo de usuario, no los SOPs internos. Mismo criterio para
piso y Mesa: cada rol ve solo los suyos.

**Para cerrarlo:** una audiencia por manual en el frontmatter (p. ej. `Audiencia:
portal | piso | mesa`) que `render_manuales` lleve al índice; `manuales_publicados`
filtrando por rol; las vistas de portal, piso y Mesa pasando el suyo; restaurar el
link del portal cuando exista al menos un manual con audiencia portal.
