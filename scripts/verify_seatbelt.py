"""Run physical macOS write/network checks; other platforms remain unverified."""

import argparse
import asyncio
import json
import platform
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from pycode.kernel import DomainError
from pycode.sandbox import Sandbox, SandboxSettings


def check(passed, name):
    # An explicit check, so the probe still fails under python -O.
    if not passed:
        raise DomainError("seatbelt_check_failed", name)


async def execute(sandbox, workspace, command, *, network=False, mode="on"):
    configured = Sandbox(SandboxSettings(mode, network))
    argv = await configured.launch(("/bin/bash", "-c", command), str(workspace))
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(10):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return {
        "exit_code": process.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


async def verify(root):
    workspace = root / "tests/.runtime-tests" / ("seatbelt-" + uuid4().hex)
    workspace.mkdir(parents=True)
    outside = workspace.parent / (workspace.name + "-outside")
    sandbox = Sandbox(SandboxSettings("on"))
    manual = await sandbox.status()
    automatic = await Sandbox(SandboxSettings("on-auto")).status()
    check(manual.enabled and not manual.auto_approve, "status_on")
    check(automatic.enabled and automatic.auto_approve, "status_on_auto")
    allowed = await execute(
        sandbox, workspace, "printf allowed > " + shlex.quote(str(workspace / "inside.txt"))
    )
    denied = await execute(sandbox, workspace, "printf denied > " + shlex.quote(str(outside)))
    check(
        allowed["exit_code"] == 0 and (workspace / "inside.txt").read_text() == "allowed",
        "write_inside",
    )
    check(denied["exit_code"] != 0 and not outside.exists(), "write_outside")
    auto_allowed = await execute(
        sandbox,
        workspace,
        "printf automatic > " + shlex.quote(str(workspace / "auto.txt")),
        mode="on-auto",
    )
    auto_denied = await execute(
        sandbox, workspace, "printf denied > " + shlex.quote(str(outside)), mode="on-auto"
    )
    check(
        auto_allowed["exit_code"] == 0 and (workspace / "auto.txt").read_text() == "automatic",
        "on_auto_write_inside",
    )
    check(auto_denied["exit_code"] != 0 and not outside.exists(), "on_auto_write_outside")
    protected = {}
    for relative in (".env", ".git/config", ".pycode/state.json"):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("protected-test-marker")
        rejected = await execute(sandbox, workspace, "printf changed > " + shlex.quote(str(path)))
        check(
            rejected["exit_code"] != 0 and path.read_text() == "protected-test-marker",
            "protected_path",
        )
        protected[relative] = rejected
    # /Users is outside the read allow list; only its listing is requested.
    read_outside = await execute(sandbox, workspace, "ls /Users")
    check(read_outside["exit_code"] != 0, "read_outside")
    (workspace / ".env.local").write_text("protected-test-marker")
    renamed = shlex.quote(str(workspace / "renamed.txt"))
    dotenv_rename = await execute(
        sandbox,
        workspace,
        f"mv {shlex.quote(str(workspace / '.env.local'))} {renamed}; cat {renamed}",
    )
    check("protected-test-marker" not in dotenv_rename["stdout"], "dotenv_rename")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"local-probe")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        command = "/usr/bin/curl --silent --show-error --max-time 3 http://127.0.0.1:" + str(
            server.server_port
        )
        no_network = await execute(sandbox, workspace, command)
        with_network = await execute(sandbox, workspace, command, network=True)
        check(no_network["exit_code"] != 0, "no_network")
        check(
            with_network["exit_code"] == 0 and with_network["stdout"] == "local-probe",
            "with_network",
        )
        return {
            "status": "passed",
            "write_inside": allowed,
            "write_outside": denied,
            "on_auto_write_inside": auto_allowed,
            "on_auto_write_outside": auto_denied,
            "protected_state": protected,
            "read_outside_allow_list": read_outside,
            "dotenv_rename": dotenv_rename,
            "modes": {
                "on": {"enabled": manual.enabled, "auto_approve": manual.auto_approve},
                "on-auto": {"enabled": automatic.enabled, "auto_approve": automatic.auto_approve},
            },
            "network_denied": no_network,
            "network_allowed": with_network,
        }
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        report = {"status": "unverified", "reason": "Physical Seatbelt enforcement requires macOS"}
    else:
        report = asyncio.run(verify(Path(__file__).resolve().parents[1]))
    report["platform"] = platform.platform()
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
