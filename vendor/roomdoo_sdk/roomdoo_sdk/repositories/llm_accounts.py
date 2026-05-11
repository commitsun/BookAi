from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.llm_account import LLMAccount
from ..transports.base import Transport

_LLM_ACCOUNT_FIELDS = [
    "id",
    "name",
    "provider",
    "api_key",
    "api_base_url",
    "default_model",
]


class LLMAccountRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def list(self) -> list[LLMAccount]:
        """All active LLM accounts (without api_key)."""
        records = await self._transport.search_read(
            "bookai.llm.account",
            [("active", "=", True)],
            fields=[
                "id", "name", "provider",
                "api_base_url", "default_model",
            ],
        )
        return [
            LLMAccount(
                id=r["id"],
                name=r.get("name", ""),
                provider=r.get("provider", ""),
                api_base_url=r.get("api_base_url") or None,
                default_model=(
                    r.get("default_model") or None
                ),
            )
            for r in records
        ]

    async def get(self, account_id: int) -> LLMAccount:
        """Get LLM account details (without api_key)."""
        records = await self._transport.read(
            "bookai.llm.account",
            [account_id],
            fields=[
                "id", "name", "provider",
                "api_base_url", "default_model",
            ],
        )
        if not records:
            raise NotFoundError(
                f"LLM Account {account_id} not found"
            )
        r = records[0]
        return LLMAccount(
            id=r["id"],
            name=r.get("name", ""),
            provider=r.get("provider", ""),
            api_base_url=r.get("api_base_url") or None,
            default_model=r.get("default_model") or None,
        )

    async def get_by_agent(self, technical_name: str) -> LLMAccount:
        agents = await self._transport.search_read(
            "bookai.agent",
            [("technical_name", "=", technical_name)],
            fields=["llm_account_id"],
            limit=1,
        )
        if not agents:
            raise NotFoundError(f"Agent '{technical_name}' not found")
        account_id = agents[0]["llm_account_id"][0]
        records = await self._transport.read(
            "bookai.llm.account", [account_id], fields=_LLM_ACCOUNT_FIELDS
        )
        if not records:
            raise NotFoundError(f"LLM Account {account_id} not found")
        data = records[0]
        return LLMAccount(
            id=data["id"],
            name=data["name"],
            provider=data["provider"],
            api_key=data.get("api_key") or None,
            api_base_url=data.get("api_base_url") or None,
            default_model=data.get("default_model") or None,
        )
