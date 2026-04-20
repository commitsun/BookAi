"""
LLM provider backed by litellm.

Handles provider routing, retries, and response normalization.
Swap this module out for a different backend by implementing
``LLMProvider`` from ``llm_client.py``.
"""

import logging

from litellm import acompletion

from app.services.llm_client import LLMClientError, LLMResponse

log = logging.getLogger("llm_litellm")

# litellm prefixes: maps our provider names → litellm model prefix
_PROVIDER_PREFIX: dict[str, str] = {
    "anthropic": "anthropic/",
    "openai": "openai/",
    "ollama": "ollama/",
    "ollama_chat": "ollama_chat/",
}


class LiteLLMProvider:
    """LLMProvider implementation using litellm.

    litellm unifies 140+ LLM providers behind a single ``acompletion()``
    call. It handles auth headers, retries, and response normalization
    automatically.
    """

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
        litellm_model = self._resolve_model(provider, model)

        kwargs: dict = {
            "model": litellm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "api_key": api_key,
        }
        if api_base_url:
            kwargs["api_base"] = api_base_url

        try:
            response = await acompletion(**kwargs)
        except Exception as exc:
            raise LLMClientError(f"litellm error ({provider}/{model}): {exc}") from exc

        usage = response.usage
        return LLMResponse(
            content=response.choices[0].message.content,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            model=response.model or model,
        )

    @staticmethod
    def _resolve_model(provider: str, model: str) -> str:
        """Build the litellm model string from provider + model name.

        litellm uses prefixed model names to route to the right provider:
        ``"anthropic/claude-sonnet-4-20250514"``, ``"openai/gpt-4"``, etc.
        If the model already contains a ``/``, assume it's pre-qualified.
        """
        if "/" in model:
            return model
        prefix = _PROVIDER_PREFIX.get(provider, f"{provider}/")
        return f"{prefix}{model}"
