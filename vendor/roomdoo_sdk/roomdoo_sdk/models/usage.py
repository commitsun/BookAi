from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageRecord:
    pms_property_id: int
    agent_id: int
    llm_account_id: int
    tokens_in: int
    tokens_out: int
    model: str
    conversation_id: str
    status: str
    error_message: str | None = None
    cost_usd: float | None = None
    # Whisper transcription
    whisper_seconds: float | None = None
    whisper_cost_usd: float | None = None
    # Vision
    vision_calls: int | None = None
    vision_cost_usd: float | None = None
    # Total (agent + whisper + vision)
    total_cost_usd: float | None = None

    def to_odoo_vals(self) -> dict:
        vals = {
            "pms_property_id": self.pms_property_id,
            "agent_id": self.agent_id,
            "llm_account_id": self.llm_account_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "conversation_id": self.conversation_id,
            "status": self.status,
        }
        if self.error_message:
            vals["error_message"] = self.error_message
        if self.cost_usd is not None:
            vals["cost_usd"] = self.cost_usd
        if self.whisper_seconds is not None:
            vals["whisper_seconds"] = self.whisper_seconds
        if self.whisper_cost_usd is not None:
            vals["whisper_cost_usd"] = self.whisper_cost_usd
        if self.vision_calls is not None:
            vals["vision_calls"] = self.vision_calls
        if self.vision_cost_usd is not None:
            vals["vision_cost_usd"] = self.vision_cost_usd
        if self.total_cost_usd is not None:
            vals["total_cost_usd"] = self.total_cost_usd
        return vals
