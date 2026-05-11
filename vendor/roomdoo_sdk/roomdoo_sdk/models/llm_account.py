from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMAccount:
    id: int
    name: str
    provider: str
    api_key: str | None = None
    api_base_url: str | None = None
    default_model: str | None = None
