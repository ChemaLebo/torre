"""Adapters de canal de salida.

WhatsApp Cloud API de Meta si hay WHATSAPP_TOKEN + WHATSAPP_PHONE_ID configurados;
ConsolaAdapter (imprime el mensaje) si no. El registro NotificacionEnviada y la
auditoría los pone el servicio, no el adapter: el adapter solo entrega.
"""
from django.conf import settings

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"


class MensajeAdapter:
    """Interfaz común: enviar(destinatario, cuerpo) -> {"ok": bool, "detalle": ...}."""

    canal = "consola"

    def enviar(self, destinatario, cuerpo):
        raise NotImplementedError


class WhatsAppCloudAPI(MensajeAdapter):
    """Cloud API de Meta (plantillas utility, centavos por conversación).

    Nada de mensajería masiva desde la app de WhatsApp Business: eso banea
    el número en semanas. Este adapter usa el endpoint oficial de Graph.
    """

    canal = "whatsapp"

    def enviar(self, destinatario, cuerpo):
        import requests  # lazy: el modo consola no necesita la dependencia

        url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": destinatario,
            "type": "text",
            "text": {"preview_url": True, "body": cuerpo},
        }
        try:
            respuesta = requests.post(url, json=payload, headers=headers, timeout=15)
            respuesta.raise_for_status()
        except requests.RequestException as exc:
            # No reventamos el flujo del pedido por una falla de canal:
            # el servicio registra el fallo y NO crea NotificacionEnviada,
            # de modo que un reintento posterior sí pueda salir.
            return {"ok": False, "detalle": f"WhatsApp Cloud API rechazó el envío: {exc}"}
        return {"ok": True, "detalle": respuesta.json()}


class ConsolaAdapter(MensajeAdapter):
    """Modo desarrollo/sin credenciales: imprime el mensaje. El registro queda en BD."""

    canal = "consola"

    def enviar(self, destinatario, cuerpo):
        print(f"[mensajeria · consola] Para: {destinatario}\n{cuerpo}\n{'-' * 60}")
        return {"ok": True, "detalle": "impreso en consola"}


def get_adapter():
    """WhatsApp real si hay credenciales; consola si no."""
    if settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_ID:
        return WhatsAppCloudAPI()
    return ConsolaAdapter()
