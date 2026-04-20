"""
Registry that manages one RoomdooClient + AgentLoader per BookAI Instance.

Clients are created lazily on first access and closed together at shutdown.
"""

import logging

from roomdoo_sdk import RoomdooClient
from roomdoo_sdk.transports.jsonrpc import JsonRpcTransport

from app.models.instance import Instance
from app.services.agent_loader import AgentLoader

log = logging.getLogger("instance_sdk_registry")


class InstanceSDKRegistry:

    def __init__(self) -> None:
        self._clients: dict[int, RoomdooClient] = {}
        self._loaders: dict[int, AgentLoader] = {}

    # -- Client management ---------------------------------------------------

    def get_client(self, instance: Instance) -> RoomdooClient | None:
        """Return an existing client or create one if the instance has Odoo config."""
        if instance.id in self._clients:
            return self._clients[instance.id]

        if not instance.roomdoo_db or not instance.roomdoo_username:
            return None

        transport = JsonRpcTransport(
            url=instance.instance_url,
            db=instance.roomdoo_db,
            username=instance.roomdoo_username,
            password=instance.roomdoo_password or "",
        )
        client = RoomdooClient(transport=transport)
        self._clients[instance.id] = client
        log.info(
            "Created RoomdooClient for instance %d (%s)",
            instance.id, instance.instance_url,
        )
        return client

    # -- AgentLoader management ----------------------------------------------

    def get_loader(self, instance_id: int) -> AgentLoader | None:
        return self._loaders.get(instance_id)

    async def get_or_load_agents(self, instance: Instance) -> AgentLoader | None:
        """Return the loader for this instance, creating and populating it if needed."""
        if instance.id in self._loaders:
            return self._loaders[instance.id]

        client = self.get_client(instance)
        if client is None:
            return None

        loader = AgentLoader(client)
        await loader.load_all()
        self._loaders[instance.id] = loader
        return loader

    def evict(self, instance_id: int) -> None:
        """Remove cached client and loader for an instance (e.g. after credential change)."""
        client = self._clients.pop(instance_id, None)
        self._loaders.pop(instance_id, None)
        if client:
            log.info("Evicted SDK client for instance %d", instance_id)

    # -- Lifecycle -----------------------------------------------------------

    async def close_all(self) -> None:
        for instance_id, client in self._clients.items():
            try:
                await client.close()
            except Exception:
                log.warning("Error closing client for instance %d", instance_id)
        self._clients.clear()
        self._loaders.clear()
        log.info("All SDK clients closed")
