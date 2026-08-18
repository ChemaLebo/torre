"""Cliente HTTP de la Admin API de Shopify (GraphQL para inventario, REST para polling).

Semántica del push (BLUEPRINT §2.2): Torre empuja `on_hand` vía
`inventorySetQuantities` con `compareQuantity` — un push con snapshot viejo FALLA
en vez de pisar al nuevo. Shopify deriva `available = on_hand − committed`.
"""
import requests
from django.conf import settings

# Cota de seguridad para la paginación del polling de pedidos.
MAX_PAGINAS_PEDIDOS = 20

CONSULTA_INVENTARIO_SKU = """
query inventarioPorSku($consulta: String!, $locationId: ID!) {
  productVariants(first: 10, query: $consulta) {
    edges {
      node {
        sku
        inventoryItem {
          id
          inventoryLevel(locationId: $locationId) {
            quantities(names: ["on_hand"]) {
              name
              quantity
            }
          }
        }
      }
    }
  }
}
"""

MUTACION_SET_ON_HAND = """
mutation fijarOnHand($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup {
      reason
      changes { name delta }
    }
    userErrors { code field message }
  }
}
"""


CONSULTA_FULFILLMENT_ORDERS = """
query ordenesDeFulfillment($id: ID!) {
  order(id: $id) {
    id
    fulfillmentOrders(first: 10) {
      edges { node { id status } }
    }
  }
}
"""

# fulfillmentCreateV2 quedó deprecado (2024-07): en versiones modernas la
# mutación es fulfillmentCreate con el mismo input.
MUTACION_CREAR_FULFILLMENT = """
mutation crearFulfillment($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""


class ShopifyError(Exception):
    """Error de la API de Shopify (HTTP, GraphQL o userErrors)."""


class ShopifyClient:
    """Cliente por tienda. Requiere `tienda.token`; sin token se usa el modo mock
    en services (este cliente nunca se instancia)."""

    def __init__(self, tienda, timeout=15):
        if not tienda.token:
            raise ShopifyError(f"La tienda {tienda.dominio} no tiene token: usa el modo mock.")
        self.tienda = tienda
        self.timeout = timeout
        self.version = settings.SHOPIFY_API_VERSION
        self.base = f"https://{tienda.dominio}/admin/api/{self.version}"
        self.sesion = requests.Session()
        self.sesion.headers.update({
            "X-Shopify-Access-Token": tienda.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ── infraestructura ──
    @property
    def location_gid(self):
        lid = str(self.tienda.location_id or "").strip()
        if not lid:
            raise ShopifyError(f"La tienda {self.tienda.dominio} no tiene location_id configurado.")
        return lid if lid.startswith("gid://") else f"gid://shopify/Location/{lid}"

    def graphql(self, query, variables=None):
        """POST /graphql.json. Levanta ShopifyError ante errores HTTP o de GraphQL."""
        try:
            resp = self.sesion.post(
                f"{self.base}/graphql.json",
                json={"query": query, "variables": variables or {}},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            datos = resp.json()
        except requests.RequestException as exc:
            raise ShopifyError(f"HTTP GraphQL {self.tienda.dominio}: {exc}") from exc
        if datos.get("errors"):
            raise ShopifyError(f"GraphQL {self.tienda.dominio}: {datos['errors']}")
        return datos.get("data") or {}

    # ── inventario ──
    def consultar_inventario_sku(self, codigo_sku):
        """Resuelve un código de SKU a (inventory_item_gid, on_hand_actual) en nuestra location.

        El on_hand actual es el snapshot que va como `compareQuantity` del push.
        """
        datos = self.graphql(CONSULTA_INVENTARIO_SKU, {
            "consulta": f'sku:"{codigo_sku}"',
            "locationId": self.location_gid,
        })
        edges = (datos.get("productVariants") or {}).get("edges") or []
        for edge in edges:
            nodo = edge.get("node") or {}
            if (nodo.get("sku") or "").strip() != codigo_sku:
                continue  # la búsqueda de Shopify es difusa: exigimos match exacto
            item = nodo.get("inventoryItem") or {}
            if not item.get("id"):
                break
            nivel = item.get("inventoryLevel") or {}
            on_hand_actual = 0
            for q in nivel.get("quantities") or []:
                if q.get("name") == "on_hand":
                    on_hand_actual = int(q.get("quantity") or 0)
            return item["id"], on_hand_actual
        raise ShopifyError(f"SKU '{codigo_sku}' no encontrado en {self.tienda.dominio} (location {self.tienda.location_id}).")

    def set_on_hand(self, inventory_item_gid, cantidad, compare_quantity):
        """Fija on_hand con concurrencia optimista: si el snapshot ya no coincide, falla."""
        datos = self.graphql(MUTACION_SET_ON_HAND, {
            "input": {
                "name": "on_hand",
                "reason": "correction",
                "ignoreCompareQuantity": False,
                "quantities": [{
                    "inventoryItemId": inventory_item_gid,
                    "locationId": self.location_gid,
                    "quantity": int(cantidad),
                    "compareQuantity": int(compare_quantity),
                }],
            },
        })
        resultado = datos.get("inventorySetQuantities") or {}
        errores = resultado.get("userErrors") or []
        if errores:
            raise ShopifyError(f"inventorySetQuantities {self.tienda.dominio}: {errores}")
        return resultado

    # ── fulfillment (write-back al marcar RECOLECTADO) ──
    def fulfillment_orders(self, order_id):
        """[(gid, status)] de las fulfillment orders del pedido.

        Shopify moderno no 'fulfillea' el pedido directo: se fulfillean sus
        fulfillment orders. El caller decide cuáles son fulfilleables.
        """
        gid = str(order_id)
        if not gid.startswith("gid://"):
            gid = f"gid://shopify/Order/{gid}"
        datos = self.graphql(CONSULTA_FULFILLMENT_ORDERS, {"id": gid})
        orden = datos.get("order") or {}
        if not orden:
            raise ShopifyError(f"Pedido {order_id} no existe en {self.tienda.dominio}.")
        edges = (orden.get("fulfillmentOrders") or {}).get("edges") or []
        nodos = [e.get("node") or {} for e in edges]
        return [(n["id"], n.get("status") or "") for n in nodos if n.get("id")]

    def crear_fulfillment(self, fulfillment_order_ids, numeros_guia, url_rastreo, carrier, notificar=True):
        """Crea UN fulfillment sobre esas fulfillment orders, con tracking.

        notifyCustomer=True es el gatillo del correo nativo de envío de
        Shopify — el link de rastreo es NUESTRA página brandeada del cliente.
        """
        tracking = {"company": carrier or "", "url": url_rastreo or ""}
        numeros = [n for n in numeros_guia if n]
        if len(numeros) == 1:
            tracking["number"] = numeros[0]
        elif numeros:
            tracking["numbers"] = numeros
        datos = self.graphql(MUTACION_CREAR_FULFILLMENT, {
            "fulfillment": {
                "lineItemsByFulfillmentOrder": [
                    {"fulfillmentOrderId": fid} for fid in fulfillment_order_ids
                ],
                "notifyCustomer": bool(notificar),
                "trackingInfo": tracking,
            },
        })
        resultado = datos.get("fulfillmentCreate") or {}
        errores = resultado.get("userErrors") or []
        if errores:
            raise ShopifyError(f"fulfillmentCreate {self.tienda.dominio}: {errores}")
        return resultado.get("fulfillment") or {}

    # ── pedidos (polling de respaldo) ──
    def listar_pedidos(self, updated_at_min=None):
        """GET /orders.json?updated_at_min=... con paginación por Link header."""
        params = {"status": "any", "limit": 250}
        if updated_at_min:
            params["updated_at_min"] = updated_at_min.isoformat()
        url = f"{self.base}/orders.json"
        pedidos = []
        for _ in range(MAX_PAGINAS_PEDIDOS):
            try:
                resp = self.sesion.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                cuerpo = resp.json()
            except requests.RequestException as exc:
                raise ShopifyError(f"HTTP orders.json {self.tienda.dominio}: {exc}") from exc
            pedidos.extend(cuerpo.get("orders") or [])
            siguiente = resp.links.get("next")
            if not siguiente:
                break
            url, params = siguiente["url"], None  # el cursor page_info ya viene en la URL
        return pedidos
