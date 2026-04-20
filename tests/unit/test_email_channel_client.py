"""Unit tests for EmailChannelClient and HMAC signature validation."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.email_channel_client import (
    EmailChannelClient,
    EmailChannelError,
    validate_mailgun_signature,
)


# ---------------------------------------------------------------------------
# validate_mailgun_signature — pure function, no network
# ---------------------------------------------------------------------------


class TestValidateMailgunSignature:
    def test_valid_signature(self):
        import hashlib
        import hmac

        signing_key = "test-signing-key"
        timestamp = "1609459200"
        token = "abc123token"
        expected = hmac.new(
            signing_key.encode(),
            msg=(timestamp + token).encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        assert validate_mailgun_signature(
            token=token,
            timestamp=timestamp,
            signature=expected,
            signing_key=signing_key,
        ) is True

    def test_invalid_signature(self):
        assert validate_mailgun_signature(
            token="abc",
            timestamp="123",
            signature="badhex",
            signing_key="mykey",
        ) is False

    def test_empty_fields_return_false(self):
        assert validate_mailgun_signature(
            token="", timestamp="", signature="", signing_key=""
        ) is False

    def test_wrong_key_returns_false(self):
        import hashlib
        import hmac

        signing_key = "real-key"
        timestamp = "1609459200"
        token = "sometoken"
        sig = hmac.new(
            signing_key.encode(),
            msg=(timestamp + token).encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        assert validate_mailgun_signature(
            token=token,
            timestamp=timestamp,
            signature=sig,
            signing_key="wrong-key",
        ) is False


# ---------------------------------------------------------------------------
# EmailChannelClient.send_email — mock_mode
# ---------------------------------------------------------------------------


def _make_endpoint(mock_mode=True, domain="mg.test.com", api_key="key-test"):
    ep = MagicMock()
    ep.mock_mode = mock_mode
    ep.external_code = "hotel@test.com"
    ep.display_number = "Test Hotel"
    ep.config = {"mailgun_domain": domain, "api_key": api_key}
    return ep


class TestEmailChannelClientMockMode:
    @pytest.mark.asyncio
    async def test_mock_mode_returns_fake_id(self):
        client = EmailChannelClient(AsyncMock())
        ep = _make_endpoint(mock_mode=True)

        result = await client.send_email(
            to_address="guest@example.com",
            to_name="Guest",
            subject="Hello",
            text_body="Hi there",
            html_body=None,
            channel_endpoint=ep,
        )

        assert result.startswith("<mock-")
        assert result.endswith("@mock.mailgun.org>")

    @pytest.mark.asyncio
    async def test_missing_config_raises_error(self):
        http = AsyncMock()
        client = EmailChannelClient(http)
        ep = _make_endpoint(mock_mode=False, domain="", api_key="")

        with pytest.raises(EmailChannelError) as exc_info:
            await client.send_email(
                to_address="guest@example.com",
                to_name=None,
                subject="Hello",
                text_body="Hi",
                html_body=None,
                channel_endpoint=ep,
            )
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_mailgun_error_raises_email_channel_error(self):
        import httpx

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 400
        mock_response.text = '{"message": "Invalid recipient"}'

        http = AsyncMock()
        http.post = AsyncMock(return_value=mock_response)

        client = EmailChannelClient(http)
        ep = _make_endpoint(mock_mode=False)

        with pytest.raises(EmailChannelError) as exc_info:
            await client.send_email(
                to_address="guest@example.com",
                to_name="Guest",
                subject="Test",
                text_body="Body",
                html_body=None,
                channel_endpoint=ep,
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_successful_send_returns_message_id(self):
        import httpx

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"id": "<abc@mg.test.com>", "message": "Queued."}'
        mock_response.json = MagicMock(
            return_value={"id": "<abc@mg.test.com>", "message": "Queued."}
        )

        http = AsyncMock()
        http.post = AsyncMock(return_value=mock_response)

        client = EmailChannelClient(http)
        ep = _make_endpoint(mock_mode=False)

        result = await client.send_email(
            to_address="guest@example.com",
            to_name="Guest",
            subject="Test",
            text_body="Body",
            html_body=None,
            channel_endpoint=ep,
        )
        assert result == "<abc@mg.test.com>"
