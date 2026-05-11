from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from ..exceptions import TransportError
from .base import Transport

_logger = logging.getLogger(__name__)

_JSONRPC_HEADERS = {"Content-Type": "application/json"}


class JsonRpcTransport(Transport):
    """JSON-RPC 2.0 transport for Odoo."""

    def __init__(
        self,
        url: str,
        db: str,
        username: str,
        password: str,
        *,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = 30.0,
    ):
        self._url = url.rstrip("/")
        self._db = db
        self._username = username
        self._password = password
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._uid: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._request_id = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _jsonrpc_call(
        self, service: str, method: str, args: list
    ) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": self._next_id(),
            "params": {
                "service": service,
                "method": method,
                "args": args,
            },
        }
        endpoint = f"{self._url}/jsonrpc"
        session = await self._get_session()
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                async with session.post(
                    endpoint,
                    data=json.dumps(payload),
                    headers=_JSONRPC_HEADERS,
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()

                if "error" in result:
                    err = result["error"]
                    message = err.get("data", {}).get("message", "") or err.get(
                        "message", str(err)
                    )
                    raise TransportError(f"JSON-RPC error: {message}")

                return result.get("result")

            except TransportError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    delay = self._retry_delay * (2 ** (attempt - 1))
                    _logger.warning(
                        "JSON-RPC attempt %d/%d failed: %s. Retrying in %.1fs",
                        attempt,
                        self._max_retries,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise TransportError(
            f"JSON-RPC failed after {self._max_retries} attempts: {last_error}"
        )

    async def _ensure_authenticated(self) -> int:
        if self._uid is None:
            await self.authenticate()
        return self._uid  # type: ignore[return-value]

    async def authenticate(self) -> None:
        uid = await self._jsonrpc_call(
            "common", "login", [self._db, self._username, self._password]
        )
        if not uid:
            raise TransportError(
                f"Authentication failed for {self._username}@{self._db}"
            )
        self._uid = uid

    async def _execute_kw(
        self,
        model: str,
        method: str,
        args: list,
        kwargs: dict | None = None,
    ) -> Any:
        uid = await self._ensure_authenticated()
        return await self._jsonrpc_call(
            "object",
            "execute_kw",
            [self._db, uid, self._password, model, method, args, kwargs or {}],
        )

    async def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if offset:
            kwargs["offset"] = offset
        if order:
            kwargs["order"] = order
        return await self._execute_kw(model, "search_read", [domain], kwargs)

    async def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = fields
        return await self._execute_kw(model, "read", [ids], kwargs)

    async def create(self, model: str, vals: dict) -> int:
        return await self._execute_kw(model, "create", [vals])

    async def write(self, model: str, ids: list[int], vals: dict) -> bool:
        return await self._execute_kw(model, "write", [ids, vals])

    async def unlink(self, model: str, ids: list[int]) -> bool:
        return await self._execute_kw(model, "unlink", [ids])

    async def call(
        self,
        model: str,
        method: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        return await self._execute_kw(model, method, args or [], kwargs)
