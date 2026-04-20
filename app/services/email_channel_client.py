"""
Adapter for the Mailgun email API.

All HTTP calls to Mailgun go through EmailChannelClient. It takes a pre-configured
httpx.AsyncClient so callers can control lifecycle and inject test doubles.

### mock_mode

When ``channel_endpoint.mock_mode`` is True, ``send_email()`` skips the real
HTTP call and returns a synthetic ``<mock-{hex}@mock.mailgun.org>`` Message-ID.
This allows local development and CI to exercise the full email flow without
a real Mailgun account.

### HMAC signature validation

Mailgun signs every inbound and delivery webhook with a timestamp, token, and
HMAC-SHA256 signature. ``validate_mailgun_signature()`` is a pure function
(no network calls) so it can be called from route handlers before any DB work.

### Multi-account support

Each ChannelEndpoint with ``channel="email"`` carries its own Mailgun credentials
in ``config JSONB`` (``mailgun_domain``, ``api_key``, ``signing_key``). There is
no global credential; every method receives the endpoint as an argument.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid

import httpx

from app.models.channel import ChannelEndpoint
from app.services.whatsapp_client import ChannelError

log = logging.getLogger("email_channel_client")

MAILGUN_API_BASE = "https://api.mailgun.net/v3"


class EmailChannelError(ChannelError):
    """Mailgun-specific channel error."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(status_code, body)
        # Override the default message for clarity in logs
        Exception.__init__(self, f"Mailgun API {status_code}: {body[:200]}")


class EmailChannelClient:
    """
    Thin async wrapper around the Mailgun Messages API.

    Callers are responsible for owning the httpx.AsyncClient lifecycle.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_email(
        self,
        *,
        to_address: str,
        to_name: str | None,
        subject: str,
        text_body: str | None,
        html_body: str | None,
        channel_endpoint: ChannelEndpoint,
        reply_to: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> str:
        """
        Send an email via Mailgun and return the provider Message-ID.

        The ``From`` header is constructed as:
            "{display_number} <{external_code}>"
        e.g. "Alda Hotels <reservas@alda.es>"

        Returns the Message-ID (<...@mailgun.org>) to store for threading.
        Raises EmailChannelError on non-2xx Mailgun responses.
        """
        if channel_endpoint.mock_mode:
            fake_id = f"<mock-{uuid.uuid4().hex[:16]}@mock.mailgun.org>"
            log.info(
                "mock_mode: skipping Mailgun call, fake_id=%s to=%s",
                fake_id,
                to_address,
            )
            return fake_id

        config = channel_endpoint.config or {}
        domain = config.get("mailgun_domain", "")
        api_key = config.get("api_key", "")
        if not domain or not api_key:
            raise EmailChannelError(
                500,
                "ChannelEndpoint config missing mailgun_domain or api_key",
            )

        from_addr = channel_endpoint.external_code
        from_name = channel_endpoint.display_number
        from_header = (
            f"{from_name} <{from_addr}>" if from_name else from_addr
        )

        to_header = f"{to_name} <{to_address}>" if to_name else to_address

        data: dict[str, str] = {
            "from": from_header,
            "to": to_header,
            "subject": subject,
        }
        if text_body:
            data["text"] = text_body
        if html_body:
            data["html"] = html_body
        if reply_to:
            data["h:Reply-To"] = reply_to
        if in_reply_to:
            data["h:In-Reply-To"] = in_reply_to
        if references:
            data["h:References"] = references

        url = f"{MAILGUN_API_BASE}/{domain}/messages"
        response = await self._http.post(
            url,
            data=data,
            auth=("api", api_key),
            timeout=30.0,
        )

        body_text = response.text
        if response.status_code >= 300:
            log.error(
                "Mailgun send_email failed status=%s body=%s",
                response.status_code,
                body_text[:500],
            )
            raise EmailChannelError(response.status_code, body_text)

        # Mailgun returns {"id": "<abc@mg.alda.es>", "message": "Queued. ..."}
        try:
            provider_message_id: str = response.json()["id"]
        except Exception:
            # Fallback: generate a synthetic ID to avoid threading failures
            provider_message_id = f"<unknown-{uuid.uuid4().hex}@mailgun.org>"
            log.warning(
                "Could not parse Mailgun response JSON, using synthetic id=%s",
                provider_message_id,
            )

        return provider_message_id


# ---------------------------------------------------------------------------
# HMAC signature validation (pure function — no network)
# ---------------------------------------------------------------------------


def validate_mailgun_signature(
    *,
    token: str,
    timestamp: str,
    signature: str,
    signing_key: str,
) -> bool:
    """
    Validate a Mailgun webhook HMAC-SHA256 signature.

    Mailgun computes: HMAC-SHA256(signing_key, timestamp + token)
    and encodes the result as a hex string.

    Returns True if valid, False otherwise.
    """
    if not all([token, timestamp, signature, signing_key]):
        return False
    expected = hmac.new(
        signing_key.encode("utf-8"),
        msg=(timestamp + token).encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
