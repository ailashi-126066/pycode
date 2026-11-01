"""Immutable checked summary objects and atomic invalidation tombstones."""

import asyncio
import json
import re
from pathlib import Path

from pydantic import TypeAdapter

from pycode.context.domain.summaries import SummaryObject, SummarySource
from pycode.kernel import StorageError, atomic_write, data_hash, json_dumps, owned_io, plain_data


class FileSummaryStore:
    def __init__(self, directory: Path):
        self._directory = directory
        self._lock = asyncio.Lock()

    def path(self, job_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise StorageError("invalid_summary_id", "Invalid summary object ID")
        return self._directory / f"{job_id}.{suffix}.json"

    async def run_io(self, action):
        async with self._lock:
            try:
                return await owned_io(action)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise StorageError(
                    "summary_storage_error", "Summary object storage failed"
                ) from error

    def publish(self, path: Path, value: dict):
        envelope = {"value": value, "sha256": data_hash(value)}
        encoded = json_dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if path.exists():
            if path.read_text(encoding="utf-8") != encoded:
                raise StorageError("summary_object_conflict", "Immutable summary object changed")
            return
        atomic_write(path, encoded.encode("utf-8"))

    def read(self, path: Path, expected_type):
        envelope = json.loads(path.read_bytes())
        if envelope["sha256"] != data_hash(envelope["value"]):
            raise StorageError("summary_corrupt", "Summary content checksum verification failed")
        return TypeAdapter(expected_type).validate_json(json_dumps(envelope["value"]), strict=True)

    async def save_source(self, source: SummarySource) -> None:
        await self.run_io(
            lambda: self.publish(self.path(source.job_id, "source"), plain_data(source))
        )

    async def save_result(self, result: SummaryObject) -> None:
        def save():
            source = self.read(self.path(result.job_id, "source"), SummarySource)
            if data_hash(plain_data(source)) != result.source_hash:
                raise StorageError(
                    "summary_origin_conflict", "Result has a different source snapshot"
                )
            self.publish(self.path(result.job_id, "result"), plain_data(result))

        await self.run_io(save)

    async def load(self, job_id: str):
        def load():
            if self.path(job_id, "invalidated").exists():
                return None
            path = self.path(job_id, "source")
            if not path.exists():
                return None
            source = self.read(path, SummarySource)
            result_path = self.path(job_id, "result")
            result = self.read(result_path, SummaryObject) if result_path.exists() else None
            if source.job_id != job_id or (
                result is not None
                and (result.job_id != job_id or result.source_hash != data_hash(plain_data(source)))
            ):
                raise StorageError(
                    "summary_origin_conflict", "Summary identity verification failed"
                )
            return source, result

        return await self.run_io(load)

    async def invalidate(self, job_id: str, reason: str) -> None:
        def invalidate():
            path = self.path(job_id, "invalidated")
            if not path.exists():
                self.publish(path, {"job_id": job_id, "reason": reason})

        await self.run_io(invalidate)
