"""
In-memory cache of AI agent configurations loaded from Odoo via roomdoo-sdk.

One AgentLoader instance exists per BookAI Instance (hotel chain / Odoo tenant).
The cache is populated at first request and can be refreshed by webhook.
"""

import asyncio
import logging
from dataclasses import dataclass

from roomdoo_sdk import RoomdooClient
from roomdoo_sdk.models import AgentConfig, KBDocument

log = logging.getLogger("agent_loader")


@dataclass
class CachedAgent:
    config: AgentConfig
    documents: list[KBDocument]


class AgentLoader:
    def __init__(self, client: RoomdooClient):
        self._client = client
        self._cache: dict[str, CachedAgent] = {}
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return len(self._cache) > 0

    @property
    def count(self) -> int:
        return len(self._cache)

    def get(self, technical_name: str) -> CachedAgent | None:
        return self._cache.get(technical_name)

    def get_by_id(self, agent_id: int) -> CachedAgent | None:
        for cached in self._cache.values():
            if cached.config.id == agent_id:
                return cached
        return None

    def remove(self, technical_name: str) -> None:
        self._cache.pop(technical_name, None)

    def list_for_caller_type(self, caller_type: str) -> list[CachedAgent]:
        return [
            c for c in self._cache.values()
            if c.config.caller_type in (caller_type, "any")
        ]

    async def load_all(self) -> None:
        async with self._lock:
            agents = await self._client.agents.list(active=True)
            new_cache: dict[str, CachedAgent] = {}
            for agent in agents:
                try:
                    docs = await self._client.kb.list_by_agent(
                        agent.technical_name
                    )
                except Exception:
                    log.warning(
                        "Failed to load KB docs for agent %s, skipping docs",
                        agent.technical_name,
                    )
                    docs = []
                new_cache[agent.technical_name] = CachedAgent(
                    config=agent, documents=docs,
                )
            self._cache = new_cache
            log.info("Loaded %d agents into cache", len(new_cache))

    async def reload_agent(self, technical_name: str) -> None:
        async with self._lock:
            try:
                agent = await self._client.agents.get(technical_name)
                docs = await self._client.kb.list_by_agent(technical_name)
                self._cache[technical_name] = CachedAgent(
                    config=agent, documents=docs,
                )
                log.info("Reloaded agent %s", technical_name)
            except Exception:
                self._cache.pop(technical_name, None)
                raise

    async def reload_agents_by_ids(self, agent_ids: list[int]) -> None:
        """Reload specific agents by their Odoo IDs.

        For IDs not currently in cache (newly created agents), falls back
        to a full reload.
        """
        remaining = set(agent_ids)
        for cached in list(self._cache.values()):
            if cached.config.id in remaining:
                await self.reload_agent(cached.config.technical_name)
                remaining.discard(cached.config.id)
        if remaining:
            await self.load_all()
