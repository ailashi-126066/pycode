"""Durable per-attempt SQLite records; no provider credentials are serialized."""

import asyncio
import json
import sqlite3
import time
from dataclasses import fields, replace
from pathlib import Path

from pycode.kernel import StorageError, UsageRecord, json_dumps, owned_io, plain_data
from pycode.models.domain.requests import RequestRecord


def decode_request(payload):
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {
            field.name for field in fields(RequestRecord)
        }:
            raise ValueError("Unsupported request record fields")
        if data["usage"] is not None:
            data["usage"] = UsageRecord(**data["usage"])
        return RequestRecord(**data)
    except (ValueError, TypeError, KeyError) as error:
        raise StorageError(
            "invalid_request_record", "Invalid or unsupported request record format"
        ) from error


class SQLiteRequestStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS model_requests ("
                "request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "status TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._connection.commit()
            self._lock = asyncio.Lock()
        except BaseException:
            # No owner receives this store when setup fails; release the handle
            # here rather than relying on a later asynchronous close.
            self._connection.close()
            raise

    async def run_io(self, operation):
        async with self._lock:
            try:
                return await owned_io(operation)
            except sqlite3.Error as error:
                raise StorageError(
                    "request_store_error", "Request database operation failed"
                ) from error

    async def start(self, record: RequestRecord) -> None:
        if record.status != "started" or record.usage is not None:
            raise ValueError("New attempt must be started with no settled usage")
        await self.run_io(lambda: self.write(record, initial=True))

    async def settle(self, record: RequestRecord) -> None:
        if record.status == "started":
            raise ValueError("Settled attempt requires a terminal status")
        await self.run_io(lambda: self.write(record, initial=False))

    def write(self, record: RequestRecord, *, initial: bool) -> None:
        payload = json_dumps(
            plain_data(record), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        previous = self._connection.execute(
            "SELECT status, payload FROM model_requests WHERE request_id = ?", (record.request_id,)
        ).fetchone()
        if previous is None and not initial:
            raise StorageError("missing_request", "Cannot settle an attempt that was not recorded")
        if previous is not None:
            if previous[1] == payload:
                return
            if initial or previous[0] != "started":
                raise StorageError(
                    "request_id_conflict", "Request ID already has different settled content"
                )
        with self._connection:
            self._connection.execute(
                "INSERT INTO model_requests VALUES (?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET "
                "status=excluded.status, payload=excluded.payload",
                (record.request_id, record.session_id, record.status, payload),
            )

    async def list_requests(self, session_id: str | None = None) -> tuple[RequestRecord, ...]:
        def read():
            query = "SELECT payload FROM model_requests"
            arguments = ()
            if session_id is not None:
                query += " WHERE session_id = ?"
                arguments = (session_id,)
            query += " ORDER BY rowid"
            output = []
            for (payload,) in self._connection.execute(query, arguments):
                output.append(decode_request(payload))
            return tuple(output)

        return await self.run_io(read)

    async def aclose(self) -> None:
        await self.run_io(self._connection.close)

    async def interrupt_unsettled(self, session_id: str, run_id: str) -> None:
        for record in await self.list_requests(session_id):
            if record.run_id == run_id and record.status == "started":
                await self.settle(
                    replace(
                        record,
                        status="interrupted",
                        error_code="process_interrupted",
                        settled_at=time.time(),
                    )
                )
