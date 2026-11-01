# Source: author-owned tools/bash.py and processes.py; fixed hashes in docs/source-map.md.
# Adaptation: shared argv/stdin runner for Bash and cancellable Python search workers.
"""Own native process groups and stream output through one publication lifecycle."""

import asyncio
import codecs
import os
import signal
import subprocess
from dataclasses import replace

from pycode.kernel import ToolError
from pycode.sandbox.adapters.tools.windows_job import WindowsJob


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
