# Pi 没有，pycode 新增；参照 MewCode local mewcode/tools/bash.py 和 mewcode/processes.py
# 固定哈希见 docs/source-map.md。
"""Child processes: an allowlisted environment, owned process groups and streamed output.

Windows job handles are process cleanup only, not an OS command sandbox.
"""

import asyncio
import codecs
import ctypes
import os
import re
import signal
import subprocess
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import replace
from typing import Any, cast

from pycode.kernel import ToolError

# Names a shell, git, Python or a virtual environment needs to behave normally.
ALLOWED_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TMPDIR",
        "TZ",
        "VIRTUAL_ENV",
        # Windows processes cannot start Python, sockets or the shell without these.
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "SYSTEMDRIVE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }
)
ALLOWED_PREFIXES = ("LC_",)
CREDENTIAL = re.compile(
    r"(?:.*_)?API_?KEY|(?:.*_)?TOKEN|.*SECRET.*|.*PASSWORD.*|.*PASSWD.*|"
    r"(?:.*_)?CREDENTIALS?|AWS_.*",
    re.IGNORECASE,
)


def credential_name(name: str) -> bool:
    return CREDENTIAL.fullmatch(name) is not None


def allowed_name(name: str) -> bool:
    upper = name.upper()
    if credential_name(upper):
        return False
    return upper in ALLOWED_NAMES or upper.startswith(ALLOWED_PREFIXES)


def passthrough_name(name: str) -> str:
    """Accept an exact environment name; never allow credential-shaped names."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Environment passthrough requires an exact variable name")
    if credential_name(name):
        raise ValueError("Credential variables cannot be passed to subprocesses")
    return name


def validate_passthrough(names: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(names, tuple):
        raise ValueError("Environment passthrough must be a tuple of exact names")
    return tuple(passthrough_name(name) for name in names)


def subprocess_environment(
    source: Mapping[str, str], *, passthrough: tuple[str, ...] = ()
) -> dict[str, str]:
    """Keep allowlisted names; credential-shaped names are dropped even if allowlisted."""
    extra = set(validate_passthrough(passthrough))
    return {
        name: value
        for name, value in source.items()
        if allowed_name(name) or name in extra and not credential_name(name)
    }


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class IOCounters(ctypes.Structure):
    _fields_ = [
        ("read_operations", ctypes.c_ulonglong),
        ("write_operations", ctypes.c_ulonglong),
        ("other_operations", ctypes.c_ulonglong),
        ("read_bytes", ctypes.c_ulonglong),
        ("write_bytes", ctypes.c_ulonglong),
        ("other_bytes", ctypes.c_ulonglong),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", IOCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
        ("owner_pid", wintypes.DWORD),
        ("priority", wintypes.LONG),
        ("delta_priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
    ]


# ctypes exposes WinDLL, WinError and get_last_error only on Windows; this module also
# imports on POSIX.
WINDOWS_CTYPES = cast(Any, ctypes)


class WindowsJob:
    def __init__(self) -> None:
        self._kernel = WINDOWS_CTYPES.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self._kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self._kernel.Thread32First.restype = wintypes.BOOL
        self._kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self._kernel.Thread32Next.restype = wintypes.BOOL
        self._kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenThread.restype = wintypes.HANDLE
        self._kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel.ResumeThread.restype = wintypes.DWORD
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())

    def attach_and_resume(self, pid: int) -> None:
        process = self._kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, process):
                raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())
        finally:
            self._kernel.CloseHandle(process)
        snapshot = self._kernel.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())
        resumed = False
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = self._kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.owner_pid == pid:
                    thread = self._kernel.OpenThread(0x0002, False, entry.thread_id)
                    if not thread:
                        raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())
                    try:
                        if self._kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise WINDOWS_CTYPES.WinError(WINDOWS_CTYPES.get_last_error())
                        resumed = True
                    finally:
                        self._kernel.CloseHandle(thread)
                found = self._kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel.CloseHandle(snapshot)
        if not resumed:
            raise OSError("Suspended command thread could not be resumed")

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


async def stop_process(process, job=None) -> None:
    if job is not None:
        job.close()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    elif os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
        if process.returncode is None:
            process.kill()
    await process.wait()


async def run_process(
    argv,
    workspace,
    writer,
    result,
    *,
    limit_seconds: float = 120,
    stdin=None,
    prefix="command",
    environment=None,
    progress=None,
):
    job = None
    process = None
    try:
        try:
            job = WindowsJob() if os.name == "nt" else None
        except OSError as error:
            raise ToolError("command_group_unavailable", "Cannot own the process group") from error
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x00000004
            if os.name == "nt"
            else 0
        )
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=workspace,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name == "posix",
                creationflags=flags,
                env=environment,
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            await stop_process(process, job)
            raise
        if job is not None:
            try:
                job.attach_and_resume(process.pid)
            except OSError as error:
                raise ToolError(
                    "command_group_unavailable", "Cannot own the process group"
                ) from error
        if stdin is not None:
            input_stream = process.stdin
            if input_stream is None:
                raise ToolError("command_input_unavailable", "Process input pipe was not created")
            input_stream.write(stdin)
            await input_stream.drain()
            input_stream.close()
            await input_stream.wait_closed()
        output_stream = process.stdout
        if output_stream is None:
            raise ToolError("command_output_unavailable", "Process output pipe was not created")

        async def collect():
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                data = await output_stream.read(64 * 1024)
                if not data:
                    tail = decoder.decode(b"", final=True)
                    if tail and progress is not None:
                        await progress(tail)
                    return
                await writer.append(data)
                if progress is not None:
                    text = decoder.decode(data)
                    for offset in range(0, len(text), 4096):
                        await progress(text[offset : offset + 4096])

        timed_out = False
        async with asyncio.TaskGroup() as group:
            reader = group.create_task(collect())
            try:
                async with asyncio.timeout(limit_seconds):
                    await process.wait()
                    await reader
            except TimeoutError:
                timed_out = True
                await stop_process(process, job)
                await reader
            except asyncio.CancelledError:
                await stop_process(process, job)
                await reader
                raise
        settled = replace(
            result,
            is_error=timed_out or process.returncode != 0,
            error_code=f"{prefix}_timeout"
            if timed_out
            else (f"{prefix}_failed" if prefix != "command" and process.returncode != 0 else None),
            exit_code=process.returncode,
            details={"timed_out": timed_out},
        )
        return await writer.finish(settled)
    except BaseException:
        if process is not None:
            await stop_process(process, job)
        await writer.abort()
        raise
    finally:
        if job is not None:
            job.close()
