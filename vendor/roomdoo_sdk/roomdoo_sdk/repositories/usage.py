from __future__ import annotations

from ..models.usage import UsageRecord
from ..transports.base import Transport


class UsageRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def log(self, record: UsageRecord) -> None:
        """Log usage, accumulating per conversation + agent.

        If a record already exists for this conversation_id + agent_id,
        all costs are added to the existing record.
        """
        existing = await self._transport.search_read(
            "bookai.agent.usage",
            [
                ("conversation_id", "=", record.conversation_id),
                ("agent_id", "=", record.agent_id),
            ],
            [
                "id", "tokens_in", "tokens_out", "cost_usd", "call_count",
                "whisper_seconds", "whisper_cost_usd",
                "vision_calls", "vision_cost_usd", "total_cost_usd",
            ],
            limit=1,
        )

        if existing:
            row = existing[0]
            await self._transport.write(
                "bookai.agent.usage",
                [row["id"]],
                {
                    "tokens_in": row["tokens_in"] + record.tokens_in,
                    "tokens_out": row["tokens_out"] + record.tokens_out,
                    "cost_usd": (row.get("cost_usd") or 0) + (record.cost_usd or 0),
                    "whisper_seconds": (row.get("whisper_seconds") or 0) + (record.whisper_seconds or 0),
                    "whisper_cost_usd": (row.get("whisper_cost_usd") or 0) + (record.whisper_cost_usd or 0),
                    "vision_calls": (row.get("vision_calls") or 0) + (record.vision_calls or 0),
                    "vision_cost_usd": (row.get("vision_cost_usd") or 0) + (record.vision_cost_usd or 0),
                    "total_cost_usd": (row.get("total_cost_usd") or 0) + (record.total_cost_usd or 0),
                    "call_count": (row.get("call_count") or 1) + 1,
                    "model": record.model,
                    "status": record.status,
                },
            )
        else:
            vals = record.to_odoo_vals()
            vals["call_count"] = 1
            await self._transport.create(
                "bookai.agent.usage", vals
            )

    async def summary_by_agent(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Usage aggregated by agent."""
        domain = self._build_date_domain(
            date_from, date_to
        )
        return await self._transport.call(
            "bookai.agent.usage",
            "read_group",
            args=[domain],
            kwargs={
                "fields": [
                    "agent_id",
                    "tokens_in:sum",
                    "tokens_out:sum",
                    "total_cost_usd:sum",
                    "call_count:sum",
                ],
                "groupby": ["agent_id"],
            },
        )

    async def summary_by_property(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Usage aggregated by property."""
        domain = self._build_date_domain(
            date_from, date_to
        )
        return await self._transport.call(
            "bookai.agent.usage",
            "read_group",
            args=[domain],
            kwargs={
                "fields": [
                    "pms_property_id",
                    "tokens_in:sum",
                    "tokens_out:sum",
                    "total_cost_usd:sum",
                    "call_count:sum",
                ],
                "groupby": ["pms_property_id"],
            },
        )

    async def summary_by_model(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Usage aggregated by LLM model."""
        domain = self._build_date_domain(
            date_from, date_to
        )
        return await self._transport.call(
            "bookai.agent.usage",
            "read_group",
            args=[domain],
            kwargs={
                "fields": [
                    "model",
                    "tokens_in:sum",
                    "tokens_out:sum",
                    "total_cost_usd:sum",
                    "call_count:sum",
                ],
                "groupby": ["model"],
            },
        )

    @staticmethod
    def _build_date_domain(date_from, date_to):
        domain: list = []
        if date_from:
            domain.append(
                ("timestamp", ">=", date_from)
            )
        if date_to:
            domain.append(("timestamp", "<=", date_to))
        return domain
