from __future__ import annotations

import abc
from typing import Any


class Transport(abc.ABC):
    """Abstract transport layer for communicating with Odoo."""

    @abc.abstractmethod
    async def authenticate(self) -> None:
        """Authenticate against the Odoo instance."""

    @abc.abstractmethod
    async def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        """Search records and return field values."""

    @abc.abstractmethod
    async def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Read records by IDs."""

    @abc.abstractmethod
    async def create(self, model: str, vals: dict) -> int:
        """Create a record and return its ID."""

    @abc.abstractmethod
    async def write(self, model: str, ids: list[int], vals: dict) -> bool:
        """Update records."""

    @abc.abstractmethod
    async def unlink(self, model: str, ids: list[int]) -> bool:
        """Delete records."""

    @abc.abstractmethod
    async def call(
        self,
        model: str,
        method: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        """Call an arbitrary model method."""
