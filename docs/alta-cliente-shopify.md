# Alta de un cliente con tienda Shopify

Proceso vigente desde el 2026-10-21 (primer uso: Soda Mate). Sustituye el link de instalación del Dashboard y el copiado de tokens: la tienda se conecta con el botón **Conectar con Shopify** de Mesa. Lo que sigue siendo a mano está marcado.

## 0. Antes de empezar: qué pedirle al cliente

- Dominio `*.myshopify.com` de la tienda y acceso de staff al admin (o que él apruebe con el link de conexión).
- Export de productos de su Shopify (Productos → Exportar → CSV): es la base del catálogo en Torre.
- Razón social, RFC, contacto, paquetería preferida y si necesitan que Torre traiga órdenes de más de 60 días.
- En su Shopify: una location para la bodega, p. ej. "WOP Fulfillment", activa y primera en la prioridad de surtido. Mientras tenga 0 existencias Shopify no le asigna nada; el push de inventario la llena cuando entre el primer ASN.

## 1. La app custom en el Dev Dashboard (a mano, una por tienda)

1. dev.shopify.com → Apps → **Create app**. Nombre con el patrón `<cliente>-wop-fulfillment` (p. ej. `sodamate-wop-fulfillment`).
2. **Settings → Client credentials**: copia el Client ID y el Client secret. Van directo al form de la tienda en Mesa; nunca al chat ni al repo.
3. Configuración de la versión:
   - App URL de relleno: `https://fulfillment.wop.partners/`.
   - Redirect URL **exacta**: `https://fulfillment.wop.partners/oauth/callback`.
   - "Use legacy install flow" **activado**. Es el authorization code grant: Shopify muestra los permisos, regresa a la redirect URL con un código y Torre lo cambia por el token. Es el único flujo para apps sin interfaz dentro del admin, como Torre; el "flujo nuevo" de Shopify (managed installation con token exchange) solo sirve para apps embebidas. Lo que Torre dejó de usar son los links de instalación que caducan y el token copiado a mano, no este ajuste. Si se apaga, Torre no recibe ningún token.
   - Access scopes, los 10 de Torre: `read_orders`, `write_orders`, `read_products`, `read_inventory`, `write_inventory`, `read_locations`, `read_fulfillments`, `write_fulfillments`, `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders`. `read_all_orders` solo si Soporte ya lo concedió a ESTA app (paso 6).
4. **Distribution → Custom distribution** → la tienda del cliente (su dominio myshopify). "Allow multi-store installs" apagado: solo aplicaría a varias tiendas de una misma organización Plus.
5. **Release / publicar la versión**: debe quedar Active. No generes el link de instalación: lo sustituye el botón.
6. Órdenes de más de 60 días: pide `read_all_orders` a Soporte de Shopify citando el Client ID. Cuando lo concedan: agrégalo a los scopes, publica otra versión, en Mesa marca "Soporte concedió read_all_orders" y aprieta **Actualizar permisos**.

## 2. En Mesa

1. **Clientes → Nuevo**: nombre, slug, razón social, RFC, contacto, buffer, integración de envíos, carriers preferente y respaldo, avisos al comprador, umbral de visto bueno, voz y branding del rastreo.
2. Ficha del cliente → form **Tienda**: dominio, Client ID, Client secret, la bandera de `read_all_orders` si aplica, activa. Token y webhook secret se dejan vacíos. Al guardar, el aviso dice "Ahora Conectar con Shopify desde la tabla".
3. Columna **Conexión** de la tabla de tiendas:
   - **Conectar con Shopify**: te manda a la pantalla de permisos de Shopify. Debes tener sesión de admin de ESA tienda en el mismo navegador.
   - **Link para el cliente**: el aviso trae un link que vale 7 días; se lo mandas y él lo abre con su sesión de admin, sin entrar a Mesa.
4. Shopify muestra los permisos → **Instalar**. Shopify regresa a Torre y la ficha muestra el resumen: conectada, "Webhooks: 4 creados", "Metafield: creada", la location elegida o la lista de disponibles, y los permisos que falten si alguno.
5. Si no se eligió location sola, **editar** la tienda y pegar el ID de la location de WOP de la lista del resumen.
6. **Sincronizar ahora**: catálogo y pedidos en segundo plano (backfill de 90 días; sin `read_all_orders` Shopify solo entrega 60). Revisa **Salud de sync** en unos minutos.
7. Tarifario, usuarios del portal (y enviar acceso), correos de incidencias.

## 3. Catálogo, antes del primer ASN

- Ficha del cliente → SKUs → **Transformar export de Shopify** con el CSV del cliente → vista previa → importar. Si prefieres esperar al sync, las variantes nacen inactivas "por completar".
- **Exportar CSV** de catálogo, completar `peso_gr`, `largo_cm`, `ancho_cm`, `alto_cm`, `requiere_lote`, `empaques_divisibles`, `es_kit`, `categoria`, `rotacion`, e importarlo. Shopify solo trae código, nombre, variante, código de barras y precio.
- Racks fijos: Bodega → Posiciones, o `cargar_asignaciones`.

## 4. Arranque físico

- ASN desde el portal del cliente o desde Mesa; recepción por escaneo con lotes; acomodo; el push de inventario lleva las existencias a su location y Shopify empieza a asignar.
- Orden de prueba pagada de punta a punta: webhook → Mesa → picking → empaque → guía → salida → fulfillment en Shopify.

## 5. Verificación

- Salud de sync: ingesta y push con fecha reciente.
- Evento `post_conexion` de la tienda en la auditoría: webhooks, locations y permisos.
- Si algún permiso falta, **Actualizar permisos** repite la aprobación con la lista completa.

## 6. Si algo falla

| Mensaje | Causa | Qué hacer |
|---|---|---|
| "Firma inválida" | El client secret pegado no es el de esa app | Corrige el secret en editar y vuelve a Conectar |
| "Conexión no iniciada desde Torre o ya usada" | El link caducó, ya se usó, o alguien apretó Conectar dos veces | Vuelve a apretar Conectar o genera otro link |
| 404 "Tienda desconocida" | El dominio en Mesa no es el de la tienda que aprobó | Corrige el dominio |
| Shopify: redirect_uri no permitida | La Redirect URL de la app no es exacta | Corrígela en el Dashboard y publica |
| Shopify: invalid scope | Un scope no está en la versión publicada, p. ej. `read_all_orders` sin concesión | Quita la bandera o publica la versión con el scope |
| "Webhooks: sin tocar: la tienda ya firma con otro secret" | Tienda vieja con webhooks de Notificaciones | Normal. Migrar es opcional: borra los de Notificaciones y vuelve a Actualizar permisos |

## 7. Tiendas que ya estaban conectadas (Colima, Infinitea)

Nada cambia solo. Si quieres usar **Actualizar permisos** desde Torre, pega una vez el Client ID y el secret de su app. Sus webhooks de Notificaciones siguen igual: Torre no los toca mientras firmen con otro secret.

## 8. ¿Y el CLI de Shopify?

Sirve para versionar la configuración de la app (`shopify.app.toml` con scopes, redirect y "legacy install flow") y publicar versiones con `shopify app deploy`, con un login por máquina que no caduca a cada rato. No crea la distribución custom hacia la tienda del cliente ni sustituye el Dashboard para eso, y con un cliente nuevo cada tanto son tres clics. Queda como opción si las apps se multiplican.
