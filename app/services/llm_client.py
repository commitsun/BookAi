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
    content: str | None      # None when finish_reason == "tool_calls"
    tokens_in: int
    tokens_out: int
    model: str
    tool_calls: list[dict] | None = None  # [{id, function: {name, arguments}}]
    finish_reason: str = "stop"           # stop | tool_calls


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
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Send a chat completion request, optionally with tool definitions.

        Args:
            messages: ``[{"role": "system|user|assistant|tool", "content": "…"}]``
            provider: Provider identifier.
            api_key: Provider API key.
            model: Model identifier.
            api_base_url: Base URL override.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in the response.
            tools: LLM tool/function definitions (OpenAI format).

        Returns:
            LLMResponse. If finish_reason == "tool_calls", content may be None
            and tool_calls contains the invocations.

        Raises:
            LLMClientError: On provider errors.
        """
        ...
