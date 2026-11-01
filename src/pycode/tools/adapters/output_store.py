# Source: https://github.com/earendil-works/pi/blob/42a3497d03ad17e308a2299fa824727894f2c0ec/packages/coding-agent/src/core/tools/output-accumulator.ts
# Adaptation: persistent atomic objects, stable IDs, bounded buffers and scoped readback.
"""Complete output objects and bounded previews; model inputs never supply paths."""

import asyncio
import hashlib
import io
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import TypeAdapter

from pycode.kernel import (
    MAX_BYTES,
    MAX_LINES,
    PreviewMode,
    SessionTree,
    ToolContext,
    ToolError,
    ToolResult,
    json_dumps,
    line_count,
    plain_data,
    truncate,
)
from pycode.kernel import HistoryJournal as SessionStore
from pycode.kernel.adapters.path_io import atomic_write, io_path, owned_io
from pycode.tools.adapters.read_range import read_range
from pycode.tools.domain.outputs import CONTROL_DETAILS, OutputMetadata
from pycode.tools.ports.outputs import SessionHeaderReader


def output_identifier(context: ToolContext) -> str:
    identity = f"{context.session_id}:{context.operation_id}"
    if context.attempt:
        identity += f":attempt-{context.attempt}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return "output_" + digest


def artifact_exceeds_limit(result: ToolResult) -> bool:
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False)
    payload = {"structured_content": result.structured_content, "details": result.details}
    for piece in encoder.iterencode(plain_data(payload)):
        size += len(piece.encode())
        if size > MAX_BYTES:
            return True
    return False


def externalized_details(details: dict, output: dict) -> dict:
    control = {key: value for key, value in details.items() if key in CONTROL_DETAILS}
    return {**control, "output": output}


def owned_result(context: ToolContext, result: ToolResult) -> ToolResult:
    return replace(
        result,
        call_id=result.call_id or context.call_id,
        name=result.name or context.tool_name,
        session_id=context.session_id,
        run_id=context.run_id,
        operation_id=context.operation_id,
    )


def preview_text(text, mode, identifier, small):
    if small:
        return text
    body = truncate(text, mode, max_lines=MAX_LINES - 1, max_bytes=MAX_BYTES - 512).text
    return body + f"\n[Output limited; full result {identifier}. Read with ReadToolResult.]"


def entry_result(entry):
    if entry.message is not None:
        return entry.message.tool_result
    if entry.type == "hook_result":
        return ToolResult(**entry.data["result"])
    return None


class FileToolOutputStore:
    def __init__(
        self,
        sessions: Path,
        session_id: str,
        journal: SessionStore | None = None,
        *,
        header_reader: SessionHeaderReader | None = None,
    ) -> None:
        self._sessions = sessions
        self._header_reader = header_reader
        self._session_id = session_id
        self._journal = journal
        self._lock = asyncio.Lock()

    def can_inline(self) -> bool:
        return self._journal is not None

    async def run_io(self, operation):
        async with self._lock:
            try:
                return await owned_io(operation)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise ToolError("output_storage_error", "Tool output storage failed") from error

    def directory(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
            raise ToolError("output_not_accessible", "Invalid output owner")
        directory = (self._sessions / session_id / "tool-results").resolve()
        if not directory.is_relative_to(self._sessions.resolve()):
            raise ToolError("output_not_accessible", "Output owner escapes session storage")
        return io_path(directory)

    def validate_id(self, output_id: str) -> None:
        if not re.fullmatch(r"output_[a-f0-9]{32}", output_id):
            raise ToolError("invalid_output_id", "ReadToolResult requires a registered output ID")

    def metadata(self, output_id: str, owner: str | None = None) -> OutputMetadata:
        self.validate_id(output_id)
        path = self.directory(owner or self._session_id) / f"{output_id}.json"
        if not path.exists():
            raise ToolError("output_missing", "Complete tool output metadata is missing")
        metadata = TypeAdapter(OutputMetadata).validate_json(path.read_bytes(), strict=True)
        if metadata.output_id != output_id or metadata.session_id != (owner or self._session_id):
            raise ToolError("output_corrupt", "Tool output identity does not match its object")
        return metadata

    def publish_metadata(self, metadata: OutputMetadata) -> None:
        directory = self.directory(metadata.session_id)
        target = directory / f"{metadata.output_id}.json"
        encoded = json_dumps(metadata, sort_keys=True, ensure_ascii=False).encode("utf-8")
        atomic_write(target, encoded)

    async def begin(self, context: ToolContext, mode: PreviewMode) -> "StreamingOutputWriter":
        if context.session_id != self._session_id:
            raise ToolError("output_not_accessible", "Tool output belongs to a different session")
        writer = StreamingOutputWriter(self, context, mode)
        await self.run_io(writer.open)
        return writer

    async def prepare(
        self, context: ToolContext, result: ToolResult, mode: PreviewMode
    ) -> ToolResult:
        if context.session_id != self._session_id:
            raise ToolError(
                "output_not_accessible", "Output context belongs to a different session"
            )
        result = owned_result(context, result)
        if not result.text and result.structured_content is not None:
            result = replace(
                result,
                text=json_dumps(result.structured_content, ensure_ascii=False, sort_keys=True),
            )
        identifier = output_identifier(context)
        if result.output_id is not None:
            if result.output_id != identifier:
                raise ToolError(
                    "output_identity_conflict", "Tool supplied an unrelated output reference"
                )
            metadata = await self.run_io(lambda: self.metadata(identifier))
            if (
                metadata.status != "completed"
                or metadata.operation_id != context.operation_id
                or metadata.call_id != result.call_id
                or metadata.mode != mode
                or metadata.is_error != result.is_error
                or metadata.exit_code != result.exit_code
            ):
                raise ToolError("output_identity_conflict", "Output operation is not complete")
            if hashlib.sha256(result.text.encode()).hexdigest() != metadata.preview_sha256:
                raise ToolError("output_identity_conflict", "Published output preview changed")
            return result
        data = result.text.encode("utf-8")
        small = (
            len(data) <= MAX_BYTES
            and line_count(result.text) <= MAX_LINES
            and not artifact_exceeds_limit(result)
        )

        def already_published():
            path = self.directory(context.session_id) / f"{identifier}.json"
            if not path.exists():
                return None
            metadata = self.metadata(identifier)
            if (
                metadata.sha256 != hashlib.sha256(data).hexdigest()
                or metadata.is_error != result.is_error
                or metadata.exit_code != result.exit_code
                or metadata.call_id != result.call_id
                or metadata.mode != mode
                or metadata.status != "completed"
            ):
                raise ToolError(
                    "output_identity_conflict", "Repeated operation output has different content"
                )
            return metadata

        previous = await self.run_io(already_published)
        if previous is not None:
            return replace(
                result,
                text=preview_text(result.text, mode, identifier, small),
                output_id=identifier,
                structured_content=result.structured_content if small else None,
                details=result.details
                if small
                else externalized_details(
                    result.details,
                    {"bytes": previous.byte_count, "lines": previous.line_count},
                ),
            )
        if small and self.can_inline():
            prepared = replace(result, output_id=identifier)
            metadata = OutputMetadata(
                identifier,
                context.session_id,
                result.call_id,
                context.operation_id,
                len(data),
                line_count(result.text),
                hashlib.sha256(data).hexdigest(),
                "",
                mode,
                "inline",
                is_error=result.is_error,
                exit_code=result.exit_code,
                preview_sha256=hashlib.sha256(prepared.text.encode()).hexdigest(),
            )
            await self.run_io(lambda: self.publish_metadata(metadata))
            return prepared
        writer = await self.begin(context, mode)
        try:
            for offset in range(0, len(data), 64 * 1024):
                await writer.append(data[offset : offset + 64 * 1024])
            return await writer.finish(result)
        except BaseException:
            await writer.abort()
            raise

    async def bind(self, output_id: str, entry_id: str) -> None:
        result = None
        if self._journal is not None:
            snapshot = await self._journal.snapshot()
            result = entry_result(SessionTree(snapshot.index).get(entry_id))

        def bind_entry():
            metadata = self.metadata(output_id)
            if metadata.source_entry_id not in {None, entry_id}:
                raise ToolError(
                    "output_origin_conflict", "Tool output already belongs to another entry"
                )
            storage = metadata.storage
            path = self.directory(metadata.session_id) / (output_id + ".txt")
            if (
                result is not None
                and storage == "file"
                and metadata.status == "completed"
                and not metadata.artifact
                and metadata.byte_count <= MAX_BYTES
                and metadata.line_count <= MAX_LINES
                and hashlib.sha256(result.text.encode()).hexdigest() == metadata.sha256
            ):
                storage = "inline"
            self.publish_metadata(replace(metadata, source_entry_id=entry_id, storage=storage))
            if storage == "inline":
                path.unlink(missing_ok=True)

        await self.run_io(bind_entry)

    async def interrupted_reference(self, context):
        if context.session_id != self._session_id:
            raise ToolError(
                "output_not_accessible", "Interrupted output belongs to another session"
            )

        def read_reference():
            identifier = output_identifier(context)
            directory = self.directory(self._session_id)
            partial = directory / (identifier + ".partial")
            path = directory / (identifier + ".txt")
            metadata_path = directory / (identifier + ".json")
            if partial.exists() and not metadata_path.exists():
                with partial.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                    size = source.seek(0, io.SEEK_END)
                os.replace(partial, path)
                self.publish_metadata(
                    OutputMetadata(
                        identifier,
                        self._session_id,
                        context.call_id,
                        context.operation_id,
                        size,
                        0,
                        digest,
                        "",
                        "tail",
                        "file",
                        "interrupted",
                        is_error=True,
                    )
                )
            if not metadata_path.exists():
                return None
            metadata = self.metadata(identifier)
            if metadata.operation_id != context.operation_id or metadata.call_id != context.call_id:
                raise ToolError("output_origin_conflict", "Interrupted output identity changed")
            if metadata.storage == "inline":
                return None
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
                size = source.seek(0, io.SEEK_END)
            if digest != metadata.sha256 or size != metadata.byte_count:
                raise ToolError("output_corrupt", "Interrupted output checksum failed")
            return identifier

        return await self.run_io(read_reference)

    async def read(
        self, output_id, offset, limit, max_bytes, byte_offset=0, *, absolute_byte_offset=None
    ):
        self.validate_id(output_id)
        if (
            offset < 1
            or not 1 <= limit <= MAX_LINES
            or not 4 <= max_bytes <= MAX_BYTES
            or byte_offset < 0
            or (
                absolute_byte_offset is not None
                and (type(absolute_byte_offset) is not int or absolute_byte_offset < 0)
            )
        ):
            raise ToolError("invalid_output_range", "Output range exceeds line or byte limits")
        entry = None
        original_result = None
        owner = self._session_id
        snapshot = await self._journal.snapshot() if self._journal is not None else None
        if snapshot is not None:
            for candidate in SessionTree(snapshot.index).path(snapshot.leaf_id):
                original = entry_result(candidate)
                if original is not None and original.output_id == output_id:
                    entry = candidate
                    original_result = original
                    break
            if entry is None or original_result is None:
                raise ToolError("output_not_accessible", "Output is not referenced by this branch")
            owner = original_result.session_id or self._session_id

        def read_object():
            if snapshot is not None and owner != self._session_id:
                parent = snapshot.header.parent_session_id
                visited = set()
                allowed = False
                while parent and parent not in visited:
                    visited.add(parent)
                    if parent == owner:
                        allowed = True
                        break
                    self.directory(parent)
                    if self._header_reader is None:
                        raise ToolError("output_not_accessible", "Ancestry reader is unavailable")
                    parent = self._header_reader.read(parent).parent_session_id
                if not allowed:
                    raise ToolError(
                        "output_not_accessible", "Output owner is outside fork ancestry"
                    )
            metadata = self.metadata(output_id, owner)
            if (
                entry is not None
                and original_result is not None
                and (
                    metadata.source_entry_id not in {None, entry.id}
                    or metadata.call_id != original_result.call_id
                    or (
                        original_result.operation_id is not None
                        and metadata.operation_id != original_result.operation_id
                    )
                    or (
                        not original_result.details.get("output_recovered")
                        and (
                            metadata.is_error != original_result.is_error
                            or metadata.exit_code != original_result.exit_code
                        )
                    )
                )
            ):
                raise ToolError(
                    "output_origin_conflict", "Output source entry does not match the branch"
                )
            if metadata.artifact:
                artifact = self.directory(owner) / (output_id + ".artifact.json")
                with artifact.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != metadata.artifact_sha256:
                    raise ToolError("output_corrupt", "Output artifact checksum failed")
            if metadata.storage == "inline":
                if entry is None or original_result is None:
                    raise ToolError(
                        "output_unbound", "Inline output requires its original history entry"
                    )
                raw = original_result.text.encode()
                if (
                    hashlib.sha256(raw).hexdigest() != metadata.sha256
                    or len(raw) != metadata.byte_count
                ):
                    raise ToolError("output_corrupt", "Inline output content verification failed")
                stream = io.BytesIO(raw)
            else:
                path = self.directory(owner) / f"{output_id}.txt"
                if not path.exists():
                    raise ToolError(
                        "output_missing",
                        "Complete output object is missing; original tool will not be rerun",
                    )
                stream = path.open("rb")
            with stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                size = stream.seek(0, io.SEEK_END)
                if digest != metadata.sha256 or size != metadata.byte_count:
                    raise ToolError("output_corrupt", "Complete output content verification failed")
                stream.seek(0)
                return read_range(
                    stream, output_id, offset, limit, max_bytes, byte_offset, absolute_byte_offset
                )

        return await self.run_io(read_object)


class StreamingOutputWriter:
    def __init__(self, store: FileToolOutputStore, context: ToolContext, mode: PreviewMode) -> None:
        self._store = store
        self._context = context
        self._mode: PreviewMode = mode
        self._id = output_identifier(context)
        self._stream: BinaryIO | None = None
        self._temporary: Path | None = None
        self._digest = hashlib.sha256()
        self._size = 0
        self._newlines = 0
        self._last_byte: int | None = None
        self._head = bytearray()
        self._tail = b""
        self._finished = False

    def open(self) -> None:
        directory = self._store.directory(self._context.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / f"{self._id}.json").exists() or (directory / f"{self._id}.txt").exists():
            raise ToolError(
                "output_operation_conflict",
                "Operation output already exists; reconcile before re-executing",
            )
        self._temporary = directory / f"{self._id}.partial"
        self._stream = self._temporary.open("xb")

    async def append(self, data: bytes) -> None:
        def write():
            stream = self._stream
            if self._finished or stream is None:
                raise ToolError("output_closed", "Cannot append to settled output")
            stream.write(data)
            self._digest.update(data)
            self._size += len(data)
            self._newlines += data.count(b"\n")
            if data:
                self._last_byte = data[-1]
            remaining = MAX_BYTES * 2 - len(self._head)
            if remaining > 0:
                self._head.extend(data[:remaining])
            rolling = MAX_BYTES * 2
            self._tail = data[-rolling:] if len(data) >= rolling else (self._tail + data)[-rolling:]

        await self._store.run_io(write)

    async def finish(self, result: ToolResult) -> ToolResult:
        return await self._store.run_io(lambda: self.publish(result, "completed"))

    async def abort(self) -> None:
        if self._finished:
            return
        if self._stream is None or self._stream.closed:
            self._finished = True
            return
        await self._store.run_io(
            lambda: self.publish(
                ToolResult("", "", "", is_error=True, error_code="interrupted"), "interrupted"
            )
        )

    def publish(
        self, result: ToolResult, status: Literal["completed", "interrupted"]
    ) -> ToolResult:
        stream, temporary = self._stream, self._temporary
        if self._finished or stream is None or temporary is None:
            raise ToolError("output_closed", "Output is already settled")
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        total_lines = self._newlines + (1 if self._size and self._last_byte != 10 else 0)
        small = self._size <= MAX_BYTES and total_lines <= MAX_LINES
        raw = (
            bytes(self._head)
            if small
            else bytes(self._head)
            if self._mode == "head"
            else self._tail
        )
        if not small and self._mode == "tail":
            start = 0
            while start < len(raw) and raw[start] & 0xC0 == 0x80:
                start += 1
            raw = raw[start:]
        text = raw.decode("utf-8", errors="replace")
        small = small and len(text.encode()) <= MAX_BYTES
        small = small and not artifact_exceeds_limit(result)
        preview = text
        if not small:
            body = truncate(
                text, self._mode, max_lines=MAX_LINES - 1, max_bytes=MAX_BYTES - 512
            ).text
            preview = (
                body + f"\n[Output limited; full result {self._id}. Read with ReadToolResult.]"
            )
        directory = self._store.directory(self._context.session_id)
        # Streaming bytes remain on disk until their session Store result is bound.
        storage: Literal["file", "inline"] = "file"
        artifact = not small and (result.structured_content is not None or bool(result.details))
        artifact_digest = None
        if artifact:
            target = directory / f"{self._id}.artifact.json"
            atomic_write(
                target,
                json.dumps(
                    {
                        "structured_content": result.structured_content,
                        "details": result.details,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            with target.open("rb") as source:
                artifact_digest = hashlib.file_digest(source, "sha256").hexdigest()
        if storage == "file":
            os.replace(temporary, directory / f"{self._id}.txt")
        else:
            temporary.unlink()
        details = (
            result.details
            if small
            else externalized_details(
                result.details,
                {"bytes": self._size, "lines": total_lines, "sha256": self._digest.hexdigest()},
            )
        )
        prepared = owned_result(
            self._context,
            replace(
                result,
                text=preview,
                output_id=self._id,
                structured_content=result.structured_content if small else None,
                details=details,
            ),
        )
        metadata = OutputMetadata(
            self._id,
            self._context.session_id,
            result.call_id or self._context.call_id,
            self._context.operation_id,
            self._size,
            total_lines,
            self._digest.hexdigest(),
            "",
            self._mode,
            storage,
            status,
            is_error=result.is_error,
            exit_code=result.exit_code,
            artifact=artifact,
            artifact_sha256=artifact_digest,
            preview_sha256=hashlib.sha256(prepared.text.encode()).hexdigest(),
        )
        self._store.publish_metadata(metadata)
        self._finished = True
        return prepared
