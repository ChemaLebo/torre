# Rastreo brandeado en el Shopify de Cervecería Colima

La página pública de rastreo (`/r/<token>/`) ya es 100% de la marca (colores,
logo y voz de Colima; el 3PL es invisible). Esta guía la lleva al dominio de la
tienda para el efecto "Servicio A+ hospedado en mi Shopify".

El link con token viaja solo: llega al comprador por WhatsApp (plantilla B) y
puede agregarse al email de confirmación de envío de Shopify. La página de la
tienda es un plus de confianza, no un requisito del flujo.

## Requisito

`BASE_URL_PUBLICA` apuntando al dominio público de Torre con HTTPS:

```bash
export BASE_URL_PUBLICA="https://rastreo.torre3pl.mx"
```

## Opción 1 — Página en la tienda con iframe (15 minutos, sin apps)

Admin de Shopify → **Tienda online → Páginas → Agregar página** → título
"Rastrea tu pedido" → en el editor, `< >` (HTML) y pega:

```html
<div id="rastreo-colima" style="min-height:80vh">
  <p style="text-align:center;padding:40px 16px;font-size:17px">
    Abre el enlace de rastreo que te enviamos por WhatsApp o correo.<br>
    Ahí ves tu pedido en vivo, paquete por paquete.
  </p>
</div>
<script>
  // Si la URL trae ?t=<token>, incrusta la página de rastreo en la tienda.
  (function () {
    var token = new URLSearchParams(window.location.search).get("t");
    if (!token || !/^[A-Za-z0-9_-]{12}$/.test(token)) return;
    var marco = document.createElement("iframe");
    marco.src = "https://rastreo.torre3pl.mx/r/" + token + "/?embed=1";
    marco.style.cssText = "width:100%;min-height:80vh;border:0";
    marco.title = "Rastreo de tu pedido";
    var caja = document.getElementById("rastreo-colima");
    caja.innerHTML = "";
    caja.appendChild(marco);
  })();
</script>
```

Con esto, `colima.mx/pages/rastrea-tu-pedido?t=<token>` muestra el rastreo
dentro de la tienda. Los links que Torre manda pueden apuntar ahí cambiando
`BASE_URL_PUBLICA` por `https://cervezadecolima.com/pages/rastrea-tu-pedido?t=`
(ajustar `rastreo.services.url_publica` si se elige este formato).

## Opción 2 — App Proxy (la página vive EN el dominio de la tienda)

Para servir el rastreo como `colima.mx/apps/rastreo/<token>/` sin iframe:

1. [partners.shopify.com](https://partners.shopify.com) → crear app ("Rastreo
   Colima", tipo custom, instalada solo en la tienda de Colima).
2. En la configuración de la app → **App proxy**:
   - Subpath prefix: `apps` · Subpath: `rastreo`
   - Proxy URL: `https://rastreo.torre3pl.mx/r/`
3. Shopify reenvía `GET colima.mx/apps/rastreo/<token>/` a
   `rastreo.torre3pl.mx/r/<token>/` con firma HMAC en los query params.
   La vista actual funciona tal cual; para producción endurecida, validar la
   firma (`signature`) con el secret de la app.
4. Poner `BASE_URL_PUBLICA="https://cervezadecolima.com/apps/rastreo"`.

## El link en el email de confirmación de envío de Shopify

Admin → **Configuración → Notificaciones → Confirmación de envío** — antes del
botón estándar agrega:

```liquid
{% if order.note_attributes.rastreo_url %}
  <a href="{{ order.note_attributes.rastreo_url }}"
     style="display:inline-block;background:#9E2B25;color:#fff;padding:12px 28px;
            border-radius:999px;text-decoration:none;font-weight:bold">
    Sigue tu pedido en vivo
  </a>
{% endif %}
```

Torre escribe `rastreo_url` como note attribute de la orden al generar el token
(pendiente de activar en integraciones: requiere scope `write_orders`). Mientras
tanto, el link viaja por WhatsApp (plantilla B), que es el canal principal.

## Qué ve el comprador (servicio A+)

- Estado en lenguaje humano con línea de tiempo (nada de "EN_TRANSITO").
- **Un tarjetón por paquete** cuando el pedido va dividido, con su contenido,
  guía y estado propio — y la explicación honesta: "viaja en 2 paquetes para
  que cada botella llegue perfecta".
- Foto de entrega (POD) cuando existe.
- **Reportar un problema** en 3 taps → abre incidencia en Torre, Colima se
  entera al instante y una persona responde en ≤30 min hábiles.
- Botón de WhatsApp directo. Cero datos sensibles expuestos (nombre de pila
  solamente; jamás dirección, teléfono ni precios).
