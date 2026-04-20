"""
LLM provider contract.

This module defines the interface that any LLM backend must implement.
The rest of BookAI depends only on ``LLMProvider`` and ``LLMResponse``
— never on a concrete implementation.

To swap backends (litellm → LangChain → raw httpx), change one line
in ``main.py`` where the provider is instantiated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    content: str
    tokens_in: int
    tokens_out: int
    model: str


class LLMClientError(Exception):
    """Raised when the LLM provider returns an error."""


@runtime_checkable
class LLMProvider(Protocol):
    """Contract that every LLM backend must satisfy."""

    async def chat(
        self,
        messages: list[dict],
        provider: str,
        api_key: str,
        model: str,
        api_base_url: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: ``[{"role": "system|user|assistant", "content": "…"}]``
            provider: Provider identifier (``"anthropic"``, ``"openai"``, …).
            api_key: Provider API key.
            model: Model identifier (e.g. ``"claude-sonnet-4-20250514"``).
            api_base_url: Base URL override (for self-hosted / compatible APIs).
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in the response.

        Returns:
            LLMResponse with content and usage metrics.

        Raises:
            LLMClientError: On provider errors.
        """
        ...
