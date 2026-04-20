"""
LLM provider backed by litellm.

Handles provider routing, retries, response normalization, and tool calling.
"""

import json
import logging

from litellm import acompletion

from app.services.llm_client import LLMClientError, LLMResponse

log = logging.getLogger("llm_litellm")

_PROVIDER_PREFIX: dict[str, str] = {
    "anthropic": "anthropic/",
    "openai": "openai/",
    "ollama": "ollama/",
    "ollama_chat": "ollama_chat/",
}


class LiteLLMProvider:
    """LLMProvider implementation using litellm with tool calling support."""

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
        if tools:
            kwargs["tools"] = tools

        try:
            response = await acompletion(**kwargs)
        except Exception as exc:
            raise LLMClientError(
                f"litellm error ({provider}/{model}): {exc}"
            ) from exc

        usage = response.usage
        choice = response.choices[0]
        message = choice.message

        # Extract tool calls if present
        tool_calls = None
        finish_reason = choice.finish_reason or "stop"

        if hasattr(message, "tool_calls") and message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                tool_calls.append({
                    "id": tc.id,
                    "function": {
                        "name": tc.function.name,
                        "arguments": args,
                    },
                })
            finish_reason = "tool_calls"

        return LLMResponse(
            content=message.content,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            model=response.model or model,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _resolve_model(provider: str, model: str) -> str:
        if "/" in model:
            return model
        prefix = _PROVIDER_PREFIX.get(provider, f"{provider}/")
        return f"{prefix}{model}"
