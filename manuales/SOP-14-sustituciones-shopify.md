# SOP-14 · Sustituir un producto sin stock (vía Shopify)

| Campo | Valor |
|---|---|
| **Código / versión** | SOP-14 · v1.0 · Septiembre 2026 |
| **Dueño** | Mesa de Control |
| **Frecuencia / disparador** | Pedido PENDIENTE con incidencia FAL y repuesto autorizado por el cliente (la marca) |
| **Tiempo estándar** | 5–10 min por pedido |
| **EPP requerido** | No aplica (trabajo de escritorio) |
| **Pantallas de Torre** | Shopify Admin → Orders → **Edit order** · Mesa → **Pedidos** (verificación) · Mesa → **Incidencias** (cerrar la FAL) |

## La regla de oro

La sustitución se hace **editando la orden en Shopify** — Torre la aplica sola.
No se toca el admin de Django ni el shell: el flujo oficial ya existe y deja el
kardex y el evento `edicion_orden` alineados.

## Pasos

1. Confirma con el cliente (la marca) qué producto entra como repuesto y cómo se maneja la diferencia de precio (política de la marca, no nuestra).
2. En **Shopify Admin → Orders**, abre la orden y elige **Edit order**.
3. **Remove item** sobre el producto sin stock (la cantidad que falta).
4. **Add product** con el repuesto acordado.
5. Si Shopify pide cobrar o reembolsar diferencia, sigue la política de la marca (con precio igual no pide nada).
6. Guarda la edición. Shopify manda el webhook y Torre encoge la línea sin stock (libera reserva si tenía), agrega la línea del repuesto y **reserva su stock**, todo con evento `edicion_orden` en el timeline.
7. En **Mesa → Pedidos** verifica que la línea nueva quedó reservada, y **resuelve la incidencia FAL**.

## Qué NO hacer

- **No editar líneas en el admin de Django**: deja la bandera de reserva y el kardex desalineados (el pedido se atora al empacar).
- **No sustituir con avance físico**: si el pedido ya está pickeándose, la edición abre incidencia en vez de aplicarse — coordina con piso para pausar primero.
- **No prometer al comprador**: el "va en camino" sale solo al firmar el manifiesto.

## Por qué funciona

Torre reacciona a `orders/updated` con lógica diff-idempotente: reducciones sin avance físico se aplican y liberan la reserva; aumentos en PENDIENTE reservan o abren FAL; cualquier conflicto con avance físico abre incidencia para un humano. La sustitución en PENDIENTE es exactamente el caso que se auto-aplica, por eso este SOP es puro Shopify y cero consola.
