"""Helpers to bridge async calls from sync code safely."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_coro_sync(coro: Coroutine[Any, Any, T]) -> T:
    """
    Run a coroutine from sync code without mutating a running event loop.

    - If no loop is running in this thread, run directly via asyncio.run.
    - If a loop is already running (e.g. uvicorn request loop), run in a worker
      thread with its own loop and block until completion.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()
