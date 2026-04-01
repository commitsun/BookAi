"""
Adapter for the Meta WhatsApp Cloud API.

All HTTP calls to Meta go through this class. It takes a pre-configured
httpx.AsyncClient so callers can control lifecycle and inject test doubles.

### mock_mode

When ``channel_endpoint.mock_mode`` is True, ``_post()`` skips the real
HTTP call and returns a fake ``wamid.mock.<hex>`` ID. This allows local
development and CI to exercise the full message flow (persistence,
Socket.IO events, unread counts) without a real WhatsApp account.
Set ``mock_mode = true`` on the ChannelEndpoint row in the database.

### Multi-account support

Each ``ChannelEndpoint`` carries its own ``access_token``,
``external_code`` (phone_number_id) and ``verify_token``. There is no
global WhatsApp credential — every method receives the endpoint as an
argument and uses its credentials directly.
"""

import logging
import uuid

import httpx

from app.models.channel import ChannelEndpoint

log = logging.getLogger("whatsapp_client")

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"


class ChannelError(Exception):
    """Base error for any channel provider failure (WhatsApp, Telegram, SMS, …)."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Channel API {status_code}: {body[:200]}")


class WhatsAppError(ChannelError):
    """WhatsApp-specific channel error (Meta Graph API)."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(status_code, body)
        # Override the message to keep legacy context
        Exception.__init__(self, f"Meta API {status_code}: {body[:200]}")


class WhatsAppClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def send_template(
        self,
        to: str,
        channel_endpoint: ChannelEndpoint,
        template_name: str,
        language: str,
        components: list[dict],
    ) -> str:
        """Send a pre-approved template. Returns the Meta wa_message_id."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        return await self._post(channel_endpoint, payload)

    async def send_text(
        self,
        to: str,
        channel_endpoint: ChannelEndpoint,
        text: str,
    ) -> str:
        """Send a plain text message within the 24-hour window. Returns wa_message_id."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        return await self._post(channel_endpoint, payload)

    async def mark_read(self, wa_message_id: str, channel_endpoint: ChannelEndpoint) -> None:
        """Mark an inbound message as read (fire-and-forget, errors are only logged)."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wa_message_id,
        }
        url = f"{GRAPH_API_BASE}/{channel_endpoint.external_code}/messages"
        headers = {"Authorization": f"Bearer {channel_endpoint.access_token}"}
        try:
            await self._http.post(url, json=payload, headers=headers, timeout=5)
        except Exception as exc:
            log.warning("mark_read failed wa_message_id=%s: %s", wa_message_id, exc)

    async def _post(self, channel_endpoint: ChannelEndpoint, payload: dict) -> str:
        if channel_endpoint.mock_mode:
            fake_id = f"wamid.mock.{uuid.uuid4().hex[:12]}"
            log.debug("mock_mode — skipping Meta API call, fake id=%s", fake_id)
            return fake_id

        url = f"{GRAPH_API_BASE}/{channel_endpoint.external_code}/messages"
        headers = {
            "Authorization": f"Bearer {channel_endpoint.access_token}",
            "Content-Type": "application/json",
        }
        try:
            r = await self._http.post(url, json=payload, headers=headers, timeout=15)
        except httpx.TimeoutException as exc:
            raise WhatsAppError(0, f"Request timeout: {exc}") from exc
        if r.status_code != 200:
            raise WhatsAppError(r.status_code, r.text)
        data = r.json()
        messages = data.get("messages") or []
        return messages[0].get("id", "") if messages else ""
