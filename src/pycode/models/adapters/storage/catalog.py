"""Atomic catalog cache containing model identities and capabilities, never keys."""

import asyncio
import json
from dataclasses import fields
from pathlib import Path

from pydantic import TypeAdapter

from pycode.kernel import StorageError, atomic_write, json_dumps, owned_io, plain_data
from pycode.models.domain.models import CatalogSnapshot


class FileCatalogStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def run_io(self, operation):
        async with self._lock:
            try:
                return await owned_io(operation)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise StorageError(
                    "invalid_catalog_cache", "Cannot read or publish model catalog"
                ) from error

    async def load(self) -> CatalogSnapshot | None:
        def read():
            if not self._path.exists():
                return None
            payload = json.loads(self._path.read_bytes())
            if (
                not isinstance(payload, dict)
                or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1
            ):
                raise ValueError("Unsupported catalog schema")
            payload.pop("schema_version")
            if set(payload) - {field.name for field in fields(CatalogSnapshot)}:
                raise ValueError("Unsupported catalog fields")
            return TypeAdapter(CatalogSnapshot).validate_json(json_dumps(payload), strict=True)

        return await self.run_io(read)

    async def save(self, snapshot: CatalogSnapshot) -> None:
        def write():
            payload = {"schema_version": 1, **plain_data(snapshot)}
            encoded = json_dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            atomic_write(self._path, encoded)

        await self.run_io(write)
