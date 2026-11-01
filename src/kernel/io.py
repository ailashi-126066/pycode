# Pi 没有，pycode 新增
"""The only kernel file doing I/O: atomic writes, file leases, clock and path policy."""

import asyncio
import hashlib
import os
import time
from pathlib import Path
from uuid import uuid4

from .errors import StorageError


def is_internal_component(part):
    # State directory names are reserved across platforms, including aliases
    # on the common case-insensitive macOS and Windows filesystems.
    return part.casefold() in {".git", ".pycode"}


def file_sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    temporary = path.with_name(f".{uuid4().hex}.pycode-write.partial")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def owned_io(function, *arguments):
    task = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


if os.name == "nt":
    import msvcrt
else:
    import fcntl


class FileLease:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise StorageError("session_busy", "Another writer owns this session") from error
        self._stream = stream
        return True

    def close(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class SystemTiming:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
