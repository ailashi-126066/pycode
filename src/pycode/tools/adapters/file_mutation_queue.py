# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/file-mutation-queue.ts
"""Serialize mutations of the same file; different files still run in parallel."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

_locks: dict[str, asyncio.Lock] = {}


# Pi: withFileMutationQueue. The key is the real path, so aliases share one queue.
async def with_file_mutation_queue(key: str, fn: Callable[[], Awaitable[T]]) -> T:
    async with _locks.setdefault(key, asyncio.Lock()):
        return await fn()
