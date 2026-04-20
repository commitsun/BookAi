"""
Accumulator for all AI-related costs during the processing of a single
inbound message: agent LLM call, Whisper transcription, vision description.

Created at the start of message processing, passed through each service,
and flushed to Odoo as a single usage record at the end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("usage_tracker")

# Whisper pricing: $0.006 per minute
WHISPER_COST_PER_SECOND = 0.006 / 60


@dataclass
class UsageTracker:
    conversation_id: int
    # Agent LLM
    tokens_in: int = 0
    tokens_out: int = 0
    llm_cost_usd: float = 0.0
    llm_model: str = ""
    # Whisper
    whisper_seconds: float = 0.0
    whisper_cost_usd: float = 0.0
    # Vision
    vision_calls: int = 0
    vision_tokens_in: int = 0
    vision_tokens_out: int = 0
    vision_cost_usd: float = 0.0

    def add_llm(self, tokens_in: int, tokens_out: int, cost: float, model: str = "") -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.llm_cost_usd += cost
        if model:
            self.llm_model = model

    def add_whisper(self, duration_seconds: float) -> None:
        self.whisper_seconds += duration_seconds
        self.whisper_cost_usd += duration_seconds * WHISPER_COST_PER_SECOND

    def add_vision(self, tokens_in: int, tokens_out: int, cost: float) -> None:
        self.vision_calls += 1
        self.vision_tokens_in += tokens_in
        self.vision_tokens_out += tokens_out
        self.vision_cost_usd += cost

    @property
    def total_cost_usd(self) -> float:
        return self.llm_cost_usd + self.whisper_cost_usd + self.vision_cost_usd

    @property
    def has_usage(self) -> bool:
        return (
            self.tokens_in > 0
            or self.whisper_seconds > 0
            or self.vision_calls > 0
        )

    def summary(self) -> str:
        parts = []
        if self.tokens_in:
            parts.append(f"llm={self.tokens_in}/{self.tokens_out}t ${self.llm_cost_usd:.6f}")
        if self.whisper_seconds:
            parts.append(f"whisper={self.whisper_seconds:.1f}s ${self.whisper_cost_usd:.6f}")
        if self.vision_calls:
            parts.append(f"vision={self.vision_calls}x ${self.vision_cost_usd:.6f}")
        parts.append(f"total=${self.total_cost_usd:.6f}")
        return " | ".join(parts)
